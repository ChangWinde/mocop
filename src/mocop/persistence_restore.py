"""Restore of the retained telemetry window from the SQLite history file.

Every partitioned table is keyed ``(host[, gpu_id], observed_at, …)`` without
a rowid, so its primary key is the clustered index. The keys are enumerated
by seeking past the last one and each partition's tail is read through that
same key, which keeps a restore proportional to the retained window rather
than to the whole database. Which incidents were open at shutdown is decided
from each condition's latest transition over the whole retained table.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field

from .incident_types import IncidentCondition, IncidentEvent, OpenIncident
from .persistence_schema import (
    GPU_ROW_FILTER,
    HISTORY_FIELDS,
    HISTORY_ROW_FILTER,
    INCIDENT_ROW_FILTER,
    INCIDENT_SEVERITIES,
    INCIDENT_STATES,
    PROCESS_ROW_FILTER,
    REQUIRED_HISTORY_FIELDS,
)

INTERNAL_USAGE_HOST = "\x00mocop-process-usage-v1"
INTERNAL_USAGE_KEY = "_mocopProcessUsageV1"
# The nine-column process_events contract is frozen for rollback safety, so a
# transition's first observation rides inside workload_json under this key.
FIRST_SEEN_KEY = "_mocopFirstSeenAt"


@dataclass(frozen=True, slots=True)
class LoadedTelemetry:
    history: dict[str, tuple[dict[str, object], ...]]
    incident_events: tuple[IncidentEvent, ...]
    gpu_history: dict[tuple[str, str], tuple[dict[str, object], ...]] = field(
        default_factory=dict
    )
    process_events: dict[tuple[str, str], tuple[dict[str, object], ...]] = field(
        default_factory=dict
    )
    open_incidents: tuple[OpenIncident, ...] = ()


def _partition_keys(
    connection: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> list[tuple[str, ...]]:
    """Distinct text values of a primary-key prefix, one seek per key.

    SQLite orders NULL and numbers before text and blobs after it, so
    starting each walk at ``''`` skips non-text keys and the first blob ends
    it; the row filters would drop those rows anyway.
    """
    prefixes: list[tuple[str, ...]] = [()]
    for depth, column in enumerate(columns):
        fixed = "".join(f"{name} = ? AND " for name in columns[:depth])
        query = (
            f"SELECT {column} FROM {table} WHERE {fixed}{column} > ? "
            f"ORDER BY {column} LIMIT 1"
        )
        expanded: list[tuple[str, ...]] = []
        for prefix in prefixes:
            last = ""
            while row := connection.execute(query, (*prefix, last)).fetchone():
                if not isinstance(row[0], str):
                    break
                last = row[0]
                expanded.append((*prefix, last))
        prefixes = expanded
    return prefixes


def _newest_per_partition(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    query: str,
    limit: int,
) -> list[tuple[object, ...]]:
    """Run a ``… ORDER BY … DESC LIMIT ?`` query per partition key.

    Rows come back oldest first within each key, keys in primary-key order,
    which is the order the in-memory rings are rebuilt in.
    """
    rows: list[tuple[object, ...]] = []
    for key in _partition_keys(connection, table, columns):
        newest = connection.execute(query, (*key, limit)).fetchall()
        rows.extend(reversed(newest))
    return rows


def _is_optional_finite_number(value: object) -> bool:
    return value is None or (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
    )


def restore_telemetry(
    connection: sqlite3.Connection, history_points: int, incident_points: int
) -> LoadedTelemetry:
    """Restore the newest retained points of every host and GPU.

    Every partitioned table is keyed ``(host[, gpu_id], observed_at, …)``
    without a rowid, so its primary key is the clustered index. The keys
    are enumerated by seeking past the last one and each partition's tail
    is read through that same key, which keeps a restore proportional to
    the retained window rather than to the whole database: a window
    function over a 2-million-row GPU table took seconds of full scan and
    temp sort where these seeks take tens of milliseconds.
    """
    history_rows = _newest_per_partition(
        connection,
        "history",
        ("host",),
        f"""
        SELECT host, observed_at, cpu_usage_pct, memory_usage_pct,
               swap_usage_pct, disk_usage_pct, network_rx_bps,
               network_tx_bps, disk_read_bps, disk_write_bps,
               gpu_usage_pct, gpu_memory_usage_pct, gpu_temperature_c,
               transport_retried
        FROM history
        WHERE host = ? AND {HISTORY_ROW_FILTER}
        ORDER BY observed_at DESC LIMIT ?
        """,
        history_points,
    )
    event_rows = connection.execute(
        f"""
        SELECT event_id, host, condition_key, category, resource,
               severity, value, threshold, condition_observed_at,
               detail, group_key, state, observed_at
        FROM (
            SELECT * FROM incident_events
            WHERE {INCIDENT_ROW_FILTER}
            ORDER BY event_id DESC LIMIT ?
        )
        ORDER BY event_id
        """,
        (incident_points,),
    ).fetchall()
    # Which conditions were open at shutdown is decided by each
    # condition's latest transition over the whole retained
    # table, not the event window above: a disk that has been
    # full for a week must not reopen because gpu_memory churn
    # pushed its opened event out of the window.
    open_rows = connection.execute(
        f"""
        SELECT event_id, host, condition_key, category, resource,
               severity, value, threshold, condition_observed_at,
               detail, group_key, state, observed_at
        FROM incident_events
        WHERE event_id IN (
            SELECT MAX(event_id) FROM incident_events
            GROUP BY host, condition_key
        ) AND state != 'resolved' AND {INCIDENT_ROW_FILTER}
        ORDER BY event_id
        """
    ).fetchall()
    opened_rows = connection.execute(
        """
        SELECT host, condition_key, observed_at FROM incident_events
        WHERE event_id IN (
            SELECT MAX(event_id) FROM incident_events
            WHERE state = 'opened' GROUP BY host, condition_key
        )
        """
    ).fetchall()
    gpu_rows = _newest_per_partition(
        connection,
        "gpu_history",
        ("host", "gpu_id"),
        f"""
        SELECT host, gpu_id, gpu_index, observed_at,
               utilization_gpu_pct, memory_used_mib,
               memory_total_mib, temperature_c, power_draw_w
        FROM gpu_history
        WHERE host = ? AND gpu_id = ? AND {GPU_ROW_FILTER}
        ORDER BY observed_at DESC LIMIT ?
        """,
        history_points,
    )
    # Same-timestamp transitions restore with ``stopped`` before
    # ``started``; the selection order is the exact reverse.
    process_rows = _newest_per_partition(
        connection,
        "process_events",
        ("host", "gpu_id"),
        f"""
        SELECT host, gpu_id, gpu_index, observed_at, event_type,
               pid, name, used_memory_mib, workload_json
        FROM process_events AS p
        WHERE p.host = ? AND p.gpu_id = ? AND {PROCESS_ROW_FILTER}
        ORDER BY p.observed_at DESC, p.event_type ASC,
                 p.pid DESC, p.name DESC
        LIMIT ?
        """,
        incident_points,
    )

    history: dict[str, list[dict[str, object]]] = {}
    for row in history_rows:
        host, observed_at, *values, transport_retried = row
        fields = dict(zip(HISTORY_FIELDS, values, strict=True))
        if (
            not isinstance(host, str)
            or not 0 < len(host) <= 253
            or not isinstance(observed_at, str)
            or not 0 < len(observed_at) <= 64
            or transport_retried not in (0, 1)
            or not all(_is_optional_finite_number(value) for value in values)
            or any(fields[field] is None for field in REQUIRED_HISTORY_FIELDS)
        ):
            continue
        point: dict[str, object] = {"observedAt": observed_at}
        point.update(fields)
        point["transportRetried"] = bool(transport_retried)
        history.setdefault(host, []).append(point)

    events = tuple(
        event for row in event_rows if (event := _event_from_row(row)) is not None
    )
    opened_at = {
        (host, key): observed
        for host, key, observed in opened_rows
        if isinstance(host, str) and isinstance(key, str) and isinstance(observed, str)
    }
    open_incidents = tuple(
        OpenIncident(
            event.host,
            event.condition,
            opened_at.get((event.host, event.condition.key), event.observed_at),
        )
        for row in open_rows
        if (event := _event_from_row(row)) is not None
    )
    gpu_history: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in gpu_rows:
        host, gpu_id, gpu_index, observed_at, *values = row
        if (
            not isinstance(host, str)
            or not isinstance(gpu_id, str)
            or not isinstance(gpu_index, int)
            or not isinstance(observed_at, str)
            or not all(_is_optional_finite_number(value) for value in values)
        ):
            continue
        gpu_history.setdefault((host, gpu_id), []).append(
            {
                "observedAt": observed_at,
                "gpuId": gpu_id,
                "index": gpu_index,
                "utilizationGpuPct": values[0],
                "memoryUsedMiB": values[1],
                "memoryTotalMiB": values[2],
                "temperatureC": values[3],
                "powerDrawW": values[4],
            }
        )

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

    restored_process_events = {
        key: tuple(_emission_order(items)[-incident_points:])
        for key, items in process_events.items()
    }

    return LoadedTelemetry(
        history={host: tuple(points) for host, points in history.items()},
        incident_events=events,
        gpu_history={key: tuple(points) for key, points in gpu_history.items()},
        process_events=restored_process_events,
        open_incidents=open_incidents,
    )


def _emission_order(items: list[dict[str, object]]) -> list[dict[str, object]]:
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


def _event_from_row(row: tuple[object, ...]) -> IncidentEvent | None:
    (
        event_id,
        host,
        condition_key,
        category,
        resource,
        severity,
        value,
        threshold,
        condition_observed_at,
        detail,
        group_key,
        state,
        observed_at,
    ) = row
    if (
        not isinstance(event_id, int)
        or event_id < 1
        or not all(
            isinstance(item, str) and 0 < len(item) <= 512
            for item in (
                host,
                condition_key,
                category,
                resource,
                condition_observed_at,
                observed_at,
            )
        )
        or severity not in INCIDENT_SEVERITIES
        or state not in INCIDENT_STATES
        or not _is_optional_finite_number(value)
        or not _is_optional_finite_number(threshold)
        or (detail is not None and (not isinstance(detail, str) or len(detail) > 4096))
        or (
            group_key is not None
            and (not isinstance(group_key, str) or len(group_key) > 512)
        )
    ):
        return None
    return IncidentEvent(
        event_id=event_id,
        host=host,
        condition=IncidentCondition(
            key=condition_key,
            category=category,
            resource=resource,
            severity=severity,
            value=float(value) if value is not None else None,
            threshold=float(threshold) if threshold is not None else None,
            observed_at=condition_observed_at,
            detail=detail,
            group_key=group_key,
        ),
        state=state,
        observed_at=observed_at,
    )
