from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import PersistenceConfig
from .incident_types import IncidentEvent
from .persistence_api import (
    DisabledPersistence,
    PersistenceError,
    TelemetryPersistence,
)
from .persistence_restore import LoadedTelemetry, restore_telemetry
from .persistence_rollups import (
    ensure_rollups,
    load_report_inputs,
    prune_rollups,
    upsert_rollups,
)
from .persistence_schema import (
    CREATE_SCHEMA_STATEMENTS,
    GPU_TABLE_STATEMENTS,
    HISTORY_FIELDS,
    SCHEMA_VERSION,
)
from .persistence_transitions import (
    FIRST_SEEN_KEY,
    INTERNAL_USAGE_HOST,
    INTERNAL_USAGE_KEY,
)
from .reports import ReportInputs

# Keep the released v3 process_events table byte-for-byte compatible.  Older
# writers use positional INSERTs, so even an appended nullable column would
# break package rollback.  Same-timestamp transitions are ordered
# deterministically at read time, with ``stopped`` before ``started``.
_QUEUE_CAPACITY = 4096
_WRITE_BATCH_SIZE = 128
_PRUNE_INTERVAL_SECONDS = 60.0
# Steady-state retention frees a few dozen pages a minute. The online reclaim
# after each prune is bounded in pages (8 MiB at the 4 KiB page size) and in
# pragma calls, so the writer's transaction stays short on every interpreter;
# a backlog left by a long downtime or a lowered cap is rebuilt away at startup.
_VACUUM_PAGES_PER_PRUNE = 2048
_VACUUM_CHUNK_PAGES = 1024
_VACUUM_CALLS_PER_PRUNE = 64
_SQLITE_FULL_ERRORCODE = 13  # sqlite3.SQLITE_FULL is unavailable on Python 3.10


def _free_pages(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA freelist_count").fetchone()[0])


def _reclaim_free_pages(connection: sqlite3.Connection, page_budget: int) -> int:
    """Return up to ``page_budget`` freed pages to the filesystem online.

    ``PRAGMA incremental_vacuum`` releases pages as its statement is stepped,
    one per step, so a bare ``execute()`` reclaims a single page and the file
    keeps its high-water mark forever. Exhausting the cursor frees the rest on
    most interpreters, but CPython 3.11's ``sqlite3`` yields no rows for the
    pragma and still frees one page per call, so progress is measured on
    ``freelist_count`` and the call count is bounded: a large backlog is not
    this path's job (startup rebuilds the file instead), keeping up with the
    few dozen pages a minute that retention frees is.
    """
    reclaimed = 0
    for _call in range(_VACUUM_CALLS_PER_PRUNE):
        before = _free_pages(connection)
        chunk = min(before, page_budget - reclaimed, _VACUUM_CHUNK_PAGES)
        if chunk <= 0:
            break
        for _row in connection.execute(f"PRAGMA incremental_vacuum({chunk})"):
            pass
        freed = before - _free_pages(connection)
        if freed <= 0:
            break
        reclaimed += freed
    return reclaimed


def _rebuild_file(connection: sqlite3.Connection) -> None:
    """Return every free page at startup, preferring a one-statement rebuild.

    ``VACUUM`` needs temporary space up to the file's size; on a full disk or
    a file another process holds it fails without touching the database, so
    the online reclaim runs instead and the cap check judges what remains.
    A refusal to start would take collection and the dashboard down over a
    condition the persistence status already reports.
    """
    try:
        connection.execute("VACUUM")
    except sqlite3.OperationalError:
        with connection:
            _reclaim_free_pages(connection, _VACUUM_PAGES_PER_PRUNE)


def _is_size_error(exc: sqlite3.Error) -> bool:
    if getattr(exc, "sqlite_errorcode", None) == _SQLITE_FULL_ERRORCODE:
        return True
    return "full" in str(exc).lower()


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
    """Write barrier: set once every write queued ahead of it was processed."""

    completed: threading.Event


_Write = _HistoryWrite | _IncidentWrite | _GpuTelemetryWrite
_QueueItem = _Write | _Flush


class SqliteTelemetryPersistence:
    """Bounded asynchronous SQLite history storage.

    Collection threads only perform a non-blocking queue insertion. The dedicated
    writer owns its SQLite connection, batches commits, and contains disk failures.
    """

    def __init__(
        self, config: PersistenceConfig, path: Path, *, busy_pct: float = 10.0
    ) -> None:
        if not config.enabled:
            raise ValueError("SQLite persistence requires enabled configuration")
        self._config = config
        self._path = path.expanduser().absolute()
        self._busy_pct = float(busy_pct)
        self._rollups_enabled = False
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
        """Restore the newest retained points of every host and GPU."""
        try:
            with closing(self._connect()) as connection:
                return restore_telemetry(connection, history_points, incident_points)
        except sqlite3.Error as exc:
            raise PersistenceError("cannot read the SQLite history database") from exc

    def report_inputs(self, since_hour: str) -> ReportInputs | None:
        """Rows for a long-window report; None until the rollup table exists."""
        if not self._rollups_enabled:
            return None
        try:
            with closing(self._connect()) as connection:
                return load_report_inputs(
                    connection, self._config.retention_hours, since_hour
                )
        except sqlite3.Error as exc:
            raise PersistenceError("cannot read the SQLite history database") from exc

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
        """Wait for every write queued so far; True only if all were committed.

        The writer batches whatever is queued when it wakes, so a write and
        the barrier that follows it may land in different batches; the drop
        counter, not the barrier's own batch, says whether anything ahead of
        the barrier was lost.
        """
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        barrier = _Flush(threading.Event())
        while True:
            with self._admission_lock:
                with self._status_lock:
                    if self._closed:
                        return False
                    dropped_before = self._dropped_writes
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
        with self._status_lock:
            return self._dropped_writes == dropped_before

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
                if not self._writer.is_alive():
                    # The writer recorded why it stopped; queueing behind it
                    # would only replace that cause with "queue is full".
                    self._dropped_writes += 1
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
            with closing(sqlite3.connect(self._path, timeout=5)) as connection:
                with connection:
                    connection.execute("PRAGMA journal_mode = DELETE")
                    connection.execute("PRAGMA synchronous = NORMAL")
                    connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
                    version = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    if version not in {0, 1, 2, SCHEMA_VERSION}:
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
                    self._prune(connection, reclaim_pages=0)
                # Pages the prune (or a long downtime's worth of expiry) left on
                # the freelist go back before the cap is compared, so a lowered
                # max_bytes judges the live data rather than the file's old
                # high-water mark. VACUUM rebuilds the file in one statement,
                # proportional to the live data and independent of how the
                # interpreter steps the incremental pragma; it runs outside a
                # transaction and only when there is something to reclaim.
                if _free_pages(connection) > 0:
                    _rebuild_file(connection)
                with connection:
                    page_size = int(
                        connection.execute("PRAGMA page_size").fetchone()[0]
                    )
                    page_count = int(
                        connection.execute("PRAGMA page_count").fetchone()[0]
                    )
                    if page_count > max(1, self._config.max_bytes // page_size):
                        raise PersistenceError(
                            "history database exceeds the configured size limit"
                        )
                    self._apply_size_limit(connection)
                rollup_error = ensure_rollups(connection, self._busy_pct)
                self._rollups_enabled = rollup_error is None
                if rollup_error is not None:
                    self._set_error(rollup_error)
            self._path.chmod(0o600)
        except PersistenceError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise PersistenceError(
                "cannot initialize SQLite history persistence"
            ) from exc

    @classmethod
    def _create_schema(cls, connection: sqlite3.Connection) -> None:
        cls._apply_schema(connection, CREATE_SCHEMA_STATEMENTS)

    @classmethod
    def _migrate_v1(cls, connection: sqlite3.Connection) -> None:
        cls._apply_schema(connection, GPU_TABLE_STATEMENTS)

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
                for statement in GPU_TABLE_STATEMENTS[2:4]:
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
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
                    # Retention must keep holding during idle periods too,
                    # on the same interval: the short get timeout exists for
                    # stop responsiveness, not as the prune cadence.
                    if time.monotonic() >= next_prune_at:
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
                if writes:
                    self._commit_batch(connection, writes)
                if time.monotonic() >= next_prune_at:
                    self._prune_batch(connection)
                    next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS

                for item in items:
                    if isinstance(item, _Flush):
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
                        written_records += self._write(
                            connection,
                            item,
                            self._busy_pct if self._rollups_enabled else None,
                        )
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
    def _write(
        connection: sqlite3.Connection, item: _Write, busy_pct: float | None
    ) -> int:
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
            return SqliteTelemetryPersistence._write_gpu_telemetry(
                connection, item, busy_pct
            )

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
        connection: sqlite3.Connection,
        item: _GpuTelemetryWrite,
        busy_pct: float | None,
    ) -> int:
        written_records = 0
        if item.points and busy_pct is not None:
            upsert_rollups(connection, item.host, item.points, busy_pct)
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
                        INTERNAL_USAGE_HOST,
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

    @staticmethod
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

    def _prune(
        self,
        connection: sqlite3.Connection,
        *,
        reclaim_pages: int = _VACUUM_PAGES_PER_PRUNE,
    ) -> None:
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
        if self._rollups_enabled:
            prune_rollups(connection, self._config.retention_hours)
        if reclaim_pages > 0:
            _reclaim_free_pages(connection, reclaim_pages)

    def _set_error(self, message: str) -> None:
        with self._status_lock:
            self._last_error = message


def user_state_path(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    service_root = values.get("STATE_DIRECTORY", "").strip()
    if service_root:
        return (Path(service_root).expanduser() / "history.sqlite3").absolute()
    xdg_root = values.get("XDG_STATE_HOME", "").strip()
    root = Path(xdg_root).expanduser() if xdg_root else Path.home() / ".local/state"
    return (root / "mocop" / "history.sqlite3").absolute()


def create_persistence(
    config: PersistenceConfig,
    path: Path | None = None,
    *,
    busy_pct: float = 10.0,
) -> TelemetryPersistence:
    if not config.enabled:
        return DisabledPersistence()
    return SqliteTelemetryPersistence(
        config, path or user_state_path(), busy_pct=busy_pct
    )
