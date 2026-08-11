from __future__ import annotations

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

_SCHEMA_VERSION = 2
_QUEUE_CAPACITY = 4096
_WRITE_BATCH_SIZE = 128
_PRUNE_INTERVAL_SECONDS = 60.0
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


def _is_optional_finite_number(value: object) -> bool:
    return value is None or (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
    )


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


@dataclass(frozen=True, slots=True)
class _Flush:
    completed: threading.Event


class _Stop:
    pass


_Write = _HistoryWrite | _IncidentWrite | _GpuTelemetryWrite
_QueueItem = _Write | _Flush | _Stop


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
        self._status_lock = threading.Lock()
        self._closed = False
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
                    """
                    SELECT host, observed_at, cpu_usage_pct, memory_usage_pct,
                           swap_usage_pct, disk_usage_pct, network_rx_bps,
                           network_tx_bps, disk_read_bps, disk_write_bps,
                           gpu_usage_pct, gpu_memory_usage_pct, gpu_temperature_c
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY host ORDER BY observed_at DESC
                        ) AS position
                        FROM history
                    )
                    WHERE position <= ?
                    ORDER BY host, observed_at
                    """,
                    (history_points,),
                ).fetchall()
                event_rows = connection.execute(
                    """
                    SELECT event_id, host, condition_key, category, resource,
                           severity, value, threshold, condition_observed_at,
                           detail, group_key, state, observed_at
                    FROM (
                        SELECT * FROM incident_events
                        ORDER BY event_id DESC LIMIT ?
                    )
                    ORDER BY event_id
                    """,
                    (incident_points,),
                ).fetchall()
                gpu_rows = connection.execute(
                    """
                    SELECT host, gpu_id, gpu_index, observed_at,
                           utilization_gpu_pct, memory_used_mib,
                           memory_total_mib, temperature_c, power_draw_w
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY host, gpu_id ORDER BY observed_at DESC
                        ) AS position
                        FROM gpu_history
                    )
                    WHERE position <= ?
                    ORDER BY host, gpu_id, observed_at
                    """,
                    (history_points,),
                ).fetchall()
                process_rows = connection.execute(
                    """
                    SELECT host, gpu_id, gpu_index, observed_at, event_type,
                           pid, name, used_memory_mib, workload_json
                    FROM process_events
                    ORDER BY observed_at DESC LIMIT ?
                    """,
                    (incident_points,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("cannot read the SQLite history database") from exc

        history: dict[str, list[dict[str, object]]] = {}
        for row in history_rows:
            host, observed_at, *values = row
            if (
                not isinstance(host, str)
                or not 0 < len(host) <= 253
                or not isinstance(observed_at, str)
                or not 0 < len(observed_at) <= 64
                or not all(_is_optional_finite_number(value) for value in values)
            ):
                continue
            point = {"observedAt": observed_at}
            point.update(dict(zip(_HISTORY_FIELDS, values, strict=True)))
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
        for row in reversed(process_rows):
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
            if isinstance(raw_workload, str) and len(raw_workload) <= 4096:
                try:
                    parsed = json.loads(raw_workload)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
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
                }
            )

        return LoadedTelemetry(
            history={host: tuple(points) for host, points in history.items()},
            incident_events=events,
            gpu_history={key: tuple(points) for key, points in gpu_history.items()},
            process_events={key: tuple(items) for key, items in process_events.items()},
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
        completed = threading.Event()
        try:
            self._queue.put(_Flush(completed), timeout=max(0.0, timeout_seconds))
        except queue.Full:
            return False
        return completed.wait(max(0.0, timeout_seconds))

    def close(self, timeout_seconds: float = 5.0) -> None:
        with self._status_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put(_Stop(), timeout=max(0.0, timeout_seconds))
        except queue.Full:
            self._set_error("history queue did not drain during shutdown")
            return
        self._writer.join(max(0.0, timeout_seconds))
        if self._writer.is_alive():
            self._set_error("history writer did not stop cleanly")

    def _enqueue(self, item: _Write) -> None:
        with self._status_lock:
            if self._closed:
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
                if version not in {0, 1, _SCHEMA_VERSION}:
                    raise PersistenceError(
                        f"unsupported history schema version: {version}"
                    )
                if version == 0:
                    self._create_schema(connection)
                elif version == 1:
                    self._migrate_v1(connection)
                self._prune(connection)
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
                page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
                max_pages = max(1, self._config.max_bytes // page_size)
                if page_count > max_pages:
                    raise PersistenceError(
                        "history database exceeds the configured size limit"
                    )
                connection.execute(f"PRAGMA max_page_count = {max_pages}")
            self._path.chmod(0o600)
        except PersistenceError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise PersistenceError(
                "cannot initialize SQLite history persistence"
            ) from exc

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE history (
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
                PRIMARY KEY (host, observed_at)
            ) WITHOUT ROWID;
            CREATE INDEX history_observed_at ON history(observed_at);

            CREATE TABLE incident_events (
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
            );
            CREATE INDEX incident_events_observed_at
                ON incident_events(observed_at);

            CREATE TABLE gpu_history (
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
            ) WITHOUT ROWID;
            CREATE INDEX gpu_history_observed_at ON gpu_history(observed_at);

            CREATE TABLE process_events (
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
            ) WITHOUT ROWID;
            CREATE INDEX process_events_observed_at
                ON process_events(observed_at);
            PRAGMA user_version = 2;
            """
        )

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE gpu_history (
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
            ) WITHOUT ROWID;
            CREATE INDEX gpu_history_observed_at ON gpu_history(observed_at);
            CREATE TABLE process_events (
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
            ) WITHOUT ROWID;
            CREATE INDEX process_events_observed_at
                ON process_events(observed_at);
            PRAGMA user_version = 2;
            """
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _write_loop(self) -> None:
        try:
            connection = self._connect()
        except sqlite3.Error:
            self._set_error("history writer could not open the database")
            return
        next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS
        try:
            should_stop = False
            while not should_stop:
                first = self._queue.get()
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
                        if isinstance(item, _Flush | _Stop):
                            break

                writes = tuple(
                    item
                    for item in items
                    if isinstance(
                        item, _HistoryWrite | _IncidentWrite | _GpuTelemetryWrite
                    )
                )
                if writes:
                    try:
                        written_records = 0
                        with connection:
                            for item in writes:
                                written_records += self._write(connection, item)
                            if time.monotonic() >= next_prune_at:
                                self._prune(connection)
                                next_prune_at = (
                                    time.monotonic() + _PRUNE_INTERVAL_SECONDS
                                )
                    except sqlite3.Error:
                        with self._status_lock:
                            self._dropped_writes += len(writes)
                            self._last_error = "history database write failed"
                    else:
                        with self._status_lock:
                            self._written_records += written_records
                            self._last_error = None

                for item in items:
                    if isinstance(item, _Flush):
                        item.completed.set()
                    elif isinstance(item, _Stop):
                        should_stop = True
                    self._queue.task_done()
        except Exception:
            # Corrupt internal records must fail this writer, not the collector.
            self._set_error("history writer stopped unexpectedly")
        finally:
            connection.close()

    @staticmethod
    def _write(connection: sqlite3.Connection, item: _Write) -> int:
        if isinstance(item, _HistoryWrite):
            point = item.point
            cursor = connection.execute(
                """
                INSERT OR REPLACE INTO history VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    item.host,
                    point.get("observedAt"),
                    *(point.get(field) for field in _HISTORY_FIELDS),
                ),
            )
            return max(0, cursor.rowcount)

        if isinstance(item, _GpuTelemetryWrite):
            return SqliteTelemetryPersistence._write_gpu_telemetry(connection, item)

        assert isinstance(item, _IncidentWrite)
        event = item.event
        condition = event.condition
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO incident_events VALUES (
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
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO process_events VALUES (
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
                    for event in item.process_events
                ),
            )
            written_records += max(0, cursor.rowcount)
        return written_records

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
