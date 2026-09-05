"""Per-owner GPU occupancy rollups behind ``GET /api/usage``.

The state store hands over a consistent copy of its process transition
timeline, live process tables, and utilization samples; everything here is a
pure computation over that copy, so the store's lock is never held while the
window is aggregated.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import GpuProcess, epoch_seconds
from .occupancy import (
    Interval,
    ProcessKey,
    ProcessTransition,
    merge_intervals,
    pair_intervals,
)

# Usage is intentionally conservative: a longer sample gap is not classified
# as measured GPU activity.  This bound is independent of the *current* poll
# setting so a later configuration change cannot rewrite historical rollups.
MAX_SAMPLE_GAP_SECONDS = 60.0

GpuKey = tuple[str, str]
# (observed_at, utilization_gpu_pct) per retained history point.
UtilizationSample = tuple[str, float | None]


@dataclass(slots=True)
class _OwnerUsage:
    gpu_seconds: float = 0.0
    sampled_seconds: float = 0.0
    idle_seconds: float = 0.0
    processes: int = 0
    hosts: set[str] = field(default_factory=set)
    gpus: set[GpuKey] = field(default_factory=set)
    kinds: dict[str, int] = field(default_factory=dict)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _classify(
    intervals: list[Interval],
    point_epochs: list[float],
    point_idle: list[bool | None],
) -> None:
    """Split each interval into sampled idle/active seconds.

    Each consecutive utilization sample pair classifies the segment it
    spans. Gaps beyond one minute stay unclassified, independent of later
    poll-setting changes. Prefix sums make each interval query logarithmic.
    """
    if len(point_epochs) < 2 or not intervals:
        return
    # Sorting also makes live behavior match SQLite restoration after an
    # NTP wall-clock correction.  Duplicate timestamps carry no duration.
    samples = sorted(zip(point_epochs, point_idle, strict=True))
    segment_starts: list[float] = []
    segment_ends: list[float] = []
    segment_idle: list[bool] = []
    for position in range(len(samples) - 1):
        segment_start, classification = samples[position]
        segment_end = samples[position + 1][0]
        if (
            classification is None
            or segment_end <= segment_start
            or segment_end - segment_start > MAX_SAMPLE_GAP_SECONDS
        ):
            continue
        segment_starts.append(segment_start)
        segment_ends.append(segment_end)
        segment_idle.append(classification)
    sampled_prefix = [0.0]
    idle_prefix = [0.0]
    for start, end, idle in zip(
        segment_starts, segment_ends, segment_idle, strict=True
    ):
        duration = end - start
        sampled_prefix.append(sampled_prefix[-1] + duration)
        idle_prefix.append(idle_prefix[-1] + (duration if idle else 0.0))
    for interval in intervals:
        start = bisect_right(segment_ends, interval.start)
        end = bisect_left(segment_starts, interval.end)
        if start >= end:
            continue
        sampled = sampled_prefix[end] - sampled_prefix[start]
        idle = idle_prefix[end] - idle_prefix[start]
        left_trim = max(0.0, interval.start - segment_starts[start])
        right_trim = max(0.0, segment_ends[end - 1] - interval.end)
        sampled -= left_trim + right_trim
        if segment_idle[start]:
            idle -= left_trim
        if segment_idle[end - 1]:
            idle -= right_trim
        interval.sampled_seconds = max(0.0, sampled)
        interval.idle_seconds = max(0.0, idle)


def aggregate_usage(
    *,
    now: datetime,
    window_hours: int,
    owner_limit: int,
    busy_pct: float,
    events_by_gpu: Mapping[GpuKey, Sequence[ProcessTransition]],
    active_by_gpu: Mapping[GpuKey, Mapping[ProcessKey, GpuProcess]],
    utilization_by_gpu: Mapping[GpuKey, Sequence[UtilizationSample]],
    event_cap: int | None = None,
) -> dict[str, object]:
    """Aggregate per-owner GPU occupancy over the requested window.

    Occupancy pairs the process start/stop timeline with the live process
    table; idle seconds reclassify occupancy segments whose sampled GPU
    utilization stayed below ``busy_pct``. Coverage is bounded by the
    retained timeline: ``earliestDataAt`` reports how far back the data goes
    overall, and ``partialGpus`` counts the devices whose retained timeline
    is full (``event_cap`` transitions) yet starts inside the window, so
    their occupancy before that point is missing from the totals.
    """
    now_epoch = now.timestamp()
    window_start = now_epoch - window_hours * 3600
    dropped_records = 0
    partial_gpus = 0
    earliest_data: float | None = None
    owners: dict[str | None, _OwnerUsage] = {}

    for key in sorted(set(events_by_gpu) | set(active_by_gpu)):
        events = events_by_gpu.get(key, ())
        intervals, dropped, earliest = pair_intervals(
            events,
            active_by_gpu.get(key, {}),
            window_start=window_start,
            now_epoch=now_epoch,
        )
        dropped_records += dropped
        if earliest is not None:
            earliest_data = (
                earliest if earliest_data is None else min(earliest_data, earliest)
            )
        if (
            event_cap is not None
            and len(events) >= event_cap
            and (earliest is None or earliest > window_start)
        ):
            partial_gpus += 1
        if not intervals:
            continue
        point_epochs: list[float] = []
        point_idle: list[bool | None] = []
        for observed_at, utilization in utilization_by_gpu.get(key, ()):
            epoch = epoch_seconds(observed_at)
            if epoch is None:
                continue
            point_epochs.append(epoch)
            point_idle.append(None if utilization is None else utilization < busy_pct)
        if point_epochs and (earliest_data is None or point_epochs[0] < earliest_data):
            earliest_data = point_epochs[0]
        host = key[0]
        by_owner: dict[str | None, list[Interval]] = {}
        for interval in intervals:
            usage = owners.setdefault(interval.owner, _OwnerUsage())
            usage.processes += 1
            usage.hosts.add(host)
            usage.gpus.add(key)
            usage.kinds[interval.kind] = usage.kinds.get(interval.kind, 0) + 1
            by_owner.setdefault(interval.owner, []).append(interval)
        # Concurrent processes owned by the same principal on one GPU are
        # one device-occupancy interval, not multiple billable GPU-hours.
        for owner, owner_intervals in by_owner.items():
            merged = merge_intervals(owner_intervals)
            _classify(merged, point_epochs, point_idle)
            usage = owners[owner]
            usage.gpu_seconds += sum(item.end - item.start for item in merged)
            usage.sampled_seconds += sum(item.sampled_seconds for item in merged)
            usage.idle_seconds += sum(item.idle_seconds for item in merged)

    ranked = sorted(
        owners.items(),
        key=lambda item: (-item[1].gpu_seconds, item[0] is None, item[0] or ""),
    )
    return {
        "generatedAt": _iso(now),
        "sinceAt": _iso(now - timedelta(hours=window_hours)),
        "windowHours": window_hours,
        "gpuBusyPct": busy_pct,
        "owners": [
            {
                "owner": owner,
                "gpuSeconds": round(usage.gpu_seconds, 1),
                "sampledSeconds": round(usage.sampled_seconds, 1),
                "idleSeconds": round(usage.idle_seconds, 1),
                "idleShare": (
                    round(usage.idle_seconds / usage.sampled_seconds, 4)
                    if usage.sampled_seconds > 0
                    else None
                ),
                "hosts": sorted(usage.hosts),
                "gpus": len(usage.gpus),
                "processes": usage.processes,
                "kinds": dict(sorted(usage.kinds.items())),
            }
            for owner, usage in ranked[:owner_limit]
        ],
        "totalOwners": len(owners),
        "totalGpuSeconds": round(
            sum(usage.gpu_seconds for usage in owners.values()), 1
        ),
        "earliestDataAt": (
            _iso(datetime.fromtimestamp(earliest_data, tz=timezone.utc))
            if earliest_data is not None
            else None
        ),
        "droppedRecords": dropped_records,
        "partialGpus": partial_gpus,
    }
