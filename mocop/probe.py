from __future__ import annotations

import csv
import io
import math
import os
import selectors
import signal
import subprocess
import threading
import time
import weakref
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from .config import MonitorConfig, ThresholdConfig, is_safe_alias
from .models import (
    DiskMetrics,
    GpuHealthMetrics,
    GpuMetrics,
    GpuProcess,
    PressureStallMetrics,
    PressureStallSample,
    ProbeResult,
    SystemMetrics,
    WorkloadMetadata,
    utc_now,
)
from .remote_script import (
    _COMBINED_QUERY_FIELDS,
    _HEALTH_QUERY_FIELDS,
    _PROCESS_QUERY_FIELDS,
    _QUERY_FIELDS,
    _SUPPORTED_PROTOCOL_VERSIONS,
    _remote_script,
)

_UNAVAILABLE = {"", "n/a", "[n/a]", "not supported", "[not supported]"}
_PROCESS_READ_CHUNK_BYTES = 65_536
_MAX_GPUS_PER_HOST = 256
_MAX_DISKS_PER_HOST = 1_024
# Single-file mounts injected by Docker, containerd and Kubernetes.
_CONTAINER_FILE_BIND_MOUNTS = frozenset(
    {"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"}
)
_MAX_PROCESSES_PER_HOST = 4_096


class ResourceProbe(Protocol):
    def probe(self, host: str, config: MonitorConfig) -> ProbeResult: ...


@runtime_checkable
class CancellableResourceProbe(Protocol):
    """Optional lifecycle extension for probes that own child processes."""

    def cancel(self) -> None: ...


@runtime_checkable
class InventoryAwareResourceProbe(Protocol):
    """Optional extension for probes that retain per-host sampling state."""

    def retain_hosts(self, hosts: set[str]) -> None: ...


@runtime_checkable
class AttendedAwareResourceProbe(Protocol):
    """Optional extension for probes that relax cadence without viewers."""

    def set_attended(self, attended: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    returncode: int
    stdout: str
    stderr: str


class _ProcessOutputLimitExceeded(RuntimeError):
    pass


class _ProcessCancelled(RuntimeError):
    pass


class _ActiveProcessRegistry:
    """Own active collectors so shutdown can interrupt them without polling."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        # Raw descriptors: the registry lives for the whole probe lifetime and
        # selectors accept integer descriptors directly. A finalizer reclaims
        # both ends so recreating probes never leaks the wake-up pipe.
        self.cancel_wakeup, self._cancel_write_fd = os.pipe()
        os.set_blocking(self._cancel_write_fd, False)
        self._finalizer = weakref.finalize(
            self,
            _ActiveProcessRegistry._close_fds,
            self.cancel_wakeup,
            self._cancel_write_fd,
        )

    @staticmethod
    def _close_fds(read_fd: int, write_fd: int) -> None:
        for fd in (read_fd, write_fd):
            with suppress(OSError):
                os.close(fd)

    def close(self) -> None:
        """Idempotently reclaim the wake-up pipe once the probe is done."""
        self._finalizer()

    def register(self, process: subprocess.Popen[bytes]) -> bool:
        with self._lock:
            if self.cancelled.is_set():
                return False
            self._processes.add(process)
            return True

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self) -> None:
        self.cancelled.set()
        # Level-triggered wake-up: the byte is intentionally never consumed,
        # so every selector that registered the read end stays readable.
        with suppress(OSError):
            os.write(self._cancel_write_fd, b"x")
        with self._lock:
            processes = tuple(self._processes)
        for process in processes:
            _kill_process_group(process)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        with suppress(OSError):
            process.kill()


def _run_bounded_process(
    command: list[str],
    *,
    input_text: str,
    timeout_seconds: float,
    max_output_bytes: int,
    environment: dict[str, str],
    cancel_event: threading.Event | None = None,
    process_registry: _ActiveProcessRegistry | None = None,
) -> _BoundedProcessResult:
    """Run one SSH process while bounding combined stdout/stderr in memory."""
    if cancel_event is not None and cancel_event.is_set():
        raise _ProcessCancelled
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    stdin = process.stdin
    stdout = process.stdout
    stderr = process.stderr
    registered = process_registry.register(process) if process_registry else False

    try:
        if process_registry is not None and not registered:
            raise _ProcessCancelled
        if stdin is None or stdout is None or stderr is None:
            raise RuntimeError("SSH process pipes were not created")
        try:
            stdin.write(input_text.encode("utf-8"))
        except BrokenPipeError:
            pass
        finally:
            # A process that exits before draining stdin makes both the write
            # above and the implicit flush on close raise EPIPE; swallow it so
            # the real exit status still drives classification and mux retries.
            with suppress(BrokenPipeError):
                stdin.close()

        output = {"stdout": bytearray(), "stderr": bytearray()}
        total_bytes = 0
        deadline = time.monotonic() + timeout_seconds
        selector = selectors.DefaultSelector()
        selector.register(stdout, selectors.EVENT_READ, "stdout")
        selector.register(stderr, selectors.EVENT_READ, "stderr")
        stream_keys = 2
        if process_registry is not None:
            # A cancellation wake-up descriptor lets the wait span the full
            # deadline; without one, a bounded poll observes the bare event.
            selector.register(
                process_registry.cancel_wakeup, selectors.EVENT_READ, "cancel"
            )
        try:
            while stream_keys:
                if cancel_event is not None and cancel_event.is_set():
                    raise _ProcessCancelled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        command, timeout_seconds, output=bytes(output["stdout"])
                    )
                wait_seconds = (
                    remaining if process_registry is not None else min(remaining, 0.25)
                )
                events = selector.select(wait_seconds)
                for key, _mask in events:
                    if key.data == "cancel":
                        # The wake-up byte stays unread so concurrent probes
                        # sharing the registry observe the same level trigger.
                        raise _ProcessCancelled
                    chunk = os.read(key.fileobj.fileno(), _PROCESS_READ_CHUNK_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        stream_keys -= 1
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > max_output_bytes:
                        raise _ProcessOutputLimitExceeded
                    output[key.data].extend(chunk)
        except (
            subprocess.TimeoutExpired,
            _ProcessOutputLimitExceeded,
            _ProcessCancelled,
        ):
            _kill_process_group(process)
            raise
        finally:
            selector.close()

        if cancel_event is not None and cancel_event.is_set():
            raise _ProcessCancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            returncode = process.poll()
            if returncode is None:
                raise subprocess.TimeoutExpired(
                    command, timeout_seconds, output=bytes(output["stdout"])
                )
        else:
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(
                    command, timeout_seconds, output=bytes(output["stdout"])
                ) from None
        return _BoundedProcessResult(
            returncode=returncode,
            stdout=output["stdout"].decode("utf-8", errors="replace"),
            stderr=output["stderr"].decode("utf-8", errors="replace"),
        )
    finally:
        if process_registry is not None and registered:
            process_registry.unregister(process)
        if process.poll() is None:
            _kill_process_group(process)
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=1)
        for stream in (stdin, stdout, stderr):
            if stream is not None:
                with suppress(OSError):
                    stream.close()


def _finite_number(normalized: str) -> float | None:
    try:
        result = float(normalized)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _number(value: str) -> float | None:
    normalized = value.strip().lower()
    return None if normalized in _UNAVAILABLE else _finite_number(normalized)


def _required_number(value: str, label: str, minimum: float = 0) -> float:
    result = _number(value)
    if result is None or result < minimum:
        raise ValueError(f"invalid {label}")
    return result


def _optional_number(
    value: str,
    label: str,
    *,
    minimum: float = 0,
    maximum: float | None = None,
) -> float | None:
    normalized = value.strip().lower()
    if normalized in _UNAVAILABLE:
        return None
    result = _finite_number(normalized)
    if result is None or result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"nvidia-smi returned an invalid {label}")
    return result


def _bounded_text(value: str, label: str, maximum: int, fallback: str = "") -> str:
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"nvidia-smi returned an oversized {label}")
    return result or fallback


def _parse_nvidia_smi_row(row: list[str], row_number: int) -> GpuMetrics:
    if len(row) != len(_QUERY_FIELDS):
        raise ValueError(
            f"nvidia-smi returned {len(row)} columns on row {row_number}; "
            f"expected {len(_QUERY_FIELDS)}"
        )
    index = _number(row[0])
    if index is None or not index.is_integer() or not 0 <= index <= 65_535:
        raise ValueError(
            f"nvidia-smi returned an invalid GPU index on row {row_number}"
        )
    return GpuMetrics(
        index=int(index),
        uuid=_bounded_text(row[1], "GPU UUID", 128),
        name=_bounded_text(row[2], "GPU name", 256, fallback="Unknown NVIDIA GPU"),
        driver_version=_bounded_text(row[3], "driver version", 64),
        pstate=(
            None
            if row[4].strip().lower() in _UNAVAILABLE
            else _bounded_text(row[4], "performance state", 32)
        ),
        temperature_c=_optional_number(
            row[5], "GPU temperature", minimum=-100, maximum=250
        ),
        utilization_gpu_pct=_optional_number(row[6], "GPU utilization", maximum=100),
        utilization_memory_pct=_optional_number(
            row[7], "memory utilization", maximum=100
        ),
        memory_total_mib=_optional_number(
            row[8], "total GPU memory", maximum=1_000_000_000
        ),
        memory_used_mib=_optional_number(
            row[9], "used GPU memory", maximum=1_000_000_000
        ),
        memory_free_mib=_optional_number(
            row[10], "free GPU memory", maximum=1_000_000_000
        ),
        power_draw_w=_optional_number(row[11], "GPU power draw", maximum=1_000_000),
        power_limit_w=_optional_number(row[12], "GPU power limit", maximum=1_000_000),
    )


def _csv_rows(payload: str):
    """Yield CSV rows while mapping csv-module errors to protocol errors.

    A NUL byte or an oversized field raises ``csv.Error``, which is not a
    ``ValueError`` and would otherwise escape the payload classification.
    """
    try:
        yield from csv.reader(io.StringIO(payload), skipinitialspace=True)
    except csv.Error as exc:
        raise ValueError(f"nvidia-smi returned unparseable CSV: {exc}") from exc


def parse_nvidia_smi_csv(payload: str) -> tuple[GpuMetrics, ...]:
    rows = _csv_rows(payload)
    gpus: list[GpuMetrics] = []
    for row_number, row in enumerate(rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(gpus) >= _MAX_GPUS_PER_HOST:
            raise ValueError("nvidia-smi returned too many GPU records")
        gpus.append(_parse_nvidia_smi_row(row, row_number))
    return tuple(gpus)


def parse_nvidia_processes_csv(payload: str) -> dict[str, tuple[GpuProcess, ...]]:
    rows = _csv_rows(payload)
    processes: dict[str, list[GpuProcess]] = {}
    count = 0
    for row_number, row in enumerate(rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if count >= _MAX_PROCESSES_PER_HOST:
            raise ValueError("nvidia-smi returned too many GPU process records")
        if len(row) != len(_PROCESS_QUERY_FIELDS):
            raise ValueError(
                f"nvidia-smi returned {len(row)} process columns on row {row_number}; "
                f"expected {len(_PROCESS_QUERY_FIELDS)}"
            )
        gpu_uuid = _bounded_text(row[0], "process GPU UUID", 128)
        pid = _number(row[1])
        if pid is None or not pid.is_integer() or not 1 <= pid <= 2_147_483_647:
            raise ValueError(
                f"nvidia-smi returned an invalid process PID on row {row_number}"
            )
        process = GpuProcess(
            pid=int(pid),
            name=_bounded_text(
                row[2], "GPU process name", 512, fallback="unknown process"
            ),
            used_memory_mib=_optional_number(
                row[3], "GPU process memory", maximum=1_000_000_000
            ),
        )
        gpu_processes = processes.get(gpu_uuid)
        if gpu_processes is None:
            gpu_processes = []
            processes[gpu_uuid] = gpu_processes
        gpu_processes.append(process)
        count += 1
    return {gpu_uuid: tuple(items) for gpu_uuid, items in processes.items()}


_PSI_RESOURCES = ("cpu", "memory", "io")


def _psi_percentage(value: str, label: str, *, required: bool) -> float | None:
    """Parse one pressure average: a percentage between 0 and 100."""
    text = value.strip()
    if not text:
        if required:
            raise ValueError(f"resource payload has an invalid {label}")
        return None
    number = _finite_number(text)
    if number is None or not 0 <= number <= 100:
        raise ValueError(f"resource payload has an invalid {label}")
    return number


def _parse_psi_records(rows: list[list[str]]) -> PressureStallMetrics | None:
    """Build pressure metrics from PSI protocol rows; None when none arrived."""
    if not rows:
        return None
    samples: dict[str, PressureStallSample] = {}
    for row in rows:
        resource = row[1].strip()
        if resource not in _PSI_RESOURCES or resource in samples:
            raise ValueError("resource payload has an invalid pressure record")
        some_avg10 = _psi_percentage(row[2], "pressure average", required=True)
        some_avg60 = _psi_percentage(row[3], "pressure average", required=True)
        assert some_avg10 is not None and some_avg60 is not None
        samples[resource] = PressureStallSample(
            some_avg10=some_avg10,
            some_avg60=some_avg60,
            full_avg10=_psi_percentage(row[4], "pressure average", required=False),
            full_avg60=_psi_percentage(row[5], "pressure average", required=False),
        )
    return PressureStallMetrics(
        cpu=samples.get("cpu"),
        memory=samples.get("memory"),
        io=samples.get("io"),
    )


_MAX_WORKLOAD_START_EPOCH = 4_102_444_800  # 2100-01-01T00:00:00Z


def _workload_start_iso(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if not text.isdigit() or not 0 < int(text) <= _MAX_WORKLOAD_START_EPOCH:
        raise ValueError("resource payload has an invalid workload start time")
    return (
        datetime.fromtimestamp(int(text), tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _sanitized_workload_command(value: str) -> str | None:
    """Bound the display-only command line without discarding the record."""
    cleaned = "".join(
        " " if ord(character) < 32 or 127 <= ord(character) <= 159 else character
        for character in value.replace("\u2028", " ").replace("\u2029", " ")
    ).strip()
    return cleaned[:255] or None


def parse_workload_records(payload: str) -> dict[int, WorkloadMetadata]:
    workloads: dict[int, WorkloadMetadata] = {}
    # ASCII newlines only: a Unicode line boundary inside a command line or
    # environment-derived field must stay within its record instead of
    # splitting it and discarding the whole workload overlay.
    for row_number, line in enumerate(payload.split("\n"), start=1):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 10 or fields[0] != "WORKLOAD":
            raise ValueError(
                f"resource payload has an invalid workload record on row {row_number}"
            )
        pid_value = _number(fields[1])
        if (
            pid_value is None
            or not pid_value.is_integer()
            or not 1 <= pid_value <= 2_147_483_647
        ):
            raise ValueError("resource payload has an invalid workload PID")
        pid = int(pid_value)
        if pid in workloads:
            raise ValueError("resource payload has duplicate workload PIDs")
        kind = fields[2].strip()
        if kind not in {"process", "slurm", "kubernetes", "docker", "podman"}:
            raise ValueError("resource payload has an invalid workload kind")

        def optional_text(value: str, label: str) -> str | None:
            text = value.strip()
            if len(text) > 255 or any(ord(character) < 32 for character in text):
                raise ValueError(f"resource payload has invalid workload {label}")
            return text or None

        workloads[pid] = WorkloadMetadata(
            kind=kind,
            workload_id=optional_text(fields[3], "identifier"),
            name=optional_text(fields[4], "name"),
            owner=optional_text(fields[5], "owner"),
            queue=optional_text(fields[6], "queue"),
            namespace=optional_text(fields[7], "namespace"),
            started_at=_workload_start_iso(fields[8]),
            command=_sanitized_workload_command(fields[9]),
        )
    if len(workloads) > _MAX_PROCESSES_PER_HOST:
        raise ValueError("resource payload has too many workload records")
    return workloads


def _optional_health_boolean(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in _UNAVAILABLE:
        return None
    if normalized in {"yes", "active", "enabled"}:
        return True
    if normalized in {"no", "not active", "disabled"}:
        return False
    raise ValueError("nvidia-smi returned an invalid health boolean")


def _parse_nvidia_health_row(
    row: list[str], row_number: int
) -> tuple[str, GpuHealthMetrics]:
    if len(row) != len(_HEALTH_QUERY_FIELDS):
        raise ValueError(
            f"nvidia-smi returned {len(row)} health columns on row {row_number}; "
            f"expected {len(_HEALTH_QUERY_FIELDS)}"
        )
    gpu_uuid = _bounded_text(row[0], "health GPU UUID", 128)
    ecc_value = _optional_number(
        row[1], "uncorrected ECC count", maximum=9_007_199_254_740_991
    )
    if ecc_value is not None and not ecc_value.is_integer():
        raise ValueError("nvidia-smi returned an invalid uncorrected ECC count")
    mig_value = (
        None
        if row[6].strip().lower() in _UNAVAILABLE
        else _bounded_text(row[6], "MIG mode", 64)
    )
    return gpu_uuid, GpuHealthMetrics(
        ecc_uncorrected_volatile=int(ecc_value) if ecc_value is not None else None,
        retired_pages_pending=_optional_health_boolean(row[2]),
        remapped_rows_pending=_optional_health_boolean(row[3]),
        thermal_slowdown=_optional_health_boolean(row[4]),
        power_brake_slowdown=_optional_health_boolean(row[5]),
        mig_mode=mig_value,
    )


def parse_nvidia_health_csv(payload: str) -> dict[str, GpuHealthMetrics]:
    rows = _csv_rows(payload)
    health: dict[str, GpuHealthMetrics] = {}
    for row_number, row in enumerate(rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(health) >= _MAX_GPUS_PER_HOST:
            raise ValueError("nvidia-smi returned too many GPU health records")
        gpu_uuid, metrics = _parse_nvidia_health_row(row, row_number)
        if gpu_uuid in health:
            raise ValueError("nvidia-smi returned duplicate GPU health records")
        health[gpu_uuid] = metrics
    return health


def parse_nvidia_combined_csv(
    payload: str,
) -> tuple[tuple[GpuMetrics, ...], dict[str, GpuHealthMetrics]]:
    """Parse one query containing base GPU and additive health fields."""
    gpus: list[GpuMetrics] = []
    health: dict[str, GpuHealthMetrics] = {}
    health_valid = True
    rows = _csv_rows(payload)
    for row_number, row in enumerate(rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(gpus) >= _MAX_GPUS_PER_HOST:
            raise ValueError("nvidia-smi returned too many GPU records")
        if len(row) != len(_COMBINED_QUERY_FIELDS):
            raise ValueError(
                f"nvidia-smi returned {len(row)} combined GPU columns on row "
                f"{row_number}; expected {len(_COMBINED_QUERY_FIELDS)}"
            )
        gpus.append(_parse_nvidia_smi_row(row[: len(_QUERY_FIELDS)], row_number))
        if health_valid:
            try:
                gpu_uuid, metrics = _parse_nvidia_health_row(
                    [row[1], *row[len(_QUERY_FIELDS) :]], row_number
                )
                if gpu_uuid in health:
                    raise ValueError("nvidia-smi returned duplicate GPU health records")
                health[gpu_uuid] = metrics
            except ValueError:
                # Optional health fields cannot hide otherwise valid GPU metrics.
                health.clear()
                health_valid = False
    return tuple(gpus), health


@dataclass(frozen=True, slots=True)
class _RawSystemSample:
    hostname: str
    cpu_total_ticks: float
    cpu_idle_ticks: float
    cpu_cores: int
    memory_total_kib: float
    memory_available_kib: float
    swap_total_kib: float
    swap_free_kib: float
    load_1m: float
    load_5m: float
    load_15m: float
    uptime_seconds: float
    network_rx_bytes: float
    network_tx_bytes: float
    disk_read_bytes: float
    disk_write_bytes: float
    disks: tuple[DiskMetrics, ...]
    pressure: PressureStallMetrics | None


@dataclass(frozen=True, slots=True)
class _ParsedResource:
    system: _RawSystemSample
    gpus: tuple[GpuMetrics, ...]
    gpu_message: str | None
    processes_available: bool
    processes_sampled: bool


def _parse_resource_payload(payload: str) -> _ParsedResource:
    # Split on the ASCII newline only: the protocol is line-delimited, and
    # Unicode line boundaries (U+0085/U+2028/U+2029) embedded in a GPU or
    # process name must stay inside their field instead of forging a record
    # separator or a premature section marker.
    lines = payload.split("\n")
    if not lines or lines[0].strip() not in _SUPPORTED_PROTOCOL_VERSIONS:
        raise ValueError("resource payload has an unknown protocol version")

    values: dict[str, list[str]] = {}
    disks: list[DiskMetrics] = []
    psi_rows: list[list[str]] = []
    gpu_lines: list[str] = []
    process_lines: list[str] = []
    health_lines: list[str] = []
    workload_lines: list[str] = []
    gpu_message: str | None = None
    processes_available = True
    processes_sampled = True
    process_status_marker: str | None = None
    health_available = True
    in_gpus = False
    in_disks = False
    in_processes = False
    in_health = False
    in_workloads = False
    section_markers: set[str] = set()

    for line in lines[1:]:
        if line == "GPUS_BEGIN":
            if (
                in_gpus
                or in_disks
                or in_processes
                or in_health
                or in_workloads
                or line in section_markers
            ):
                raise ValueError("resource payload has an invalid GPU section")
            section_markers.add(line)
            in_gpus = True
            continue
        if line == "GPUS_END":
            if not in_gpus or line in section_markers:
                raise ValueError("resource payload has an invalid GPU section")
            section_markers.add(line)
            in_gpus = False
            continue
        if line == "DISKS_BEGIN":
            if (
                in_gpus
                or in_disks
                or in_processes
                or in_health
                or in_workloads
                or line in section_markers
            ):
                raise ValueError("resource payload has an invalid disk section")
            section_markers.add(line)
            in_disks = True
            continue
        if line == "DISKS_END":
            if not in_disks or line in section_markers:
                raise ValueError("resource payload has an invalid disk section")
            section_markers.add(line)
            in_disks = False
            continue
        if line == "PROCESSES_BEGIN":
            if (
                in_gpus
                or in_disks
                or in_processes
                or in_health
                or in_workloads
                or line in section_markers
            ):
                raise ValueError("resource payload has an invalid process section")
            section_markers.add(line)
            in_processes = True
            continue
        if line == "PROCESSES_END":
            if not in_processes or line in section_markers:
                raise ValueError("resource payload has an invalid process section")
            section_markers.add(line)
            in_processes = False
            continue
        if line == "WORKLOADS_BEGIN":
            if (
                in_gpus
                or in_disks
                or in_processes
                or in_health
                or in_workloads
                or line in section_markers
            ):
                raise ValueError("resource payload has an invalid workload section")
            section_markers.add(line)
            in_workloads = True
            continue
        if line == "WORKLOADS_END":
            if not in_workloads or line in section_markers:
                raise ValueError("resource payload has an invalid workload section")
            section_markers.add(line)
            in_workloads = False
            continue
        if line == "GPU_HEALTH_BEGIN":
            if (
                in_gpus
                or in_disks
                or in_processes
                or in_health
                or in_workloads
                or line in section_markers
            ):
                raise ValueError("resource payload has an invalid GPU health section")
            section_markers.add(line)
            in_health = True
            continue
        if line == "GPU_HEALTH_END":
            if not in_health or line in section_markers:
                raise ValueError("resource payload has an invalid GPU health section")
            section_markers.add(line)
            in_health = False
            continue
        if in_gpus:
            if line == "GPU_UNAVAILABLE":
                gpu_message = "nvidia-smi is unavailable"
            elif line.startswith("GPU_ERROR\t"):
                gpu_message = "nvidia-smi query failed"
            elif line.strip():
                gpu_lines.append(line)
            continue
        if in_processes:
            if line == "PROCESS_SKIPPED":
                if process_status_marker is not None or process_lines:
                    raise ValueError(
                        "resource payload has conflicting process telemetry"
                    )
                process_status_marker = "skipped"
                processes_sampled = False
            elif line.startswith("PROCESS_ERROR\t"):
                if process_status_marker is not None or process_lines:
                    raise ValueError(
                        "resource payload has conflicting process telemetry"
                    )
                process_status_marker = "error"
                processes_available = False
            elif line.strip():
                if process_status_marker is not None:
                    raise ValueError(
                        "resource payload has conflicting process telemetry"
                    )
                process_lines.append(line)
            continue
        if in_health:
            if line.startswith("GPU_HEALTH_ERROR\t"):
                health_available = False
                health_lines.clear()
            elif line.strip() and health_available:
                health_lines.append(line)
            continue
        if in_workloads:
            if line.strip():
                workload_lines.append(line)
            continue

        parts = line.split("\t")
        if not parts:
            continue
        if in_disks and parts[0] == "DISK" and len(parts) == 8:
            if len(disks) >= _MAX_DISKS_PER_HOST:
                raise ValueError("resource payload has too many disk records")
            if parts[7] in _CONTAINER_FILE_BIND_MOUNTS:
                # Container runtimes bind-mount these single files from the
                # host, so df reports the host's filesystem under a file path.
                # That capacity is not this target's and would double-count.
                continue
            total = _required_number(parts[3], "disk total") / 1024
            used = _required_number(parts[4], "disk used") / 1024
            available = _required_number(parts[5], "disk available") / 1024
            used_pct = _required_number(parts[6], "disk usage")
            disks.append(
                DiskMetrics(
                    device=parts[1][:255],
                    filesystem_type=parts[2][:64],
                    mountpoint=parts[7][:512],
                    total_mib=round(total, 1),
                    used_mib=round(used, 1),
                    available_mib=round(available, 1),
                    used_pct=min(100, used_pct),
                )
            )
        elif in_disks:
            raise ValueError("resource payload has an invalid disk record")
        elif parts[0] == "PSI":
            # PSI repeats per resource, so it cannot ride the last-wins
            # key/value map; rows are validated together after the scan.
            if len(parts) != 6:
                raise ValueError("resource payload has an invalid pressure record")
            psi_rows.append(parts)
        elif len(parts) >= 2:
            values[parts[0]] = parts[1:]

    expected_markers = {
        "DISKS_BEGIN",
        "DISKS_END",
        "GPUS_BEGIN",
        "GPUS_END",
        "PROCESSES_BEGIN",
        "PROCESSES_END",
        "WORKLOADS_BEGIN",
        "WORKLOADS_END",
        "GPU_HEALTH_BEGIN",
        "GPU_HEALTH_END",
    }
    if (
        section_markers != expected_markers
        or in_disks
        or in_gpus
        or in_processes
        or in_health
        or in_workloads
    ):
        raise ValueError("resource payload has incomplete metric sections")
    if gpu_message is not None and gpu_lines:
        raise ValueError("resource payload has conflicting GPU status")
    if not processes_sampled and workload_lines:
        raise ValueError("resource payload has workload data without a process sample")

    required = {"HOST", "CPU", "CORES", "MEM", "LOAD", "UPTIME", "NET", "IO"}
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError(f"resource payload is missing: {', '.join(missing)}")
    if len(values["CPU"]) != 2 or len(values["MEM"]) != 4:
        raise ValueError("resource payload has invalid CPU or memory fields")
    if len(values["LOAD"]) != 3 or len(values["NET"]) != 2 or len(values["IO"]) != 2:
        raise ValueError(
            "resource payload has invalid load, network or disk I/O fields"
        )

    cores = _required_number(values["CORES"][0], "CPU core count", 1)
    if not cores.is_integer() or cores > 65536:
        raise ValueError("resource payload has invalid CPU core count")

    raw = _RawSystemSample(
        hostname=values["HOST"][0].strip()[:255] or "unknown",
        cpu_total_ticks=_required_number(values["CPU"][0], "CPU total ticks", 1),
        cpu_idle_ticks=_required_number(values["CPU"][1], "CPU idle ticks"),
        cpu_cores=int(cores),
        memory_total_kib=_required_number(values["MEM"][0], "memory total", 1),
        memory_available_kib=_required_number(values["MEM"][1], "memory available"),
        swap_total_kib=_required_number(values["MEM"][2], "swap total"),
        swap_free_kib=_required_number(values["MEM"][3], "swap free"),
        load_1m=_required_number(values["LOAD"][0], "load 1m"),
        load_5m=_required_number(values["LOAD"][1], "load 5m"),
        load_15m=_required_number(values["LOAD"][2], "load 15m"),
        uptime_seconds=_required_number(values["UPTIME"][0], "uptime"),
        network_rx_bytes=_required_number(values["NET"][0], "network RX bytes"),
        network_tx_bytes=_required_number(values["NET"][1], "network TX bytes"),
        disk_read_bytes=_required_number(values["IO"][0], "disk read bytes"),
        disk_write_bytes=_required_number(values["IO"][1], "disk write bytes"),
        disks=tuple(disks),
        pressure=_parse_psi_records(psi_rows),
    )
    gpu_payload = "\n".join(gpu_lines)
    first_gpu_row = next(
        (
            row
            for row in _csv_rows(gpu_payload)
            if row and any(cell.strip() for cell in row)
        ),
        None,
    )
    inline_health: dict[str, GpuHealthMetrics] = {}
    if first_gpu_row is not None and len(first_gpu_row) == len(_COMBINED_QUERY_FIELDS):
        gpus, inline_health = parse_nvidia_combined_csv(gpu_payload)
    else:
        gpus = parse_nvidia_smi_csv(gpu_payload)
    if processes_sampled and processes_available:
        try:
            processes = parse_nvidia_processes_csv("\n".join(process_lines))
        except ValueError:
            # A malformed process row is isolated: the core system and GPU
            # sample stays online while the process view degrades to
            # unavailable, prompting a retry on the next core cadence.
            processes = {}
            processes_available = False
    else:
        processes = {}
    try:
        workloads = parse_workload_records("\n".join(workload_lines))
    except ValueError:
        # Workload metadata is additive; a malformed record only drops the
        # ownership overlay, never the underlying process or GPU sample.
        workloads = {}
    # Attach processes only through an unambiguous UUID foreign key: an empty,
    # unavailable or duplicated GPU UUID must not double-count or misattribute
    # another device's processes.
    uuid_counts = Counter(gpu.uuid for gpu in gpus)

    def _processes_for(uuid: str) -> tuple[GpuProcess, ...]:
        if not uuid or uuid.strip().lower() in _UNAVAILABLE or uuid_counts[uuid] != 1:
            return ()
        return tuple(
            replace(process, workload=workloads.get(process.pid))
            for process in processes.get(uuid, ())
        )

    health = inline_health
    if inline_health and health_lines:
        raise ValueError("resource payload has conflicting GPU health records")
    if health_available and not inline_health:
        try:
            health = parse_nvidia_health_csv("\n".join(health_lines))
        except ValueError:
            # Health is additive; an unsupported field must not hide base metrics.
            health = {}
    gpus = tuple(
        replace(
            gpu,
            processes=_processes_for(gpu.uuid),
            processes_available=processes_available,
            processes_sampled=processes_sampled,
            health=health.get(gpu.uuid),
        )
        for gpu in gpus
    )
    return _ParsedResource(
        system=raw,
        gpus=gpus,
        gpu_message=gpu_message,
        processes_available=processes_available,
        processes_sampled=processes_sampled,
    )


def parse_linux_resource_payload(
    payload: str,
) -> tuple[_RawSystemSample, tuple[GpuMetrics, ...], str | None]:
    parsed = _parse_resource_payload(payload)
    return parsed.system, parsed.gpus, parsed.gpu_message


@dataclass(frozen=True, slots=True)
class _Baseline:
    observed_monotonic: float
    cpu_total_ticks: float
    cpu_idle_ticks: float
    network_rx_bytes: float
    network_tx_bytes: float
    disk_read_bytes: float
    disk_write_bytes: float


@dataclass(frozen=True, slots=True)
class _ProcessSample:
    sampled_at_monotonic: float
    observed_at: str
    workload_mode: str
    processes_by_gpu: dict[str, tuple[GpuProcess, ...]]
    idle_streak: int = 0


_MAX_PROCESS_INTERVAL_STRETCH = 4
# Without a connected dashboard the process list serves only the event
# timeline, so the cadence stretches much further; core telemetry, trends and
# incidents keep their own cadence, and the first viewer forces a catch-up.
_UNATTENDED_PROCESS_INTERVAL_STRETCH = 16


def _gpu_activity(gpus: tuple[GpuMetrics, ...], thresholds: ThresholdConfig) -> bool:
    """Judge whether any device shows compute activity worth fresh process data.

    Unknown utilization or memory cannot prove a device is idle, so it counts
    as activity: the stretch is only ever cancelled, never extended, which
    keeps new-task discovery latency within the base interval.
    """
    for gpu in gpus:
        if gpu.processes:
            return True
        utilization = gpu.utilization_gpu_pct
        if utilization is None or utilization >= thresholds.gpu_busy_pct:
            return True
        used = gpu.memory_used_mib
        total = gpu.memory_total_mib
        if used is None or not total:
            return True
        if (used / total) * 100 >= thresholds.gpu_idle_memory_pct:
            return True
    return False


def _safe_ssh_failure(stderr: str) -> str:
    """Classify SSH failures without exposing remote addresses, users or paths."""
    normalized = stderr.lower()
    categories = (
        (("remote host identification has changed",), "SSH host key changed"),
        (("host key verification failed",), "SSH host key is not trusted"),
        (("permission denied", "authentication failed"), "SSH authentication failed"),
        (
            ("could not resolve hostname", "name or service not known"),
            "SSH name resolution failed",
        ),
        (("connection refused",), "SSH connection was refused"),
        (("connection timed out", "operation timed out"), "SSH connection timed out"),
        (("no route to host", "network is unreachable"), "SSH network is unreachable"),
        (
            ("timeout, server", "server not responding"),
            "SSH transport stopped responding",
        ),
    )
    for needles, message in categories:
        if any(needle in normalized for needle in needles):
            return message
    return "SSH connection failed"


def _is_retryable_ssh_transport_failure(stderr: str) -> bool:
    """Recognize stale multiplexed sessions without retrying hard failures.

    A healthy master can still emit ``mux_client_request_session`` or
    ``control socket connect`` while refusing a session or denying access to
    its socket; those are not dead transports, so an authentication, host-key
    or refusal signal vetoes the retry even when a mux marker is present.
    """
    normalized = stderr.lower()
    hard_failures = (
        "permission denied",
        "authentication failed",
        "session open refused",
        "administratively prohibited",
        "host key verification failed",
        "remote host identification has changed",
        "open failed",
    )
    if any(marker in normalized for marker in hard_failures):
        return False
    stale_markers = (
        "mux_client_request_session",
        "control socket connect",
        "master is dead",
        "broken pipe",
        "read from master failed",
    )
    return any(marker in normalized for marker in stale_markers)


def _force_fresh_transport(command: list[str]) -> list[str]:
    """Return the command with any shared ControlMaster bypassed.

    The reused mux socket may point at a dead master whose keepalive this
    invocation cannot influence, so the recovery attempt opens its own
    connection instead of re-binding to the same stale control path.
    """
    if "--" not in command:
        return list(command)
    separator = command.index("--")
    return [
        *command[:separator],
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        *command[separator:],
    ]


class OpenSshLinuxResourceProbe:
    """Collect Linux and NVIDIA metrics locally or through one fixed SSH script."""

    def __init__(self) -> None:
        self._sample_lock = threading.Lock()
        self._baselines: dict[str, _Baseline] = {}
        self._process_samples: dict[str, _ProcessSample] = {}
        self._activity_hints: dict[str, bool] = {}
        self._process_retry_forced: set[str] = set()
        self._attended = True
        self._processes = _ActiveProcessRegistry()
        self._environment = os.environ.copy()
        self._environment["LC_ALL"] = "C"

    def cancel(self) -> None:
        """Stop active child processes when the owning service is shutting down."""
        self._processes.cancel()

    def close(self) -> None:
        """Release the collector's cancellation pipe once it is retired."""
        self._processes.close()

    def set_attended(self, attended: bool) -> None:
        """Track dashboard presence; a returning viewer forces fresh samples."""
        with self._sample_lock:
            catching_up = attended and not self._attended
            self._attended = attended
            if catching_up:
                # Every cached host refreshes on the next core cycle so the
                # first dialog a viewer opens shows current processes.
                self._process_retry_forced.update(self._process_samples)

    def retain_hosts(self, hosts: set[str]) -> None:
        """Discard rate baselines for removed hosts and later clean re-additions."""
        with self._sample_lock:
            if (
                self._baselines.keys() <= hosts
                and self._process_samples.keys() <= hosts
                and self._activity_hints.keys() <= hosts
                and self._process_retry_forced <= hosts
            ):
                return
            self._baselines = {
                host: baseline
                for host, baseline in self._baselines.items()
                if host in hosts
            }
            self._process_samples = {
                host: sample
                for host, sample in self._process_samples.items()
                if host in hosts
            }
            self._activity_hints = {
                host: hint
                for host, hint in self._activity_hints.items()
                if host in hosts
            }
            self._process_retry_forced &= hosts

    def _system_metrics(
        self, host: str, raw: _RawSystemSample, observed_monotonic: float
    ) -> SystemMetrics:
        current = _Baseline(
            observed_monotonic=observed_monotonic,
            cpu_total_ticks=raw.cpu_total_ticks,
            cpu_idle_ticks=raw.cpu_idle_ticks,
            network_rx_bytes=raw.network_rx_bytes,
            network_tx_bytes=raw.network_tx_bytes,
            disk_read_bytes=raw.disk_read_bytes,
            disk_write_bytes=raw.disk_write_bytes,
        )
        with self._sample_lock:
            previous = self._baselines.get(host)
            self._baselines[host] = current

        cpu_usage: float | None = None
        network_rx_bps: float | None = None
        network_tx_bps: float | None = None
        disk_read_bps: float | None = None
        disk_write_bps: float | None = None
        if previous is not None:
            elapsed = current.observed_monotonic - previous.observed_monotonic
            total_delta = current.cpu_total_ticks - previous.cpu_total_ticks
            idle_delta = current.cpu_idle_ticks - previous.cpu_idle_ticks
            if total_delta > 0 and 0 <= idle_delta <= total_delta:
                cpu_usage = round((1 - idle_delta / total_delta) * 100, 2)
            rx_delta = current.network_rx_bytes - previous.network_rx_bytes
            tx_delta = current.network_tx_bytes - previous.network_tx_bytes
            read_delta = current.disk_read_bytes - previous.disk_read_bytes
            write_delta = current.disk_write_bytes - previous.disk_write_bytes
            if elapsed > 0 and rx_delta >= 0:
                network_rx_bps = round(rx_delta / elapsed, 1)
            if elapsed > 0 and tx_delta >= 0:
                network_tx_bps = round(tx_delta / elapsed, 1)
            if elapsed > 0 and read_delta >= 0:
                disk_read_bps = round(read_delta / elapsed, 1)
            if elapsed > 0 and write_delta >= 0:
                disk_write_bps = round(write_delta / elapsed, 1)

        memory_total = raw.memory_total_kib / 1024
        memory_available = min(memory_total, raw.memory_available_kib / 1024)
        swap_total = raw.swap_total_kib / 1024
        swap_free = min(swap_total, raw.swap_free_kib / 1024)

        unique_disks: dict[str, DiskMetrics] = {}
        for disk in raw.disks:
            unique_disks.setdefault(disk.device, disk)
        disk_total = sum(disk.total_mib for disk in unique_disks.values())
        disk_used = sum(disk.used_mib for disk in unique_disks.values())

        return SystemMetrics(
            hostname=raw.hostname,
            uptime_seconds=round(raw.uptime_seconds, 1),
            load_1m=raw.load_1m,
            load_5m=raw.load_5m,
            load_15m=raw.load_15m,
            cpu_cores=raw.cpu_cores,
            cpu_usage_pct=cpu_usage,
            memory_total_mib=round(memory_total, 1),
            memory_used_mib=round(memory_total - memory_available, 1),
            memory_available_mib=round(memory_available, 1),
            swap_total_mib=round(swap_total, 1),
            swap_used_mib=round(swap_total - swap_free, 1),
            disk_total_mib=round(disk_total, 1),
            disk_used_mib=round(disk_used, 1),
            network_rx_bps=network_rx_bps,
            network_tx_bps=network_tx_bps,
            disk_read_bps=disk_read_bps,
            disk_write_bps=disk_write_bps,
            disks=raw.disks,
            pressure=raw.pressure,
        )

    def _processes_due(
        self,
        host: str,
        now: float,
        interval_seconds: float,
        workload_mode: str,
    ) -> bool:
        with self._sample_lock:
            sample = self._process_samples.get(host)
            active = self._activity_hints.get(host, False)
            forced = host in self._process_retry_forced
            attended = self._attended
        # An unavailable process query is retried every core cycle until it
        # succeeds, so a transient error never leaves the process view stale
        # for the stretched interval.
        if forced or sample is None or sample.workload_mode != workload_mode:
            return True
        # Without a connected dashboard the process list has no reader, so
        # busy and idle devices alike stretch to the unattended ceiling; the
        # attended transition forces an immediate catch-up sample.
        if not attended:
            return (
                now - sample.sampled_at_monotonic
                >= interval_seconds * _UNATTENDED_PROCESS_INTERVAL_STRETCH
            )
        # Idle devices stretch the process cadence up to fourfold; any
        # activity hint from the five-second core telemetry cancels the
        # stretch, so detection latency never exceeds the base interval.
        effective_interval = interval_seconds
        if sample.idle_streak and not active:
            effective_interval = interval_seconds * min(
                2**sample.idle_streak, _MAX_PROCESS_INTERVAL_STRETCH
            )
        return now - sample.sampled_at_monotonic >= effective_interval

    def _merge_process_sample(
        self,
        host: str,
        gpus: tuple[GpuMetrics, ...],
        *,
        process_sampled: bool,
        processes_available: bool,
        sampled_at_monotonic: float,
        observed_at: str,
        workload_mode: str,
        thresholds: ThresholdConfig,
    ) -> tuple[GpuMetrics, ...]:
        activity = _gpu_activity(gpus, thresholds)
        if process_sampled and processes_available:
            processes_by_gpu = {gpu.uuid: gpu.processes for gpu in gpus}
            sampled_idle = not any(processes_by_gpu.values())
            with self._sample_lock:
                previous = self._process_samples.get(host)
                idle_streak = (
                    previous.idle_streak + 1
                    if sampled_idle and previous is not None
                    else int(sampled_idle)
                )
                self._process_samples[host] = _ProcessSample(
                    sampled_at_monotonic=sampled_at_monotonic,
                    observed_at=observed_at,
                    workload_mode=workload_mode,
                    processes_by_gpu=processes_by_gpu,
                    idle_streak=idle_streak,
                )
                # A completed process query authoritatively resets both the
                # activity latch and the forced-retry flag.
                self._activity_hints[host] = activity
                self._process_retry_forced.discard(host)
            return tuple(
                replace(gpu, processes_observed_at=observed_at) for gpu in gpus
            )
        if process_sampled:
            # The query ran but returned no authoritative data: latch any
            # activity seen so far and force a retry until it succeeds.
            with self._sample_lock:
                self._activity_hints[host] = (
                    self._activity_hints.get(host, False) or activity
                )
                self._process_retry_forced.add(host)
            return tuple(replace(gpu, processes_observed_at=None) for gpu in gpus)

        # The query was skipped this cycle: never lose an activity hint raised
        # by an intervening core sample before the next process query runs.
        with self._sample_lock:
            self._activity_hints[host] = (
                self._activity_hints.get(host, False) or activity
            )
            sample = self._process_samples.get(host)
        if sample is None:
            return tuple(
                replace(
                    gpu,
                    processes=(),
                    processes_available=False,
                    processes_sampled=False,
                    processes_observed_at=None,
                )
                for gpu in gpus
            )
        merged: list[GpuMetrics] = []
        for gpu in gpus:
            if gpu.uuid not in sample.processes_by_gpu:
                merged.append(
                    replace(
                        gpu,
                        processes=(),
                        processes_available=False,
                        processes_sampled=False,
                        processes_observed_at=None,
                    )
                )
                continue
            merged.append(
                replace(
                    gpu,
                    processes=sample.processes_by_gpu[gpu.uuid],
                    processes_available=True,
                    processes_sampled=False,
                    processes_observed_at=sample.observed_at,
                )
            )
        return tuple(merged)

    def probe(self, host: str, config: MonitorConfig) -> ProbeResult:
        if not is_safe_alias(host):
            raise ValueError(f"unsafe SSH alias: {host!r}")

        local = config.local_host == host
        # A dead transport must never consume the whole probe budget: two
        # unanswered keepalives bound silent-death detection near the
        # operator's connect tolerance instead of probe_timeout_seconds.
        keepalive_interval = max(2, config.connect_timeout_seconds // 2)
        command = (
            ["sh", "-s"]
            if local
            else [
                "ssh",
                "-F",
                str(config.ssh_config),
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "NumberOfPasswordPrompts=0",
                "-o",
                f"ConnectTimeout={config.connect_timeout_seconds}",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                f"ServerAliveInterval={keepalive_interval}",
                "-o",
                "ServerAliveCountMax=2",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "LogLevel=ERROR",
                "--",
                host,
                "sh",
                "-s",
            ]
        )
        started = time.monotonic()
        process_sampled = self._processes_due(
            host,
            started,
            config.gpu_process_poll_interval_seconds,
            config.workloads.mode,
        )
        override = config.host_override(host)
        timeout_seconds = (
            override.probe_timeout_seconds
            if override and override.probe_timeout_seconds is not None
            else config.probe_timeout_seconds
        )
        deadline = started + timeout_seconds
        script = _remote_script(config.workloads.mode, process_sampled)
        transport_retries = 0
        try:
            completed = _run_bounded_process(
                command,
                input_text=script,
                timeout_seconds=timeout_seconds,
                max_output_bytes=config.max_output_bytes,
                environment=self._environment,
                cancel_event=self._processes.cancelled,
                process_registry=self._processes,
            )
            if (
                not local
                and completed.returncode == 255
                and _is_retryable_ssh_transport_failure(completed.stderr)
            ):
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    transport_retries = 1
                    completed = _run_bounded_process(
                        _force_fresh_transport(command),
                        input_text=script,
                        timeout_seconds=remaining,
                        max_output_bytes=config.max_output_bytes,
                        environment=self._environment,
                        cancel_event=self._processes.cancelled,
                        process_registry=self._processes,
                    )
        except _ProcessCancelled:
            return ProbeResult(
                host=host,
                status="error",
                latency_ms=round((time.monotonic() - started) * 1000),
                message="Resource collection cancelled",
                transport_retries=transport_retries,
            )
        except subprocess.TimeoutExpired as exc:
            # Only a silent connection or transport timeout is "unreachable".
            # Partial output proves the transport reached the host and the
            # remote command started, so a stall there is a remote "error"
            # that must not masquerade as a connectivity incident.
            if local:
                status = "error"
                message = "Local resource collection timed out"
            elif exc.output:
                status = "error"
                message = "Remote collection stalled after partial output"
            else:
                status = "unreachable"
                message = "SSH produced no output before the collection timeout"
            return ProbeResult(
                host=host,
                status=status,
                latency_ms=round((time.monotonic() - started) * 1000),
                message=message,
                transport_retries=transport_retries,
            )
        except _ProcessOutputLimitExceeded:
            return ProbeResult(
                host=host,
                status="error",
                latency_ms=round((time.monotonic() - started) * 1000),
                message=(
                    "Local resource output exceeded the configured limit"
                    if local
                    else "Remote resource output exceeded the configured limit"
                ),
                transport_retries=transport_retries,
            )
        except OSError:
            return ProbeResult(
                host=host,
                status="error",
                latency_ms=round((time.monotonic() - started) * 1000),
                message=(
                    "Local resource probe could not be started"
                    if local
                    else "Local SSH client could not be started"
                ),
                transport_retries=transport_retries,
            )

        observed_monotonic = time.monotonic()
        latency_ms = round((observed_monotonic - started) * 1000)
        if not local and completed.returncode == 255:
            return ProbeResult(
                host=host,
                status="unreachable",
                latency_ms=latency_ms,
                message=_safe_ssh_failure(completed.stderr),
                transport_retries=transport_retries,
            )
        if completed.returncode != 0:
            return ProbeResult(
                host=host,
                status="error",
                latency_ms=latency_ms,
                message=(
                    f"Local resource query failed (exit {completed.returncode})"
                    if local
                    else f"Remote resource query failed (exit {completed.returncode})"
                ),
                transport_retries=transport_retries,
            )
        try:
            parsed = _parse_resource_payload(completed.stdout)
            system = self._system_metrics(host, parsed.system, observed_monotonic)
        except ValueError:
            return ProbeResult(
                host=host,
                status="error",
                latency_ms=latency_ms,
                message=(
                    "Local resource output was not recognized"
                    if local
                    else "Remote resource output was not recognized"
                ),
                transport_retries=transport_retries,
            )
        gpus = parsed.gpus
        gpu_message = parsed.gpu_message
        observed_at = utc_now()
        gpus = self._merge_process_sample(
            host,
            gpus,
            process_sampled=process_sampled,
            processes_available=parsed.processes_available,
            sampled_at_monotonic=started,
            observed_at=observed_at,
            workload_mode=config.workloads.mode,
            thresholds=config.thresholds,
        )
        return ProbeResult(
            host=host,
            status="online",
            latency_ms=latency_ms,
            gpus=gpus,
            message=gpu_message,
            observed_at=observed_at,
            system=system,
            transport_retries=transport_retries,
        )
