"""Long-window reports from the history database.

``GET /api/usage`` aggregates the in-memory timeline, which a busy device
fills in a day. These reports read the persisted process transitions (whole
retained window, no per-device cap) and the hourly GPU rollups the writer
maintains, so owner GPU-hours are exact over the raw retention and idle
shares and utilization come at hourly resolution over up to 90 days.
Everything here is a pure computation over rows the persistence layer read.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import GpuProcess, epoch_seconds
from .occupancy import Interval, ProcessKey
from .telemetry_points import GpuProcessTransition
from .usage import GpuKey, aggregate_usage

# One row of gpu_hourly: host, gpu_id, hour, samples, utilization_samples,
# busy_samples, utilization_sum, memory_samples, memory_used_sum,
# memory_total_max.
HourlyRow = tuple[str, str, str, int, int, int, float, int, float, float | None]


@dataclass(frozen=True, slots=True)
class ReportInputs:
    """What the history database contributes to a report."""

    transitions: dict[GpuKey, tuple[dict[str, object], ...]]
    hourly: tuple[HourlyRow, ...]
    retention_hours: int


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _hour_epoch(hour: str) -> float | None:
    return epoch_seconds(hour)


def classify_hourly(
    intervals: list[Interval], buckets: Sequence[tuple[float, int, int]]
) -> None:
    """Fill sampled and idle seconds from hourly buckets.

    ``buckets`` are ``(hour_start_epoch, utilization_samples, busy_samples)``
    sorted by hour. Each bucket's idle share applies uniformly to the part
    of the interval inside it; an hour without utilization samples leaves
    that part unsampled rather than guessing.
    """
    if not buckets or not intervals:
        return
    starts = [bucket[0] for bucket in buckets]
    for interval in intervals:
        position = max(0, bisect_left(starts, interval.start) - 1)
        sampled = idle = 0.0
        while position < len(buckets) and starts[position] < interval.end:
            hour_start, utilization_samples, busy_samples = buckets[position]
            overlap = min(interval.end, hour_start + 3600.0) - max(
                interval.start, hour_start
            )
            if overlap > 0 and utilization_samples > 0:
                sampled += overlap
                idle += overlap * (1.0 - busy_samples / utilization_samples)
            position += 1
        interval.sampled_seconds = sampled
        interval.idle_seconds = idle


def _day_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()


def _split_by_day(owner: str | None, merged: Sequence[Interval], days) -> None:
    for interval in merged:
        cursor = interval.start
        while cursor < interval.end:
            day_start = datetime.fromtimestamp(cursor, tz=timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            day_end = (day_start + timedelta(days=1)).timestamp()
            segment_end = min(interval.end, day_end)
            days[_day_of(cursor)][owner] += segment_end - cursor
            cursor = segment_end


def usage_report(
    *,
    now: datetime,
    window_hours: int,
    owner_limit: int,
    busy_pct: float,
    inputs: ReportInputs,
    active_by_gpu: Mapping[GpuKey, Mapping[ProcessKey, GpuProcess]],
    observed_until_by_gpu: Mapping[GpuKey, str],
) -> dict[str, object]:
    """Per-owner GPU occupancy from persisted transitions and hourly rollups."""
    events_by_gpu = {
        key: [GpuProcessTransition.from_dict(record) for record in records]
        for key, records in inputs.transitions.items()
    }
    buckets: dict[GpuKey, list[tuple[float, int, int]]] = defaultdict(list)
    for (
        host,
        gpu_id,
        hour,
        _samples,
        utilization_samples,
        busy_samples,
        *_rest,
    ) in inputs.hourly:
        epoch = _hour_epoch(hour)
        if epoch is not None:
            buckets[(host, gpu_id)].append((epoch, utilization_samples, busy_samples))
    for series in buckets.values():
        series.sort()
    days: dict[str, dict[str | None, float]] = defaultdict(lambda: defaultdict(float))

    def classifier(intervals: list[Interval], key: GpuKey) -> None:
        classify_hourly(intervals, buckets.get(key, ()))

    def sink(owner: str | None, merged: Sequence[Interval]) -> None:
        _split_by_day(owner, merged, days)

    report = aggregate_usage(
        now=now,
        window_hours=window_hours,
        owner_limit=owner_limit,
        busy_pct=busy_pct,
        events_by_gpu=events_by_gpu,
        active_by_gpu=active_by_gpu,
        utilization_by_gpu={},
        observed_until_by_gpu=observed_until_by_gpu,
        classifier=classifier,
        interval_sink=sink,
        resolution="hour",
    )
    retention_start = now - timedelta(hours=inputs.retention_hours)
    since = now - timedelta(hours=window_hours)
    report["source"] = "history"
    report["retentionHours"] = inputs.retention_hours
    report["coveredFromAt"] = _iso(max(since, retention_start))
    report["days"] = [
        {
            "day": day,
            "gpuSeconds": round(sum(owners.values()), 1),
            "owners": [
                {"owner": owner, "gpuSeconds": round(seconds, 1)}
                for owner, seconds in sorted(
                    owners.items(),
                    key=lambda item: (-item[1], item[0] is None, item[0] or ""),
                )
            ],
        }
        for day, owners in sorted(days.items())
    ]
    return report


def utilization_report(
    *,
    now: datetime,
    window_hours: int,
    rows: Sequence[HourlyRow],
    host: str | None = None,
) -> dict[str, object]:
    """Hourly utilization series per host (or per device of one host)."""
    since = now - timedelta(hours=window_hours)
    since_hour = _iso(since.replace(minute=0, second=0, microsecond=0))
    series: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0, 0, 0.0, 0, 0.0, 0.0])
    )
    fleet: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0, 0.0, 0, 0.0, 0.0])
    fleet_devices: dict[str, set[str]] = defaultdict(set)
    subject_devices: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        (
            row_host,
            gpu_id,
            hour,
            samples,
            utilization_samples,
            busy_samples,
            utilization_sum,
            memory_samples,
            memory_used_sum,
            memory_total_max,
        ) = row
        if hour < since_hour or (host is not None and row_host != host):
            continue
        subject = gpu_id if host is not None else row_host
        for target in (series[subject][hour], fleet[hour]):
            target[0] += samples
            target[1] += utilization_samples
            target[2] += busy_samples
            target[3] += utilization_sum
            target[4] += memory_samples
            target[5] += memory_used_sum
            target[6] += memory_total_max or 0.0
        device = f"{row_host}\x00{gpu_id}"
        fleet_devices[hour].add(device)
        subject_devices[(subject, hour)].add(device)

    def point(hour: str, values: Sequence[float], gpus: int) -> dict[str, object]:
        samples, utilization_samples, busy_samples, utilization_sum = values[:4]
        memory_samples, memory_used_sum, memory_total = values[4:]
        return {
            "hour": hour,
            "gpus": gpus,
            "samples": int(samples),
            "busyShare": (
                round(busy_samples / utilization_samples, 4)
                if utilization_samples
                else None
            ),
            "utilizationAvgPct": (
                round(utilization_sum / utilization_samples, 2)
                if utilization_samples
                else None
            ),
            "memoryUsedAvgMiB": (
                round(memory_used_sum / memory_samples, 1) if memory_samples else None
            ),
            "memoryTotalMiB": round(memory_total, 1) if memory_total else None,
        }

    subjects = [
        {
            "host" if host is None else "gpuId": subject,
            "hours": [
                point(hour, hours[hour], len(subject_devices[(subject, hour)]))
                for hour in sorted(hours)
            ],
        }
        for subject, hours in sorted(series.items())
    ]
    return {
        "generatedAt": _iso(now),
        "sinceAt": _iso(since),
        "windowHours": window_hours,
        "resolution": "hour",
        "host": host,
        "fleet": [
            point(hour, fleet[hour], len(fleet_devices[hour])) for hour in sorted(fleet)
        ],
        ("hosts" if host is None else "gpus"): subjects,
    }
