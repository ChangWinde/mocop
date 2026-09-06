"""Persisted process transitions: decoding rows and restoring their order.

Hidden transitions (seeds and closes that the dashboard never lists) are
stored under a pseudo-host with the real device inside ``workload_json``, a
transition's first observation rides in the same column under a sidecar key
because the table's nine columns are frozen for rollback safety, and a
``started`` and ``stopped`` of one process in the same second are put back in
the order the collector emitted them.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence

INTERNAL_USAGE_HOST = "\x00mocop-process-usage-v1"
INTERNAL_USAGE_KEY = "_mocopProcessUsageV1"
# The nine-column process_events contract is frozen for rollback safety, so a
# transition's first observation rides inside workload_json under this key.
FIRST_SEEN_KEY = "_mocopFirstSeenAt"


def _is_optional_finite_number(value: object) -> bool:
    return value is None or (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
    )


def decode_process_rows(
    process_rows: Sequence[tuple[object, ...]],
) -> dict[tuple[str, str], list[dict[str, object]]]:
    """Decode persisted process transitions, mapping hidden rows back to
    their real device and stripping the first-observation sidecar."""
    process_events: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in process_rows:
        (
            host,
            gpu_id,
            gpu_index,
            observed_at,
            event_type,
            pid,
            name,
            memory,
            raw_workload,
        ) = row
        if (
            not isinstance(host, str)
            or not isinstance(gpu_id, str)
            or not isinstance(gpu_index, int)
            or not isinstance(observed_at, str)
            or event_type not in {"started", "stopped"}
            or not isinstance(pid, int)
            or not isinstance(name, str)
            or not _is_optional_finite_number(memory)
        ):
            continue
        workload = None
        parsed: object = None
        if isinstance(raw_workload, str) and len(raw_workload) <= 16_384:
            try:
                parsed = json.loads(raw_workload)
            except json.JSONDecodeError:
                parsed = None
        first_seen_at: object = None
        visible = host != INTERNAL_USAGE_HOST
        if not visible:
            marker = (
                parsed.get(INTERNAL_USAGE_KEY) if isinstance(parsed, dict) else None
            )
            if not isinstance(marker, dict):
                continue
            restored_host = marker.get("host")
            restored_gpu_id = marker.get("gpuId")
            restored_workload = marker.get("workload")
            first_seen_at = marker.get("firstSeenAt")
            if (
                not isinstance(restored_host, str)
                or not 0 < len(restored_host) <= 253
                or "\x00" in restored_host
                or not isinstance(restored_gpu_id, str)
                or not 0 < len(restored_gpu_id) <= 512
                or (
                    restored_workload is not None
                    and not isinstance(restored_workload, dict)
                )
            ):
                continue
            host = restored_host
            gpu_id = restored_gpu_id
            workload = restored_workload
        elif isinstance(parsed, dict) and len(raw_workload) <= 4096:
            first_seen_at = parsed.pop(FIRST_SEEN_KEY, None)
            workload = parsed or None
        if not (isinstance(first_seen_at, str) and 0 < len(first_seen_at) <= 64):
            first_seen_at = None
        process_events.setdefault((host, gpu_id), []).append(
            {
                "observedAt": observed_at,
                "gpuId": gpu_id,
                "index": gpu_index,
                "event": event_type,
                "pid": pid,
                "name": name,
                "usedMemoryMiB": memory,
                "workload": workload,
                "firstSeenAt": first_seen_at,
                **({"_visible": False} if not visible else {}),
            }
        )

    return process_events


def emission_order(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Order one device's transitions as the collector emitted them.

    The table has no sequence column, so a ``started`` and a ``stopped`` of
    one process in the same second are ambiguous. The collector emits
    stop-then-start only for a PID reuse, which by construction carries two
    different workload start times; every other such pair (a process seeded
    and closed by the same sample) is a zero-length run emitted
    start-then-stop, and restoring it stop-first left a phantom open start.
    """

    def workload_started_at(item: dict[str, object]) -> object:
        workload = item.get("workload")
        return workload.get("started_at") if isinstance(workload, dict) else None

    def identity(item: dict[str, object], event: object) -> tuple[object, ...]:
        return (item["observedAt"], item["pid"], item["name"], event)

    started_at = {
        identity(item, item["event"]): workload_started_at(item) for item in items
    }

    def rank(item: dict[str, object]) -> tuple[object, ...]:
        stopped = item["event"] == "stopped"
        own = workload_started_at(item)
        twin = started_at.get(identity(item, "started" if stopped else "stopped"))
        pid_reuse = bool(own and twin and own != twin)
        return (item["observedAt"], item["pid"], item["name"], stopped != pid_reuse)

    return sorted(items, key=rank)
