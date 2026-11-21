"""Field parsing for the fixed collection protocol's NVIDIA sections.

Numeric and text field validation shared with the payload parser, and the
nvidia-smi GPU, compute-process, health, and combined CSV parsers. Every
parser bounds its input and raises ``ValueError`` on anything malformed, so
``probe.py`` keeps only section framing, system metrics, and transport.
"""

from __future__ import annotations

import csv
import io
import math

from .models import GpuHealthMetrics, GpuMetrics, GpuProcess
from .remote_script import (
    _COMBINED_QUERY_FIELDS,
    _HEALTH_QUERY_FIELDS,
    _PROCESS_QUERY_FIELDS,
    _QUERY_FIELDS,
)

_UNAVAILABLE = {"", "n/a", "[n/a]", "not supported", "[not supported]"}
_MAX_GPUS_PER_HOST = 256
_MAX_PROCESSES_PER_HOST = 4_096


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
        gpu = _parse_nvidia_smi_row(row, row_number)
        if any(existing.index == gpu.index for existing in gpus):
            raise ValueError("nvidia-smi returned duplicate GPU indices")
        gpus.append(gpu)
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
        gpu = _parse_nvidia_smi_row(row[: len(_QUERY_FIELDS)], row_number)
        if any(existing.index == gpu.index for existing in gpus):
            raise ValueError("nvidia-smi returned duplicate GPU indices")
        gpus.append(gpu)
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
