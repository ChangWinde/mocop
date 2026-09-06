"""Pure arithmetic over GPU process transitions.

The ``StateStore`` owns the process rings and the live process tables under
its lock; this module holds the lock-free pieces it applies to them: building
one transition, deciding whether two samples or a restored transition and a
live sample describe the same process instance, listing the ``started``
transitions a ring leaves open, and closing or reconciling those restored
open transitions against the first live observation after a restart.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .models import GpuProcess
from .telemetry_points import GpuProcessTransition

ProcessKey = tuple[int, str]


def process_transition(
    observed_at: str,
    gpu_id: str,
    gpu_index: int,
    event: str,
    process: GpuProcess,
    *,
    visible: bool = True,
) -> GpuProcessTransition:
    return GpuProcessTransition(
        observed_at=observed_at,
        gpu_id=gpu_id,
        index=gpu_index,
        event=event,
        pid=process.pid,
        name=process.name,
        used_memory_mib=process.used_memory_mib,
        workload=process.workload.to_dict() if process.workload else None,
        visible=visible,
        first_seen_at=process.first_seen_at,
    )


def same_process_instance(previous: GpuProcess, current: GpuProcess) -> bool:
    """False only when both samples carry workload start times that differ.

    The workload's real start time (V7 protocol) distinguishes a new
    process behind a reused PID from a continuously running one. When
    either side lacks it (workloads disabled), the (pid, name) key alone
    keeps identifying the process and first-seen stays a documented
    lower bound.
    """
    previous_started = previous.workload.started_at if previous.workload else None
    current_started = current.workload.started_at if current.workload else None
    if previous_started is None or current_started is None:
        return True
    return previous_started == current_started


def transition_matches_process(
    event: GpuProcessTransition, process: GpuProcess
) -> bool:
    event_start = (
        event.workload.get("started_at") if isinstance(event.workload, dict) else None
    )
    process_start = process.workload.started_at if process.workload else None
    return not event_start or not process_start or event_start == process_start


def open_transitions(
    events: Iterable[GpuProcessTransition],
) -> dict[ProcessKey, GpuProcessTransition]:
    """The ``started`` transitions of a ring that no later ``stopped`` closed."""
    open_events: dict[ProcessKey, GpuProcessTransition] = {}
    for event in events:
        process_key = (event.pid, event.name)
        if event.event == "started":
            open_events[process_key] = event
        else:
            open_events.pop(process_key, None)
    return open_events


def close_restored_start(
    event: GpuProcessTransition, close_at: str, *, visible: bool | None = None
) -> GpuProcessTransition:
    """The ``stopped`` transition that ends a restored open start at ``close_at``.

    A start is by construction the monitor's first observation of the
    process, so it anchors the stop even when the start predates first-seen
    stamps; the stop then still describes the whole run once the start has
    left the retained window.
    """
    return replace(
        event,
        observed_at=close_at,
        event="stopped",
        first_seen_at=event.first_seen_at or event.observed_at,
        visible=event.visible if visible is None else visible,
    )


def reconcile_restored_processes(
    events: Iterable[GpuProcessTransition],
    gpu_id: str,
    gpu_index: int,
    current: dict[ProcessKey, GpuProcess],
    observed_at: str,
    previous_gpu_observed_at: str | None,
) -> tuple[GpuProcessTransition, ...]:
    """Reconcile restored open transitions with the first live sample.

    A restored start whose process is still listed continues its run:
    ``current`` is updated in place so the live process inherits the
    restored first observation. A restored start whose process is gone
    stopped somewhere in the blind spot and closes at the last GPU sample
    before this one; a live process without a restored start begins here.
    """
    open_events = open_transitions(events)
    transitions: list[GpuProcessTransition] = []
    close_at = previous_gpu_observed_at or observed_at
    for process_key, event in sorted(open_events.items()):
        process = current.get(process_key)
        if process is not None and transition_matches_process(event, process):
            current[process_key] = replace(process, first_seen_at=event.observed_at)
            continue
        transitions.append(close_restored_start(event, close_at))
    for process_key, process in sorted(current.items()):
        event = open_events.get(process_key)
        if event is not None and transition_matches_process(event, process):
            continue
        transitions.append(
            process_transition(observed_at, gpu_id, gpu_index, "started", process)
        )
    return tuple(transitions)
