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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Protocol

from .config import MonitorConfig, is_safe_alias
from .models import (
    DiskMetrics,
    GpuHealthMetrics,
    GpuMetrics,
    GpuProcess,
    ProbeResult,
    SystemMetrics,
)

_QUERY_FIELDS = (
    "index",
    "uuid",
    "name",
    "driver_version",
    "pstate",
    "temperature.gpu",
    "utilization.gpu",
    "utilization.memory",
    "memory.total",
    "memory.used",
    "memory.free",
    "power.draw",
    "power.limit",
)
_PROCESS_QUERY_FIELDS = ("gpu_uuid", "pid", "process_name", "used_gpu_memory")
_HEALTH_QUERY_FIELDS = (
    "uuid",
    "ecc.errors.uncorrected.volatile.total",
    "retired_pages.pending",
    "remapped_rows.pending",
    "clocks_event_reasons.hw_thermal_slowdown",
    "clocks_event_reasons.hw_power_brake_slowdown",
    "mig.mode.current",
)
_UNAVAILABLE = {"", "n/a", "[n/a]", "not supported", "[not supported]"}
_PROTOCOL_VERSION = "MONITOR_V4"
_PROCESS_READ_CHUNK_BYTES = 65_536
_MAX_GPUS_PER_HOST = 256
_MAX_DISKS_PER_HOST = 1_024
_MAX_PROCESSES_PER_HOST = 4_096

_REMOTE_SCRIPT_TEMPLATE = r"""
LC_ALL=C
export LC_ALL
printf 'MONITOR_V4\n'
host_value=$(hostname 2>/dev/null || printf 'unknown')
host_value=$(printf '%s' "$host_value" | tr '\t\r\n' '   ' | cut -c 1-255)
printf 'HOST\t%s\n' "$host_value"
awk '/^cpu / { total=0; for (i=2; i<=NF; i++) total += $i; idle=$5+$6; printf "CPU\t%.0f\t%.0f\n", total, idle; exit }' /proc/stat
cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || awk '/^processor[[:space:]]*:/ { n++ } END { print n+0 }' /proc/cpuinfo)
printf 'CORES\t%s\n' "$cores"
awk '
  /^MemTotal:/ { mt=$2 }
  /^MemAvailable:/ { ma=$2 }
  /^SwapTotal:/ { st=$2 }
  /^SwapFree:/ { sf=$2 }
  END { printf "MEM\t%.0f\t%.0f\t%.0f\t%.0f\n", mt, ma, st, sf }
' /proc/meminfo
awk '{ printf "LOAD\t%s\t%s\t%s\n", $1, $2, $3 }' /proc/loadavg
awk '{ printf "UPTIME\t%s\n", $1 }' /proc/uptime
awk '
  NR > 2 {
    gsub(/:/, "", $1)
    if ($1 != "lo") { rx += $2; tx += $10 }
  }
  END { printf "NET\t%.0f\t%.0f\n", rx, tx }
' /proc/net/dev
awk '
  FILENAME !~ /\/(loop[0-9]+|ram[0-9]+|zram[0-9]+|dm-[0-9]+|md[0-9]+)\/stat$/ {
    read_bytes += $3 * 512
    write_bytes += $7 * 512
  }
  END { printf "IO\t%.0f\t%.0f\n", read_bytes, write_bytes }
' /sys/block/*/stat 2>/dev/null || printf 'IO\t0\t0\n'
printf 'DISKS_BEGIN\n'
df -PTk 2>/dev/null | awk '
  NR > 1 && $2 !~ /^(tmpfs|devtmpfs|squashfs|overlay|proc|sysfs|cgroup2?|efivarfs|tracefs|debugfs|mqueue|fusectl|securityfs|pstore|configfs|autofs|binfmt_misc|ramfs|nsfs)$/ {
    pct=$6; gsub(/%/, "", pct)
    printf "DISK\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", $1, $2, $3, $4, $5, pct, $7
  }
'
printf 'DISKS_END\n'
printf 'GPUS_BEGIN\n'
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=__GPU_QUERY__ --format=csv,noheader,nounits 2>/dev/null || printf 'GPU_ERROR\t%s\n' "$?"
else
  printf 'GPU_UNAVAILABLE\n'
fi
printf 'GPUS_END\n'
printf 'PROCESSES_BEGIN\n'
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-compute-apps=__PROCESS_QUERY__ --format=csv,noheader,nounits 2>/dev/null || printf 'PROCESS_ERROR\t%s\n' "$?"
fi
printf 'PROCESSES_END\n'
printf 'GPU_HEALTH_BEGIN\n'
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=__HEALTH_QUERY__ --format=csv,noheader,nounits 2>/dev/null || printf 'GPU_HEALTH_ERROR\t%s\n' "$?"
fi
printf 'GPU_HEALTH_END\n'
"""
_REMOTE_SCRIPT = (
    _REMOTE_SCRIPT_TEMPLATE.replace("__GPU_QUERY__", ",".join(_QUERY_FIELDS))
    .replace("__PROCESS_QUERY__", ",".join(_PROCESS_QUERY_FIELDS))
    .replace("__HEALTH_QUERY__", ",".join(_HEALTH_QUERY_FIELDS))
)


class ResourceProbe(Protocol):
    def probe(self, host: str, config: MonitorConfig) -> ProbeResult: ...


# Backward-compatible name for callers that used the first GPU-only interface.
GpuProbe = ResourceProbe
ResourceProbeFactory = Callable[[], ResourceProbe]
_PROBES: dict[str, ResourceProbeFactory] = {}


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    returncode: int
    stdout: str
    stderr: str


class _ProcessOutputLimitExceeded(RuntimeError):
    pass


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
) -> _BoundedProcessResult:
    """Run one SSH process while bounding combined stdout/stderr in memory."""
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
    if stdin is None or stdout is None or stderr is None:
        _kill_process_group(process)
        raise RuntimeError("SSH process pipes were not created")

    try:
        try:
            stdin.write(input_text.encode("utf-8"))
        except BrokenPipeError:
            pass
        finally:
            stdin.close()

        output = {"stdout": bytearray(), "stderr": bytearray()}
        total_bytes = 0
        deadline = time.monotonic() + timeout_seconds
        selector = selectors.DefaultSelector()
        selector.register(stdout, selectors.EVENT_READ, "stdout")
        selector.register(stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                events = selector.select(min(remaining, 0.25))
                for key, _mask in events:
                    chunk = os.read(key.fileobj.fileno(), _PROCESS_READ_CHUNK_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > max_output_bytes:
                        raise _ProcessOutputLimitExceeded
                    output[key.data].extend(chunk)
        except (subprocess.TimeoutExpired, _ProcessOutputLimitExceeded):
            _kill_process_group(process)
            raise
        finally:
            selector.close()

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            returncode = process.poll()
            if returncode is None:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
        else:
            returncode = process.wait(timeout=remaining)
        return _BoundedProcessResult(
            returncode=returncode,
            stdout=output["stdout"].decode("utf-8", errors="replace"),
            stderr=output["stderr"].decode("utf-8", errors="replace"),
        )
    finally:
        if process.poll() is None:
            _kill_process_group(process)
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=1)
        for stream in (stdin, stdout, stderr):
            with suppress(OSError):
                stream.close()


def register_probe(name: str) -> Callable[[ResourceProbeFactory], ResourceProbeFactory]:
    def decorator(factory: ResourceProbeFactory) -> ResourceProbeFactory:
        _PROBES[name] = factory
        return factory

    return decorator


def create_probe(name: str) -> ResourceProbe:
    try:
        return _PROBES[name]()
    except KeyError as exc:
        raise KeyError(
            f"unknown resource probe {name!r}; available: {sorted(_PROBES)}"
        ) from exc


def _number(value: str) -> float | None:
    normalized = value.strip().lower()
    if normalized in _UNAVAILABLE:
        return None
    try:
        result = float(normalized)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


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
    if value.strip().lower() in _UNAVAILABLE:
        return None
    result = _number(value)
    if result is None or result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"nvidia-smi returned an invalid {label}")
    return result


def _bounded_text(value: str, label: str, maximum: int, fallback: str = "") -> str:
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"nvidia-smi returned an oversized {label}")
    return result or fallback


def parse_nvidia_smi_csv(payload: str) -> tuple[GpuMetrics, ...]:
    rows = csv.reader(io.StringIO(payload), skipinitialspace=True)
    gpus: list[GpuMetrics] = []
    for row_number, row in enumerate(rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(gpus) >= _MAX_GPUS_PER_HOST:
            raise ValueError("nvidia-smi returned too many GPU records")
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
        gpus.append(
            GpuMetrics(
                index=int(index),
                uuid=_bounded_text(row[1], "GPU UUID", 128),
                name=_bounded_text(
                    row[2], "GPU name", 256, fallback="Unknown NVIDIA GPU"
                ),
                driver_version=_bounded_text(row[3], "driver version", 64),
                pstate=(
                    None
                    if row[4].strip().lower() in _UNAVAILABLE
                    else _bounded_text(row[4], "performance state", 32)
                ),
                temperature_c=_optional_number(
                    row[5], "GPU temperature", minimum=-100, maximum=250
                ),
                utilization_gpu_pct=_optional_number(
                    row[6], "GPU utilization", maximum=100
                ),
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
                power_draw_w=_optional_number(
                    row[11], "GPU power draw", maximum=1_000_000
                ),
                power_limit_w=_optional_number(
                    row[12], "GPU power limit", maximum=1_000_000
                ),
            )
        )
    return tuple(gpus)


def parse_nvidia_processes_csv(payload: str) -> dict[str, tuple[GpuProcess, ...]]:
    rows = csv.reader(io.StringIO(payload), skipinitialspace=True)
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
        processes.setdefault(gpu_uuid, []).append(process)
        count += 1
    return {gpu_uuid: tuple(items) for gpu_uuid, items in processes.items()}


def _optional_health_boolean(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in _UNAVAILABLE:
        return None
    if normalized in {"yes", "active", "enabled"}:
        return True
    if normalized in {"no", "not active", "disabled"}:
        return False
    raise ValueError("nvidia-smi returned an invalid health boolean")


def parse_nvidia_health_csv(payload: str) -> dict[str, GpuHealthMetrics]:
    rows = csv.reader(io.StringIO(payload), skipinitialspace=True)
    health: dict[str, GpuHealthMetrics] = {}
    for row_number, row in enumerate(rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(health) >= _MAX_GPUS_PER_HOST:
            raise ValueError("nvidia-smi returned too many GPU health records")
        if len(row) != len(_HEALTH_QUERY_FIELDS):
            raise ValueError(
                f"nvidia-smi returned {len(row)} health columns on row {row_number}; "
                f"expected {len(_HEALTH_QUERY_FIELDS)}"
            )
        gpu_uuid = _bounded_text(row[0], "health GPU UUID", 128)
        if gpu_uuid in health:
            raise ValueError("nvidia-smi returned duplicate GPU health records")
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
        health[gpu_uuid] = GpuHealthMetrics(
            ecc_uncorrected_volatile=int(ecc_value) if ecc_value is not None else None,
            retired_pages_pending=_optional_health_boolean(row[2]),
            remapped_rows_pending=_optional_health_boolean(row[3]),
            thermal_slowdown=_optional_health_boolean(row[4]),
            power_brake_slowdown=_optional_health_boolean(row[5]),
            mig_mode=mig_value,
        )
    return health


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


def parse_linux_resource_payload(
    payload: str,
) -> tuple[_RawSystemSample, tuple[GpuMetrics, ...], str | None]:
    lines = payload.splitlines()
    if not lines or lines[0].strip() != _PROTOCOL_VERSION:
        raise ValueError("resource payload has an unknown protocol version")

    values: dict[str, list[str]] = {}
    disks: list[DiskMetrics] = []
    gpu_lines: list[str] = []
    process_lines: list[str] = []
    health_lines: list[str] = []
    gpu_message: str | None = None
    processes_available = True
    health_available = True
    in_gpus = False
    in_disks = False
    in_processes = False
    in_health = False
    section_markers: set[str] = set()

    for line in lines[1:]:
        if line == "GPUS_BEGIN":
            if (
                in_gpus
                or in_disks
                or in_processes
                or in_health
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
        if line == "GPU_HEALTH_BEGIN":
            if (
                in_gpus
                or in_disks
                or in_processes
                or in_health
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
            if line.startswith("PROCESS_ERROR\t"):
                processes_available = False
            elif line.strip():
                process_lines.append(line)
            continue
        if in_health:
            if line.startswith("GPU_HEALTH_ERROR\t"):
                health_available = False
                health_lines.clear()
            elif line.strip() and health_available:
                health_lines.append(line)
            continue

        parts = line.split("\t")
        if not parts:
            continue
        if in_disks and parts[0] == "DISK" and len(parts) == 8:
            if len(disks) >= _MAX_DISKS_PER_HOST:
                raise ValueError("resource payload has too many disk records")
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
        elif len(parts) >= 2:
            values[parts[0]] = parts[1:]

    expected_markers = {
        "DISKS_BEGIN",
        "DISKS_END",
        "GPUS_BEGIN",
        "GPUS_END",
        "PROCESSES_BEGIN",
        "PROCESSES_END",
        "GPU_HEALTH_BEGIN",
        "GPU_HEALTH_END",
    }
    if (
        section_markers != expected_markers
        or in_disks
        or in_gpus
        or in_processes
        or in_health
    ):
        raise ValueError("resource payload has incomplete metric sections")
    if gpu_message is not None and gpu_lines:
        raise ValueError("resource payload has conflicting GPU status")

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
    )
    gpus = parse_nvidia_smi_csv("\n".join(gpu_lines))
    processes = (
        parse_nvidia_processes_csv("\n".join(process_lines))
        if processes_available
        else {}
    )
    health: dict[str, GpuHealthMetrics] = {}
    if health_available:
        try:
            health = parse_nvidia_health_csv("\n".join(health_lines))
        except ValueError:
            # Health is additive; an unsupported field must not hide base metrics.
            health = {}
    gpus = tuple(
        replace(
            gpu,
            processes=processes.get(gpu.uuid, ()),
            processes_available=processes_available,
            health=health.get(gpu.uuid),
        )
        for gpu in gpus
    )
    return raw, gpus, gpu_message


@dataclass(frozen=True, slots=True)
class _Baseline:
    observed_monotonic: float
    cpu_total_ticks: float
    cpu_idle_ticks: float
    network_rx_bytes: float
    network_tx_bytes: float
    disk_read_bytes: float
    disk_write_bytes: float


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
    )
    for needles, message in categories:
        if any(needle in normalized for needle in needles):
            return message
    return "SSH connection failed"


@register_probe("openssh-linux-v4")
class OpenSshLinuxResourceProbe:
    """Collect Linux and NVIDIA metrics locally or through one fixed SSH script."""

    def __init__(self) -> None:
        self._baseline_lock = threading.Lock()
        self._baselines: dict[str, _Baseline] = {}

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
        with self._baseline_lock:
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
        )

    def probe(self, host: str, config: MonitorConfig) -> ProbeResult:
        if not is_safe_alias(host):
            raise ValueError(f"unsafe SSH alias: {host!r}")

        local = config.local_host == host
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
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            completed = _run_bounded_process(
                command,
                input_text=_REMOTE_SCRIPT,
                timeout_seconds=config.probe_timeout_seconds,
                max_output_bytes=config.max_output_bytes,
                environment=environment,
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(
                host=host,
                status="unreachable",
                latency_ms=round((time.monotonic() - started) * 1000),
                message=(
                    "Local resource collection timed out"
                    if local
                    else "SSH/resource collection timed out"
                ),
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
            )

        observed_monotonic = time.monotonic()
        latency_ms = round((observed_monotonic - started) * 1000)
        if not local and completed.returncode == 255:
            return ProbeResult(
                host=host,
                status="unreachable",
                latency_ms=latency_ms,
                message=_safe_ssh_failure(completed.stderr),
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
            )
        try:
            raw, gpus, gpu_message = parse_linux_resource_payload(completed.stdout)
            system = self._system_metrics(host, raw, observed_monotonic)
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
            )
        return ProbeResult(
            host=host,
            status="online",
            latency_ms=latency_ms,
            gpus=gpus,
            message=gpu_message,
            system=system,
        )


# The old class and registry name remain import-compatible for one release.
OpenSshNvidiaSmiProbe = OpenSshLinuxResourceProbe
_PROBES["openssh-linux-v2"] = OpenSshLinuxResourceProbe
_PROBES["openssh-linux-v1"] = OpenSshLinuxResourceProbe
_PROBES["openssh-nvidia-smi"] = OpenSshLinuxResourceProbe
_PROBES["openssh-linux-v3"] = OpenSshLinuxResourceProbe
