from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .config import PersistenceConfig
from .incidents import IncidentCondition, IncidentEvent

# Keep the released v3 process_events table byte-for-byte compatible.  Older
# writers use positional INSERTs, so even an appended nullable column would
# break package rollback.  Same-timestamp transitions are ordered
# deterministically at read time, with ``stopped`` before ``started``.
_SCHEMA_VERSION = 3
_QUEUE_CAPACITY = 4096
_WRITE_BATCH_SIZE = 128
_PRUNE_INTERVAL_SECONDS = 60.0
_SQLITE_FULL_ERRORCODE = 13  # sqlite3.SQLITE_FULL is unavailable on Python 3.10
_HISTORY_FIELDS = (
    "cpuUsagePct",
    "memoryUsagePct",
    "swapUsagePct",
    "diskUsagePct",
    "networkRxBps",
    "networkTxBps",
    "diskReadBps",
    "diskWriteBps",
    "gpuUsagePct",
    "gpuMemoryUsagePct",
    "gpuTemperatureC",
)
_INCIDENT_STATES = frozenset({"opened", "resolved", "escalated", "deescalated"})
_INCIDENT_SEVERITIES = frozenset({"warning", "critical"})
_INTERNAL_USAGE_HOST = "\x00mocop-process-usage-v1"
_INTERNAL_USAGE_KEY = "_mocopProcessUsageV1"

# Rows whose stored types cannot round-trip are excluded in SQL before any
# restore limit applies, so corrupt rows never displace older valid records.
_NUMERIC_COLUMN_TYPES = "('integer', 'real', 'null')"
# The in-memory history point requires these three percentages, so a NULL
# written by a foreign or corrupted database must not survive the restore:
# it would crash host initialization on every service start.
_REQUIRED_NUMERIC_COLUMN_TYPES = "('integer', 'real')"
_REQUIRED_HISTORY_COLUMNS = frozenset(
    {"memory_usage_pct", "swap_usage_pct", "disk_usage_pct"}
)
_REQUIRED_HISTORY_FIELDS = frozenset({"memoryUsagePct", "swapUsagePct", "diskUsagePct"})
_HISTORY_ROW_FILTER = " AND ".join(
    (
        "typeof(host) = 'text'",
        "typeof(observed_at) = 'text'",
        *(
            f"typeof({column}) IN "
            + (
                _REQUIRED_NUMERIC_COLUMN_TYPES
                if column in _REQUIRED_HISTORY_COLUMNS
                else _NUMERIC_COLUMN_TYPES
            )
            for column in (
                "cpu_usage_pct",
                "memory_usage_pct",
                "swap_usage_pct",
                "disk_usage_pct",
                "network_rx_bps",
                "network_tx_bps",
                "disk_read_bps",
                "disk_write_bps",
                "gpu_usage_pct",
                "gpu_memory_usage_pct",
                "gpu_temperature_c",
            )
        ),
        "transport_retried IN (0, 1)",
    )
)
_INCIDENT_ROW_FILTER = " AND ".join(
    (
        "typeof(event_id) = 'integer'",
        "event_id >= 1",
        "typeof(host) = 'text'",
        "typeof(condition_key) = 'text'",
        "typeof(category) = 'text'",
        "typeof(resource) = 'text'",
        f"typeof(value) IN {_NUMERIC_COLUMN_TYPES}",
        f"typeof(threshold) IN {_NUMERIC_COLUMN_TYPES}",
        "typeof(condition_observed_at) = 'text'",
        "typeof(detail) IN ('text', 'null')",
        "typeof(group_key) IN ('text', 'null')",
        "typeof(observed_at) = 'text'",
    )
)
_GPU_ROW_FILTER = " AND ".join(
    (
        "typeof(host) = 'text'",
        "typeof(gpu_id) = 'text'",
        "typeof(gpu_index) = 'integer'",
        "typeof(observed_at) = 'text'",
        *(
            f"typeof({column}) IN {_NUMERIC_COLUMN_TYPES}"
            for column in (
                "utilization_gpu_pct",
                "memory_used_mib",
                "memory_total_mib",
                "temperature_c",
                "power_draw_w",
            )
        ),
    )
)
_PROCESS_ROW_FILTER = " AND ".join(
    (
        "typeof(p.host) = 'text'",
        "typeof(p.gpu_id) = 'text'",
        "typeof(p.gpu_index) = 'integer'",
        "typeof(p.observed_at) = 'text'",
        "p.event_type IN ('started', 'stopped')",
        "typeof(p.pid) = 'integer'",
        "typeof(p.name) = 'text'",
        f"typeof(p.used_memory_mib) IN {_NUMERIC_COLUMN_TYPES}",
        "typeof(p.workload_json) IN ('text', 'null')",
    )
)

_GPU_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS gpu_history (
        host TEXT NOT NULL,
        gpu_id TEXT NOT NULL,
        gpu_index INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        utilization_gpu_pct REAL,
        memory_used_mib REAL,
        memory_total_mib REAL,
        temperature_c REAL,
        power_draw_w REAL,
        PRIMARY KEY (host, gpu_id, observed_at)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS gpu_history_observed_at ON gpu_history(observed_at)",
    """
    CREATE TABLE IF NOT EXISTS process_events (
        host TEXT NOT NULL,
        gpu_id TEXT NOT NULL,
        gpu_index INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN ('started', 'stopped')),
        pid INTEGER NOT NULL,
        name TEXT NOT NULL,
        used_memory_mib REAL,
        workload_json TEXT,
        PRIMARY KEY (
            host, gpu_id, observed_at, event_type, pid, name
        )
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS process_events_observed_at"
    " ON process_events(observed_at)",
)
_CREATE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS history (
        host TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        cpu_usage_pct REAL,
        memory_usage_pct REAL,
        swap_usage_pct REAL,
        disk_usage_pct REAL,
        network_rx_bps REAL,
        network_tx_bps REAL,
        disk_read_bps REAL,
        disk_write_bps REAL,
        gpu_usage_pct REAL,
        gpu_memory_usage_pct REAL,
        gpu_temperature_c REAL,
        transport_retried INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (host, observed_at)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS history_observed_at ON history(observed_at)",
    """
    CREATE TABLE IF NOT EXISTS incident_events (
        event_id INTEGER PRIMARY KEY,
        host TEXT NOT NULL,
        condition_key TEXT NOT NULL,
        category TEXT NOT NULL,
        resource TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
        value REAL,
        threshold REAL,
        condition_observed_at TEXT NOT NULL,
        detail TEXT,
        group_key TEXT,
        state TEXT NOT NULL CHECK (
            state IN ('opened', 'resolved', 'escalated', 'deescalated')
        ),
        observed_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS incident_events_observed_at"
    " ON incident_events(observed_at)",
) + _GPU_TABLE_STATEMENTS


def _is_optional_finite_number(value: object) -> bool:
    return value is None or (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
    )


def _is_size_error(exc: sqlite3.Error) -> bool:
    if getattr(exc, "sqlite_errorcode", None) == _SQLITE_FULL_ERRORCODE:
        return True
    return "full" in str(exc).lower()


class PersistenceError(RuntimeError):
    """Raised when explicitly enabled persistence cannot start safely."""


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


class TelemetryPersistence(Protocol):
    def is_enabled(self) -> bool: ...

    def load(self, history_points: int, incident_points: int) -> LoadedTelemetry: ...

    def record_history(self, host: str, point: dict[str, object]) -> None: ...

    def record_incidents(self, events: tuple[IncidentEvent, ...]) -> None: ...

    def record_gpu_telemetry(
        self,
        host: str,
        points: tuple[dict[str, object], ...],
        process_events: tuple[dict[str, object], ...],
    ) -> None: ...

    def status(self) -> dict[str, object]: ...

    def close(self, timeout_seconds: float = 5.0) -> None: ...


class DisabledPersistence:
    def is_enabled(self) -> bool:
        return False

    def load(self, history_points: int, incident_points: int) -> LoadedTelemetry:
        del history_points, incident_points
        return LoadedTelemetry({}, ())

    def record_history(self, host: str, point: dict[str, object]) -> None:
        del host, point

    def record_incidents(self, events: tuple[IncidentEvent, ...]) -> None:
        del events

    def record_gpu_telemetry(
        self,
        host: str,
        points: tuple[dict[str, object], ...],
        process_events: tuple[dict[str, object], ...],
    ) -> None:
        del host, points, process_events

    def status(self) -> dict[str, object]:
        return {
            "enabled": False,
            "backend": "memory",
            "healthy": True,
            "queuedWrites": 0,
            "droppedWrites": 0,
            "lastError": None,
        }

    def close(self, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds


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


@dataclass(slots=True)
class _Flush:
    """Write barrier that reports whether preceding writes were committed."""

    completed: threading.Event
    committed: bool = True


_Write = _HistoryWrite | _IncidentWrite | _GpuTelemetryWrite
_QueueItem = _Write | _Flush


class SqliteTelemetryPersistence:
    """Bounded asynchronous SQLite history storage.

    Collection threads only perform a non-blocking queue insertion. The dedicated
    writer owns its SQLite connection, batches commits, and contains disk failures.
    """

    def __init__(self, config: PersistenceConfig, path: Path) -> None:
        if not config.enabled:
            raise ValueError("SQLite persistence requires enabled configuration")
        self._config = config
        self._path = path.expanduser().absolute()
        self._queue: queue.Queue[_QueueItem] = queue.Queue(_QUEUE_CAPACITY)
        # Serializes producer admission with close.  Without this boundary a
        # producer could observe ``_closed == False``, lose the CPU to close,
        # and enqueue after the writer had already exited.
        self._admission_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._closed = False
        self._stop_requested = threading.Event()
        self._dropped_writes = 0
        self._written_records = 0
        self._last_error: str | None = None
        self._prepare_database()
        self._writer = threading.Thread(
            target=self._write_loop,
            name="mocop-history-writer",
            daemon=True,
        )
        self._writer.start()

    def is_enabled(self) -> bool:
        return True

    def load(self, history_points: int, incident_points: int) -> LoadedTelemetry:
        try:
            with closing(self._connect()) as connection:
                history_rows = connection.execute(
                    f"""
                    SELECT host, observed_at, cpu_usage_pct, memory_usage_pct,
                           swap_usage_pct, disk_usage_pct, network_rx_bps,
                           network_tx_bps, disk_read_bps, disk_write_bps,
                           gpu_usage_pct, gpu_memory_usage_pct, gpu_temperature_c,
                           transport_retried
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY host ORDER BY observed_at DESC
                        ) AS position
                        FROM history
                        WHERE {_HISTORY_ROW_FILTER}
                    )
                    WHERE position <= ?
                    ORDER BY host, observed_at
                    """,
                    (history_points,),
                ).fetchall()
                event_rows = connection.execute(
                    f"""
                    SELECT event_id, host, condition_key, category, resource,
                           severity, value, threshold, condition_observed_at,
                           detail, group_key, state, observed_at
                    FROM (
                        SELECT * FROM incident_events
                        WHERE {_INCIDENT_ROW_FILTER}
                        ORDER BY event_id DESC LIMIT ?
                    )
                    ORDER BY event_id
                    """,
                    (incident_points,),
                ).fetchall()
                gpu_rows = connection.execute(
                    f"""
                    SELECT host, gpu_id, gpu_index, observed_at,
                           utilization_gpu_pct, memory_used_mib,
                           memory_total_mib, temperature_c, power_draw_w
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY host, gpu_id ORDER BY observed_at DESC
                        ) AS position
                        FROM gpu_history
                        WHERE {_GPU_ROW_FILTER}
                    )
                    WHERE position <= ?
                    ORDER BY host, gpu_id, observed_at
                    """,
                    (history_points,),
                ).fetchall()
                process_rows = connection.execute(
                    f"""
                    SELECT host, gpu_id, gpu_index, observed_at, event_type,
                           pid, name, used_memory_mib, workload_json
                    FROM (
                        SELECT p.*, ROW_NUMBER() OVER (
                            PARTITION BY p.host, p.gpu_id
                            ORDER BY p.observed_at DESC, p.event_type ASC,
                                     p.pid DESC, p.name DESC
                        ) AS position
                        FROM process_events AS p
                        WHERE {_PROCESS_ROW_FILTER}
                    )
                    WHERE position <= ?
                    ORDER BY host, gpu_id, observed_at, event_type DESC, pid, name
                    """,
                    (incident_points,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("cannot read the SQLite history database") from exc

        history: dict[str, list[dict[str, object]]] = {}
        for row in history_rows:
            host, observed_at, *values, transport_retried = row
            fields = dict(zip(_HISTORY_FIELDS, values, strict=True))
            if (
                not isinstance(host, str)
                or not 0 < len(host) <= 253
                or not isinstance(observed_at, str)
                or not 0 < len(observed_at) <= 64
                or transport_retried not in (0, 1)
                or not all(_is_optional_finite_number(value) for value in values)
                or any(fields[field] is None for field in _REQUIRED_HISTORY_FIELDS)
            ):
                continue
            point: dict[str, object] = {"observedAt": observed_at}
            point.update(fields)
            point["transportRetried"] = bool(transport_retried)
            history.setdefault(host, []).append(point)

        events = tuple(
            event
            for row in event_rows
            if (event := self._event_from_row(row)) is not None
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
            visible = host != _INTERNAL_USAGE_HOST
            if not visible:
                marker = (
                    parsed.get(_INTERNAL_USAGE_KEY)
                    if isinstance(parsed, dict)
                    else None
                )
                if not isinstance(marker, dict):
                    continue
                restored_host = marker.get("host")
                restored_gpu_id = marker.get("gpuId")
                restored_workload = marker.get("workload")
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
                workload = parsed
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
                    **({"_visible": False} if not visible else {}),
                }
            )

        restored_process_events = {}
        for key, items in process_events.items():
            items.sort(
                key=lambda item: (
                    str(item["observedAt"]),
                    0 if item["event"] == "stopped" else 1,
                    int(item["pid"]),
                    str(item["name"]),
                )
            )
            restored_process_events[key] = tuple(items[-incident_points:])

        return LoadedTelemetry(
            history={host: tuple(points) for host, points in history.items()},
            incident_events=events,
            gpu_history={key: tuple(points) for key, points in gpu_history.items()},
            process_events=restored_process_events,
        )

    def record_history(self, host: str, point: dict[str, object]) -> None:
        self._enqueue(_HistoryWrite(host, dict(point)))

    def record_incidents(self, events: tuple[IncidentEvent, ...]) -> None:
        for event in events:
            self._enqueue(_IncidentWrite(event))

    def record_gpu_telemetry(
        self,
        host: str,
        points: tuple[dict[str, object], ...],
        process_events: tuple[dict[str, object], ...],
    ) -> None:
        if points or process_events:
            self._enqueue(
                _GpuTelemetryWrite(
                    host,
                    tuple(dict(point) for point in points),
                    tuple(dict(event) for event in process_events),
                )
            )

    def status(self) -> dict[str, object]:
        with self._status_lock:
            return {
                "enabled": True,
                "backend": "sqlite",
                "healthy": self._last_error is None and self._writer.is_alive(),
                "queuedWrites": self._queue.qsize(),
                "droppedWrites": self._dropped_writes,
                "writtenRecords": self._written_records,
                "lastError": self._last_error,
            }

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        barrier = _Flush(threading.Event())
        while True:
            with self._admission_lock:
                with self._status_lock:
                    if self._closed:
                        return False
                try:
                    self._queue.put_nowait(barrier)
                except queue.Full:
                    pass
                else:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        if not barrier.completed.wait(max(0.0, deadline - time.monotonic())):
            return False
        return barrier.committed

    def close(self, timeout_seconds: float = 5.0) -> None:
        with self._admission_lock:
            with self._status_lock:
                self._closed = True
            self._stop_requested.set()
        self._writer.join(max(0.0, timeout_seconds))
        if self._writer.is_alive():
            self._set_error("history writer did not stop cleanly")

    def _enqueue(self, item: _Write) -> None:
        with self._admission_lock:
            with self._status_lock:
                if self._closed:
                    self._dropped_writes += 1
                    self._last_error = "history persistence is closed"
                    return
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                with self._status_lock:
                    self._dropped_writes += 1
                    self._last_error = "history write queue is full"

    def _prepare_database(self) -> None:
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.parent.chmod(0o700)
            if self._path.is_symlink():
                raise PersistenceError("history database must not be a symbolic link")
            if self._path.exists() and not self._path.is_file():
                raise PersistenceError("history database path is not a regular file")
            with (
                closing(sqlite3.connect(self._path, timeout=5)) as connection,
                connection,
            ):
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, 1, 2, _SCHEMA_VERSION}:
                    raise PersistenceError(
                        f"unsupported history schema version: {version}"
                    )
                if version == 0:
                    self._create_schema(connection)
                elif version == 1:
                    self._migrate_v1(connection)
                elif version == 2:
                    self._migrate_v2(connection)
                elif version == 3:
                    self._migrate_v3(connection)
                self._prune(connection)
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
                page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
                if page_count > max(1, self._config.max_bytes // page_size):
                    raise PersistenceError(
                        "history database exceeds the configured size limit"
                    )
                self._apply_size_limit(connection)
            self._path.chmod(0o600)
        except PersistenceError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise PersistenceError(
                "cannot initialize SQLite history persistence"
            ) from exc

    @classmethod
    def _create_schema(cls, connection: sqlite3.Connection) -> None:
        cls._apply_schema(connection, _CREATE_SCHEMA_STATEMENTS)

    @classmethod
    def _migrate_v1(cls, connection: sqlite3.Connection) -> None:
        cls._apply_schema(connection, _GPU_TABLE_STATEMENTS)

    @classmethod
    def _migrate_v2(cls, connection: sqlite3.Connection) -> None:
        cls._apply_schema(connection, ())

    @classmethod
    def _migrate_v3(cls, connection: sqlite3.Connection) -> None:
        cls._apply_schema(connection, ())

    @staticmethod
    def _apply_schema(
        connection: sqlite3.Connection, statements: tuple[str, ...]
    ) -> None:
        """Apply schema DDL atomically so an interrupted upgrade can be retried.

        Statements tolerate leftovers of a partially applied earlier run, and
        the user_version stamp only commits together with the schema changes.
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            history_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(history)")
            }
            if "transport_retried" not in history_columns:
                connection.execute(
                    "ALTER TABLE history"
                    " ADD COLUMN transport_retried INTEGER NOT NULL DEFAULT 0"
                )
            process_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(process_events)")
            }
            if "sequence" in process_columns:
                # Restore the released v3 nine-column table contract.  Older
                # writers use positional INSERTs and must remain able to write
                # after a one-version package rollback.
                connection.execute("DROP INDEX IF EXISTS process_events_observed_at")
                connection.execute(
                    "ALTER TABLE process_events RENAME TO process_events_sequenced"
                )
                for statement in _GPU_TABLE_STATEMENTS[2:4]:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO process_events (
                        host, gpu_id, gpu_index, observed_at, event_type,
                        pid, name, used_memory_mib, workload_json
                    )
                    SELECT host, gpu_id, gpu_index, observed_at, event_type,
                           pid, name, used_memory_mib, workload_json
                    FROM process_events_sequenced
                    """
                )
                connection.execute("DROP TABLE process_events_sequenced")
            # Remove tables/triggers created by short-lived development builds.
            # They were never part of a released schema and can otherwise use
            # retention space that a rolled-back v3 binary cannot reclaim.
            connection.execute("DROP TRIGGER IF EXISTS process_events_order_insert")
            connection.execute("DROP TRIGGER IF EXISTS process_events_order_delete")
            connection.execute("DROP TRIGGER IF EXISTS process_events_prune_order")
            connection.execute("DROP TRIGGER IF EXISTS gpu_history_prune_usage_events")
            connection.execute("DROP TABLE IF EXISTS process_event_order")
            connection.execute("DROP TABLE IF EXISTS process_usage_events")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = NORMAL")
            self._apply_size_limit(connection)
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _apply_size_limit(self, connection: sqlite3.Connection) -> None:
        """Cap the database size; max_page_count only binds its own connection."""
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        max_pages = max(1, self._config.max_bytes // page_size)
        connection.execute(f"PRAGMA max_page_count = {max_pages}")
        applied = int(connection.execute("PRAGMA max_page_count").fetchone()[0])
        if applied != max_pages:
            raise sqlite3.OperationalError(
                "history database size limit could not be applied"
            )

    def _write_loop(self) -> None:
        try:
            connection = self._connect()
        except sqlite3.Error:
            self._set_error("history writer could not open the database")
            return
        next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS
        try:
            while True:
                try:
                    first = self._queue.get(
                        timeout=min(0.1, max(0.0, next_prune_at - time.monotonic()))
                    )
                except queue.Empty:
                    if self._stop_requested.is_set():
                        break
                    # Retention must keep holding during idle periods too.
                    self._prune_batch(connection)
                    next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS
                    continue
                items = [first]
                if isinstance(
                    first, _HistoryWrite | _IncidentWrite | _GpuTelemetryWrite
                ):
                    for _ in range(_WRITE_BATCH_SIZE - 1):
                        try:
                            item = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        items.append(item)
                        if isinstance(item, _Flush):
                            break

                writes = tuple(
                    item
                    for item in items
                    if isinstance(
                        item, _HistoryWrite | _IncidentWrite | _GpuTelemetryWrite
                    )
                )
                committed = not writes or self._commit_batch(connection, writes)
                if time.monotonic() >= next_prune_at:
                    self._prune_batch(connection)
                    next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS

                for item in items:
                    if isinstance(item, _Flush):
                        item.committed = committed
                        item.completed.set()
                    self._queue.task_done()
        except Exception:
            # Corrupt internal records must fail this writer, not the collector.
            self._set_error("history writer stopped unexpectedly")
        finally:
            connection.close()

    def _commit_batch(
        self, connection: sqlite3.Connection, writes: tuple[_Write, ...]
    ) -> bool:
        pruned = False
        while True:
            try:
                written_records = 0
                with connection:
                    for item in writes:
                        written_records += self._write(connection, item)
            except sqlite3.Error as exc:
                if not pruned and _is_size_error(exc):
                    # Expired records may free enough space; retry this batch
                    # once after pruning instead of dropping it outright.
                    pruned = True
                    if self._prune_batch(connection):
                        continue
                with self._status_lock:
                    self._dropped_writes += len(writes)
                    self._last_error = "history database write failed"
                return False
            with self._status_lock:
                self._written_records += written_records
                self._last_error = None
            return True

    def _prune_batch(self, connection: sqlite3.Connection) -> bool:
        """Prune in a dedicated transaction so failures stay contained."""
        try:
            with connection:
                self._prune(connection)
        except sqlite3.Error:
            self._set_error("history database prune failed")
            return False
        return True

    @staticmethod
    def _write(connection: sqlite3.Connection, item: _Write) -> int:
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
                    *(point.get(field) for field in _HISTORY_FIELDS),
                    1 if point.get("transportRetried") else 0,
                ),
            )
            return max(0, cursor.rowcount)

        if isinstance(item, _GpuTelemetryWrite):
            return SqliteTelemetryPersistence._write_gpu_telemetry(connection, item)

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
        return max(0, cursor.rowcount)

    @staticmethod
    def _write_gpu_telemetry(
        connection: sqlite3.Connection, item: _GpuTelemetryWrite
    ) -> int:
        written_records = 0
        if item.points:
            cursor = connection.executemany(
                """
                INSERT OR REPLACE INTO gpu_history VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    (
                        item.host,
                        point.get("gpuId"),
                        point.get("index"),
                        point.get("observedAt"),
                        point.get("utilizationGpuPct"),
                        point.get("memoryUsedMiB"),
                        point.get("memoryTotalMiB"),
                        point.get("temperatureC"),
                        point.get("powerDrawW"),
                    )
                    for point in item.points
                ),
            )
            written_records += max(0, cursor.rowcount)
        if item.process_events:
            visible_events = tuple(
                event
                for event in item.process_events
                if event.get("_visible") is not False
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
                        SqliteTelemetryPersistence._serialize_workload(event),
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
                        _INTERNAL_USAGE_HOST,
                        SqliteTelemetryPersistence._internal_usage_gpu_key(
                            item.host, event
                        ),
                        event.get("index"),
                        event.get("observedAt"),
                        event.get("event"),
                        event.get("pid"),
                        event.get("name"),
                        event.get("usedMemoryMiB"),
                        SqliteTelemetryPersistence._serialize_hidden_usage(
                            item.host, event
                        ),
                    )
                    for event in hidden_events
                ),
            )
            written_records += max(0, hidden_cursor.rowcount)
        return written_records

    @staticmethod
    def _internal_usage_gpu_key(host: str, event: dict[str, object]) -> str:
        identity = f"{host}\x00{event.get('gpuId')}".encode(
            "utf-8", errors="surrogatepass"
        )
        return hashlib.sha256(identity).hexdigest()

    @staticmethod
    def _serialize_hidden_usage(host: str, event: dict[str, object]) -> str:
        return json.dumps(
            {
                _INTERNAL_USAGE_KEY: {
                    "host": host,
                    "gpuId": event.get("gpuId"),
                    "workload": event.get("workload")
                    if isinstance(event.get("workload"), dict)
                    else None,
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _serialize_workload(event: dict[str, object]) -> str | None:
        workload = event.get("workload")
        if not isinstance(workload, dict):
            return None
        return json.dumps(
            workload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _prune(self, connection: sqlite3.Connection) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._config.retention_hours
        )
        cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
        connection.execute("DELETE FROM history WHERE observed_at < ?", (cutoff_text,))
        connection.execute(
            "DELETE FROM incident_events WHERE observed_at < ?", (cutoff_text,)
        )
        connection.execute(
            "DELETE FROM gpu_history WHERE observed_at < ?", (cutoff_text,)
        )
        connection.execute(
            "DELETE FROM process_events WHERE observed_at < ?", (cutoff_text,)
        )
        connection.execute("PRAGMA incremental_vacuum")

    @staticmethod
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
            or severity not in _INCIDENT_SEVERITIES
            or state not in _INCIDENT_STATES
            or not _is_optional_finite_number(value)
            or not _is_optional_finite_number(threshold)
            or (
                detail is not None
                and (not isinstance(detail, str) or len(detail) > 4096)
            )
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

    def _set_error(self, message: str) -> None:
        with self._status_lock:
            self._last_error = message


PersistenceFactory = Callable[[PersistenceConfig, Path], TelemetryPersistence]


def user_state_path(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    service_root = values.get("STATE_DIRECTORY", "").strip()
    if service_root:
        return (Path(service_root).expanduser() / "history.sqlite3").absolute()
    xdg_root = values.get("XDG_STATE_HOME", "").strip()
    root = Path(xdg_root).expanduser() if xdg_root else Path.home() / ".local/state"
    return (root / "mocop" / "history.sqlite3").absolute()


def _disabled_factory(_config: PersistenceConfig, _path: Path) -> TelemetryPersistence:
    return DisabledPersistence()


_PERSISTENCE_FACTORIES: dict[str, PersistenceFactory] = {
    "memory": _disabled_factory,
    "sqlite": SqliteTelemetryPersistence,
}


def create_persistence(
    config: PersistenceConfig,
    path: Path | None = None,
) -> TelemetryPersistence:
    backend = "sqlite" if config.enabled else "memory"
    return _PERSISTENCE_FACTORIES[backend](config, path or user_state_path())
