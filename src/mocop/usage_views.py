"""The store's usage views over one consistent copy of its state.

``StateStore.usage_inputs`` copies the timeline, live process tables, and
utilization samples under the lock; these functions aggregate that copy,
alone for the in-memory rollup or together with the history database for the
long-window reports, without holding the store up.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import GpuProcess
from .occupancy import ProcessKey, ProcessTransition
from .persistence_api import TelemetryPersistence
from .reports import usage_report, utilization_report
from .usage import GpuKey, UtilizationSample, aggregate_usage


@dataclass(frozen=True, slots=True)
class UsageInputs:
    now: datetime
    busy_pct: float
    event_cap: int
    events_by_gpu: Mapping[GpuKey, Sequence[ProcessTransition]]
    active_by_gpu: Mapping[GpuKey, Mapping[ProcessKey, GpuProcess]]
    observed_until_by_gpu: Mapping[GpuKey, str]
    utilization_by_gpu: Mapping[GpuKey, Sequence[UtilizationSample]]
    monitored_hosts: frozenset[str]


def memory_usage(
    inputs: UsageInputs, window_hours: int, owner_limit: int
) -> dict[str, object]:
    return aggregate_usage(
        now=inputs.now,
        window_hours=window_hours,
        owner_limit=owner_limit,
        busy_pct=inputs.busy_pct,
        events_by_gpu=inputs.events_by_gpu,
        active_by_gpu=inputs.active_by_gpu,
        utilization_by_gpu=inputs.utilization_by_gpu,
        event_cap=inputs.event_cap,
        observed_until_by_gpu=inputs.observed_until_by_gpu,
    )


def _since_hour(now: datetime, window_hours: int) -> str:
    since = (now - timedelta(hours=window_hours)).replace(
        minute=0, second=0, microsecond=0
    )
    return since.isoformat(timespec="seconds").replace("+00:00", "Z")


def history_usage(
    inputs: UsageInputs,
    persistence: TelemetryPersistence,
    window_hours: int,
    owner_limit: int,
) -> dict[str, object] | None:
    """Owner occupancy over the retained transitions; the live process tables
    extend open runs exactly as the in-memory view does."""
    report_inputs = persistence.report_inputs(_since_hour(inputs.now, window_hours))
    if report_inputs is None:
        return None
    return usage_report(
        now=inputs.now,
        window_hours=window_hours,
        owner_limit=owner_limit,
        busy_pct=inputs.busy_pct,
        inputs=report_inputs,
        active_by_gpu=inputs.active_by_gpu,
        observed_until_by_gpu=inputs.observed_until_by_gpu,
    )


def history_utilization(
    now: datetime,
    persistence: TelemetryPersistence,
    window_hours: int,
    host: str | None,
) -> dict[str, object] | None:
    report_inputs = persistence.report_inputs(_since_hour(now, window_hours))
    if report_inputs is None:
        return None
    return utilization_report(
        now=now, window_hours=window_hours, rows=report_inputs.hourly, host=host
    )
