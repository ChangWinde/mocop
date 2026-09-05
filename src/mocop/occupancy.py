"""Occupancy intervals: pairing a GPU's process transitions into runs.

A run is one process holding one device between two observations by this
monitor. Starts pair with stops; a stop whose start has left the retained
timeline anchors on the first observation it carries; a live process without
a retained start anchors on its first-seen stamp; anything else has no safe
anchor and is counted as dropped rather than guessed. Owner attribution and
same-owner merging live here too, so ``usage.py`` only classifies and sums.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import GpuProcess, epoch_seconds

ProcessKey = tuple[int, str]


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

    @property
    def first_seen_at(self) -> str | None: ...


@dataclass(slots=True)
class Interval:
    """One process's clipped occupancy window on a single GPU."""

    start: float
    end: float
    owner: str | None
    kind: str
    sampled_seconds: float = 0.0
    idle_seconds: float = 0.0


def attribution(workload: dict[str, object] | None) -> tuple[str | None, str]:
    owner = workload.get("owner") if isinstance(workload, dict) else None
    kind = workload.get("kind") if isinstance(workload, dict) else None
    return (
        owner if isinstance(owner, str) and owner else None,
        kind if isinstance(kind, str) and kind else "process",
    )


def pair_intervals(
    events: Sequence[ProcessTransition],
    active_processes: Mapping[ProcessKey, GpuProcess],
    *,
    window_start: float,
    now_epoch: float,
) -> tuple[list[Interval], int, float | None]:
    """Pair start/stop transitions into clipped occupancy intervals.

    Returns the intervals, the count of dropped (unanchorable) records,
    and the earliest event timestamp seen before clipping.
    """
    intervals: list[Interval] = []
    dropped = 0
    earliest: float | None = None
    open_processes: dict[ProcessKey, tuple[float, dict[str, object] | None]] = {}

    def close(start: float, end: float, workload: dict[str, object] | None) -> None:
        clipped_start = max(start, window_start)
        clipped_end = min(end, now_epoch)
        if clipped_end <= clipped_start:
            return
        owner, kind = attribution(workload)
        intervals.append(Interval(clipped_start, clipped_end, owner, kind))

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
        # The matching start has left the retained window. The stop carries
        # the monitor's own first observation of the process on this device,
        # which is a GPU-occupancy observation and anchors the run; a process
        # start time would not be, so a stop without it has no safe anchor.
        anchored_start = epoch_seconds(event.first_seen_at)
        if anchored_start is not None and anchored_start <= observed:
            if earliest is None or anchored_start < earliest:
                earliest = anchored_start
            close(anchored_start, observed, event.workload)
            continue
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


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Return the wall-clock union of one owner's intervals on one GPU."""
    ordered = sorted(intervals, key=lambda item: (item.start, item.end))
    merged: list[Interval] = []
    for interval in ordered:
        if merged and interval.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, interval.end)
            continue
        merged.append(
            Interval(interval.start, interval.end, interval.owner, interval.kind)
        )
    return merged
