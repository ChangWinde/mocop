"""Transactional record writes and their immutable queue payloads."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .incident_types import IncidentEvent
from .persistence_gpu import write_gpu_points
from .persistence_schema import HISTORY_FIELDS
from .persistence_transitions import (
    FIRST_SEEN_KEY,
    INTERNAL_USAGE_HOST,
    INTERNAL_USAGE_KEY,
)


@dataclass(frozen=True, slots=True)
class _HistoryWrite:
    host: str
    point: dict[str, object]


@dataclass(frozen=True, slots=True)
class _IncidentWrite:
    event: IncidentEvent


@dataclass(frozen=True, slots=True)
class _GpuTelemetryWrite:
    host: str
    points: tuple[dict[str, object], ...]
    process_events: tuple[dict[str, object], ...]


_Write = _HistoryWrite | _IncidentWrite | _GpuTelemetryWrite


def _write(connection: sqlite3.Connection, item: _Write, busy_pct: float | None) -> int:
    if isinstance(item, _HistoryWrite):
        point = item.point
        cursor = connection.execute(
            """
            INSERT OR REPLACE INTO history VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                item.host,
                point.get("observedAt"),
                *(point.get(field) for field in HISTORY_FIELDS),
                1 if point.get("transportRetried") else 0,
            ),
        )
        return max(0, cursor.rowcount)

    if isinstance(item, _GpuTelemetryWrite):
        return _write_gpu_telemetry(connection, item, busy_pct)

    assert isinstance(item, _IncidentWrite)
    event = item.event
    condition = event.condition
    # OR REPLACE lets a restarted tracker reclaim event ids still held by
    # corrupt rows; valid rows are never hit because trackers restart from
    # the highest restorable id.
    cursor = connection.execute(
        """
        INSERT OR REPLACE INTO incident_events VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            event.event_id,
            event.host,
            condition.key,
            condition.category,
            condition.resource,
            condition.severity,
            condition.value,
            condition.threshold,
            condition.observed_at,
            condition.detail,
            condition.group_key,
            event.state,
            event.observed_at,
        ),
    )
    connection.execute(
        "INSERT OR REPLACE INTO incident_criteria VALUES (?, ?)",
        (event.event_id, condition.critical_at),
    )
    return max(0, cursor.rowcount)


def _write_gpu_telemetry(
    connection: sqlite3.Connection,
    item: _GpuTelemetryWrite,
    busy_pct: float | None,
) -> int:
    written_records = 0
    if item.points:
        written_records += write_gpu_points(
            connection, item.host, item.points, busy_pct
        )
    if item.process_events:
        visible_events = tuple(
            event for event in item.process_events if event.get("_visible") is not False
        )
        cursor = connection.executemany(
            """
            INSERT OR IGNORE INTO process_events (
                host, gpu_id, gpu_index, observed_at, event_type,
                pid, name, used_memory_mib, workload_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                (
                    item.host,
                    event.get("gpuId"),
                    event.get("index"),
                    event.get("observedAt"),
                    event.get("event"),
                    event.get("pid"),
                    event.get("name"),
                    event.get("usedMemoryMiB"),
                    _serialize_workload(event),
                )
                for event in visible_events
            ),
        )
        written_records += max(0, cursor.rowcount)
        hidden_events = tuple(
            event for event in item.process_events if event.get("_visible") is False
        )
        hidden_cursor = connection.executemany(
            """
            INSERT OR IGNORE INTO process_events (
                host, gpu_id, gpu_index, observed_at, event_type,
                pid, name, used_memory_mib, workload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    INTERNAL_USAGE_HOST,
                    _internal_usage_gpu_key(item.host, event),
                    event.get("index"),
                    event.get("observedAt"),
                    event.get("event"),
                    event.get("pid"),
                    event.get("name"),
                    event.get("usedMemoryMiB"),
                    _serialize_hidden_usage(item.host, event),
                )
                for event in hidden_events
            ),
        )
        written_records += max(0, hidden_cursor.rowcount)
    return written_records


def _internal_usage_gpu_key(host: str, event: dict[str, object]) -> str:
    identity = f"{host}\x00{event.get('gpuId')}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(identity).hexdigest()


def _serialize_hidden_usage(host: str, event: dict[str, object]) -> str:
    return json.dumps(
        {
            INTERNAL_USAGE_KEY: {
                "host": host,
                "gpuId": event.get("gpuId"),
                "workload": event.get("workload")
                if isinstance(event.get("workload"), dict)
                else None,
                "firstSeenAt": event.get("firstSeenAt")
                if isinstance(event.get("firstSeenAt"), str)
                else None,
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _serialize_workload(event: dict[str, object]) -> str | None:
    workload = event.get("workload")
    payload = dict(workload) if isinstance(workload, dict) else {}
    first_seen_at = event.get("firstSeenAt")
    if isinstance(first_seen_at, str):
        payload[FIRST_SEEN_KEY] = first_seen_at
    if not payload:
        return None
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
