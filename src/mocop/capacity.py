"""Capacity matching: rank same-host, same-model GPU groups against a demand.

This is the server-side twin of ``static/capacity-match.js``. The dashboard
keeps its browser-local copy so the capacity watch can re-evaluate every
accepted snapshot without a request; ``GET /api/capacity`` gives agents and
scripts the same answer from one bounded call. ``tests/fixtures/capacity_match.json``
is the shared contract: both implementations must rank it identically.

Matching never triggers collection and reports observations, not reservations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

HOST_BLOCKERS = frozenset({"connectivity", "gpu_availability", "gpu_count"})
GPU_BLOCKERS = frozenset(
    {"gpu_ecc", "gpu_memory_repair", "gpu_slowdown", "gpu_temperature"}
)
UNKNOWN_MODEL = "Unknown NVIDIA GPU"
ANY_MODEL = "any"
# A group with no available GPU sorts behind every measured group.
_NO_AVAILABLE_UTILIZATION = 101.0


@dataclass(frozen=True, slots=True)
class CapacityRequest:
    gpu_count: int
    min_vram_gib: int
    model: str = ANY_MODEL


def _metric(record: Any, key: str) -> float:
    """Finite number or NaN, mirroring the browser's optionalMetric."""
    value = record.get(key) if isinstance(record, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return math.nan
    return float(value) if math.isfinite(value) else math.nan


def _gpu_has_blocker(gpu: dict[str, Any], conditions: list[dict[str, Any]]) -> bool:
    identity = str(gpu.get("uuid") or gpu.get("index"))
    resource_prefix = f"GPU {gpu.get('index')}"
    for condition in conditions:
        if condition.get("category") not in GPU_BLOCKERS:
            continue
        key = str(condition.get("conditionKey") or "")
        resource = str(condition.get("resource") or "")
        if (
            key.endswith(f":{identity}")
            or resource == resource_prefix
            or resource.startswith(f"{resource_prefix} ")
        ):
            return True
    return False


def _available_gpu(gpu: dict[str, Any]) -> dict[str, object]:
    return {
        "index": gpu.get("index"),
        "uuid": gpu.get("uuid"),
        "freeVramMiB": gpu.get("memory_free_mib"),
        "utilizationPct": gpu.get("utilization_gpu_pct"),
        "temperatureC": gpu.get("temperature_c"),
    }


def match_capacity(
    servers: list[dict[str, Any]],
    active_conditions: list[dict[str, Any]],
    request: CapacityRequest,
    *,
    busy_pct: float,
    temperature_c: float,
) -> dict[str, object]:
    """Rank candidate groups exactly as the dashboard does.

    Stale or offline hosts are skipped silently; maintained hosts and hosts
    with a connectivity/availability/count condition are counted as excluded;
    GPUs with a hardware condition, a busy utilization, too little free VRAM,
    or a temperature at the warning threshold are not available.
    """
    minimum_free_mib = request.min_vram_gib * 1024
    conditions_by_host: dict[str, list[dict[str, Any]]] = {}
    for condition in active_conditions:
        conditions_by_host.setdefault(str(condition.get("host")), []).append(condition)
    candidates: list[dict[str, Any]] = []
    excluded_maintenance = 0
    excluded_health = 0

    for server in servers:
        if server.get("status") != "online" or server.get("stale"):
            continue
        if server.get("maintenance"):
            excluded_maintenance += 1
            continue
        conditions = conditions_by_host.get(str(server.get("host")), [])
        if any(condition.get("category") in HOST_BLOCKERS for condition in conditions):
            excluded_health += 1
            continue
        groups: dict[str, list[dict[str, Any]]] = {}
        for gpu in server.get("gpus", []):
            model = str(gpu.get("name") or UNKNOWN_MODEL)
            if request.model != ANY_MODEL and model != request.model:
                continue
            groups.setdefault(model, []).append(gpu)
        for model, gpus in groups.items():
            available = []
            for gpu in gpus:
                utilization = _metric(gpu, "utilization_gpu_pct")
                free_memory = _metric(gpu, "memory_free_mib")
                temperature = _metric(gpu, "temperature_c")
                if (
                    not math.isnan(utilization)
                    and utilization < busy_pct
                    and not math.isnan(free_memory)
                    and free_memory >= minimum_free_mib
                    and (math.isnan(temperature) or temperature < temperature_c)
                    and not _gpu_has_blocker(gpu, conditions)
                ):
                    available.append(gpu)
            free_values = [_metric(gpu, "memory_free_mib") for gpu in available]
            utilization_values = [
                _metric(gpu, "utilization_gpu_pct") for gpu in available
            ]
            cpu_usage = _metric(server.get("system") or {}, "cpu_usage_pct")
            candidates.append(
                {
                    "host": str(server.get("host")),
                    "model": model,
                    "total": len(gpus),
                    "available": [_available_gpu(gpu) for gpu in available],
                    "satisfies": len(available) >= request.gpu_count,
                    "deficit": max(0, request.gpu_count - len(available)),
                    "minimumFreeMiB": min(free_values) if free_values else 0,
                    "averageUtilization": (
                        sum(utilization_values) / len(utilization_values)
                        if utilization_values
                        else _NO_AVAILABLE_UTILIZATION
                    ),
                    "cpuUsagePct": None if math.isnan(cpu_usage) else cpu_usage,
                }
            )

    candidates.sort(
        key=lambda candidate: (
            not candidate["satisfies"],
            candidate["deficit"],
            -len(candidate["available"]),
            -candidate["minimumFreeMiB"],
            candidate["averageUtilization"],
            candidate["host"],
        )
    )
    return {
        "request": {
            "gpuCount": request.gpu_count,
            "minVramGiB": request.min_vram_gib,
            "model": request.model,
        },
        "candidates": candidates,
        "satisfying": sum(candidate["satisfies"] for candidate in candidates),
        "excludedMaintenance": excluded_maintenance,
        "excludedHealth": excluded_health,
    }
