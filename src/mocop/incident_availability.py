"""Conditions that say a telemetry source on an online host is unavailable.

A failed or stuck nvidia-smi and a df stuck on a hung filesystem leave the
sample without GPU or disk data. Each opens one critical condition that names
the cause, and the matching telemetry domain stays unobserved, so the
resource conditions that depend on it freeze instead of recovering.
"""

from __future__ import annotations

from .incident_types import IncidentCondition
from .models import ProbeResult

# Every probe message that means "this sample cannot speak for the GPUs" must
# be listed here. A malformed nvidia-smi payload degrades to zero GPUs with
# this message; omitting it would let the empty inventory look authoritative
# and, via the absence-recovery rule, silently resolve every per-GPU hardware
# incident without opening an availability alert.
GPU_QUERY_FAILURE_MESSAGES = frozenset(
    {
        "nvidia-smi is unavailable",
        "nvidia-smi query failed",
        "nvidia-smi output was malformed",
        "nvidia-smi is stuck in the NVIDIA driver",
    }
)
HUNG_FILESYSTEM_DETAIL = "df is stuck on a hung filesystem"


def availability_conditions(
    result: ProbeResult, recovery_cycles: int
) -> dict[str, IncidentCondition]:
    """The availability conditions of an online sample, keyed by condition key."""
    conditions: dict[str, IncidentCondition] = {}
    if result.message in GPU_QUERY_FAILURE_MESSAGES:
        conditions["gpu_availability"] = IncidentCondition(
            key="gpu_availability",
            category="gpu_availability",
            resource="NVIDIA telemetry",
            severity="critical",
            value=None,
            threshold=None,
            observed_at=result.observed_at,
            detail=result.message,
            open_after_cycles=1,
            recovery_cycles=recovery_cycles,
        )
    if result.system is not None and result.system.disks_hung:
        conditions["disk_availability"] = IncidentCondition(
            key="disk_availability",
            category="disk_availability",
            resource="Filesystems",
            severity="critical",
            value=float(result.system.disks_hung),
            threshold=None,
            observed_at=result.observed_at,
            detail=HUNG_FILESYSTEM_DETAIL,
            open_after_cycles=1,
            recovery_cycles=recovery_cycles,
        )
    return conditions
