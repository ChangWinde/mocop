"""GPU hardware-health conditions: ECC, pending memory repair, slowdown.

Derived per device from the health block of a sample, with the policy's
confirmation and recovery parameters; the values are counts or flags, so
they carry no threshold band.
"""

from __future__ import annotations

from .config import IncidentConfig
from .incident_types import IncidentCondition
from .models import GpuMetrics


def gpu_health_conditions(
    gpu: GpuMetrics, identity: str, observed_at: str, incidents: IncidentConfig
) -> dict[str, IncidentCondition]:
    health = gpu.health
    if health is None:
        return {}
    conditions: dict[str, IncidentCondition] = {}
    if (health.ecc_uncorrected_volatile or 0) > 0:
        key = f"gpu_ecc:{identity}"
        conditions[key] = IncidentCondition(
            key=key,
            category="gpu_ecc",
            resource=f"GPU {gpu.index} ECC",
            severity="critical",
            value=float(health.ecc_uncorrected_volatile or 0),
            threshold=0,
            observed_at=observed_at,
            detail="Volatile uncorrected ECC errors detected",
            open_after_cycles=incidents.resource_open_cycles,
            open_after_seconds=incidents.resource_open_seconds,
            recovery_cycles=incidents.recovery_cycles,
        )
    if health.retired_pages_pending or health.remapped_rows_pending:
        key = f"gpu_memory_repair:{identity}"
        conditions[key] = IncidentCondition(
            key=key,
            category="gpu_memory_repair",
            resource=f"GPU {gpu.index} memory",
            severity="critical",
            value=None,
            threshold=None,
            observed_at=observed_at,
            detail="GPU memory repair is pending",
            open_after_cycles=incidents.resource_open_cycles,
            open_after_seconds=incidents.resource_open_seconds,
            recovery_cycles=incidents.recovery_cycles,
        )
    if health.thermal_slowdown or health.power_brake_slowdown:
        key = f"gpu_slowdown:{identity}"
        causes = []
        if health.thermal_slowdown:
            causes.append("thermal")
        if health.power_brake_slowdown:
            causes.append("power brake")
        conditions[key] = IncidentCondition(
            key=key,
            category="gpu_slowdown",
            resource=f"GPU {gpu.index}",
            severity="critical" if health.thermal_slowdown else "warning",
            value=None,
            threshold=None,
            observed_at=observed_at,
            detail=f"Hardware slowdown active: {', '.join(causes)}",
            open_after_cycles=incidents.resource_open_cycles,
            open_after_seconds=incidents.resource_open_seconds,
            recovery_cycles=incidents.recovery_cycles,
        )
    return conditions
