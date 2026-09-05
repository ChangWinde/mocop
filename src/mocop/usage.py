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
from typing import Protocol

from .models import GpuProcess, epoch_seconds

# Usage is intentionally conservative: a longer sample gap is not classified
# as measured GPU activity.  This bound is independent of the *current* poll
# setting so a later configuration change cannot rewrite historical rollups.
MAX_SAMPLE_GAP_SECONDS = 60.0

GpuKey = tuple[str, str]
ProcessKey = tuple[int, str]
# (observed_at, utilization_gpu_pct) per retained history point.
UtilizationSample = tuple[str, float | None]


class ProcessTransition(Protocol):
    """One ``started``/``stopped`` edge of a GPU process."""

    @property
    def observed_at(self) -> str: ...

    @property
    def event(self) -> str: ...

    @property
    def pid(self) -> int: ...

    @property
    def name(self) -> str: ...

    @property
    def workload(self) -> dict[str, object] | None: ...


@dataclass(slots=True)
class _Interval:
    """One process's clipped occupancy window on a single GPU."""

    start: float
    end: float
    owner: str | None
    kind: str
    sampled_seconds: float = 0.0
    idle_seconds: float = 0.0


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


def _attribution(workload: dict[str, object] | None) -> tuple[str | None, str]:
    owner = workload.get("owner") if isinstance(workload, dict) else None
    kind = workload.get("kind") if isinstance(workload, dict) else None
    return (
        owner if isinstance(owner, str) and owner else None,
        kind if isinstance(kind, str) and kind else "process",
    )


def _intervals(
    events: Sequence[ProcessTransition],
    active_processes: Mapping[ProcessKey, GpuProcess],
    *,
    window_start: float,
    now_epoch: float,
) -> tuple[list[_Interval], int, float | None]:
    """Pair start/stop transitions into clipped occupancy intervals.

    Returns the intervals, the count of dropped (unanchorable) records,
    and the earliest event timestamp seen before clipping.
    """
    intervals: list[_Interval] = []
    dropped = 0
    earliest: float | None = None
    open_processes: dict[ProcessKey, tuple[float, dict[str, object] | None]] = {}

    def close(start: float, end: float, workload: dict[str, object] | None) -> None:
        clipped_start = max(start, window_start)
        clipped_end = min(end, now_epoch)
        if clipped_end <= clipped_start:
            return
        owner, kind = _attribution(workload)
        intervals.append(_Interval(clipped_start, clipped_end, owner, kind))

    for event in events:
        observed = epoch_seconds(event.observed_at)
        if observed is None:
            dropped += 1
            continue
        if earliest is None or observed < earliest:
            earliest = observed
        process_key = (event.pid, event.name)
        if event.event == "started":
            previous = open_processes.pop(process_key, None)
            if previous is not None:
                # A missed stop: the replacement start bounds the old run.
                close(previous[0], observed, previous[1])
            open_processes[process_key] = (observed, event.workload)
            continue
        opened = open_processes.pop(process_key, None)
        if opened is not None:
            close(opened[0], observed, opened[1] or event.workload)
            continue
        # Process start time is not a GPU-occupancy observation.  An
        # unmatched stop therefore has no safe accounting anchor.
        dropped += 1

    for process_key, (started, workload) in open_processes.items():
        # Only the live process table proves that an unmatched start is
        # still occupying the GPU. Collection failures deliberately reset
        # that table without synthesizing stop events; extending such an
        # orphan to ``now`` would turn an observation gap into fabricated
        # billable occupancy.
        if process_key not in active_processes:
            dropped += 1
            continue
        close(started, now_epoch, workload)

    # Processes seeded from the first sample of a GPU never emitted a
    # started transition, so the live process table fills that gap.
    for process_key, process in active_processes.items():
        if process_key in open_processes:
            continue
        workload_dict = process.workload.to_dict() if process.workload else None
        anchored_start = epoch_seconds(process.first_seen_at)
        if anchored_start is None:
            dropped += 1
            continue
        close(anchored_start, now_epoch, workload_dict)

    return intervals, dropped, earliest


def _merged(intervals: list[_Interval]) -> list[_Interval]:
    """Return the wall-clock union of one owner's intervals on one GPU."""
    ordered = sorted(intervals, key=lambda item: (item.start, item.end))
    merged: list[_Interval] = []
    for interval in ordered:
        if merged and interval.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, interval.end)
            continue
        merged.append(
            _Interval(interval.start, interval.end, interval.owner, interval.kind)
        )
    return merged


def _classify(
    intervals: list[_Interval],
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
) -> dict[str, object]:
    """Aggregate per-owner GPU occupancy over the requested window.

    Occupancy pairs the process start/stop timeline with the live process
    table; idle seconds reclassify occupancy segments whose sampled GPU
    utilization stayed below ``busy_pct``. Coverage is bounded by the
    retained timeline, so ``earliestDataAt`` reports how far back the data
    really goes.
    """
    now_epoch = now.timestamp()
    window_start = now_epoch - window_hours * 3600
    dropped_records = 0
    earliest_data: float | None = None
    owners: dict[str | None, _OwnerUsage] = {}

    for key in sorted(set(events_by_gpu) | set(active_by_gpu)):
        intervals, dropped, earliest = _intervals(
            events_by_gpu.get(key, ()),
            active_by_gpu.get(key, {}),
            window_start=window_start,
            now_epoch=now_epoch,
        )
        dropped_records += dropped
        if earliest is not None:
            earliest_data = (
                earliest if earliest_data is None else min(earliest_data, earliest)
            )
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
        by_owner: dict[str | None, list[_Interval]] = {}
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
            merged = _merged(owner_intervals)
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
    }
