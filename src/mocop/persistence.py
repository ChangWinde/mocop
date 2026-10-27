from __future__ import annotations

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
from .persistence_batches import commit_batch
from .persistence_criteria import CriteriaCache
from .persistence_gpu import gpu_cutoff, prune_gpu
from .persistence_incidents import prune_incidents
from .persistence_restore import LoadedTelemetry, restore_telemetry
from .persistence_retention import bootstrap_gpu_retention
from .persistence_rollups import (
    ensure_rollups,
    load_report_inputs,
    prune_rollups,
)
from .persistence_schema import (
    COMPANION_STATEMENTS,
    CREATE_SCHEMA_STATEMENTS,
    GPU_TABLE_STATEMENTS,
    LEGACY_MEMORY_BASELINE,
    SCHEMA_VERSION,
)
from .persistence_space import (
    _VACUUM_PAGES_PER_PRUNE,
    _free_pages,
    _rebuild_file,
    _reclaim_free_pages,
)
from .persistence_writes import (
    _GpuTelemetryWrite,
    _HistoryWrite,
    _IncidentStateWrite,
    _IncidentWrite,
    _Write,
    _write,
)
from .reports import ReportInputs

# Keep the released v3 process_events table byte-for-byte compatible.  Older
# writers use positional INSERTs, so even an appended nullable column would
# break package rollback.  Same-timestamp transitions are ordered
# deterministically at read time, with ``stopped`` before ``started``.
_QUEUE_CAPACITY = 4096
_WRITE_BATCH_SIZE = 128
_PRUNE_INTERVAL_SECONDS = 60.0
_SQLITE_FULL_ERRORCODE = 13  # sqlite3.SQLITE_FULL is unavailable on Python 3.10


def _is_size_error(exc: sqlite3.Error) -> bool:
    if getattr(exc, "sqlite_errorcode", None) == _SQLITE_FULL_ERRORCODE:
        return True
    return "full" in str(exc).lower()


@dataclass(slots=True)
class _Flush:
    """Write barrier: set once every write queued ahead of it was processed."""

    completed: threading.Event
    dropped: int = 0
    failed: bool = False


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
        self._companions_enabled = False
        self._queue: queue.Queue[_QueueItem] = queue.Queue(_QUEUE_CAPACITY)
        # Serializes producer admission with close.  Without this boundary a
        # producer could observe ``_closed == False``, lose the CPU to close,
        # and enqueue after the writer had already exited.
        self._admission_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._closed = False
        self._writer_failed = False
        self._flushed_drops = 0
        self._stop_requested = threading.Event()
        self._dropped_writes = 0
        self._written_records = 0
        self._incident_criteria: CriteriaCache = {}
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

    def record_incident_state(
        self,
        host: str,
        events: tuple[IncidentEvent, ...],
        criteria: tuple[tuple[str, float | None], ...],
    ) -> None:
        if events or criteria:
            self._enqueue(_IncidentStateWrite(host, events, criteria))

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
        """Wait for prior writes; report losses since the last completed flush.

        A completed flush acknowledges its losses even when returning False.
        Timeouts do not acknowledge losses. Each barrier snapshots the counter
        before later writes can fail, and retries never reset the baseline.
        """
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        barrier = _Flush(threading.Event())
        with self._status_lock:
            dropped_before = self._flushed_drops
        while True:
            with self._admission_lock:
                with self._status_lock:
                    if self._closed or self._writer_failed:
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
        with self._status_lock:
            self._flushed_drops = max(self._flushed_drops, barrier.dropped)
            return not barrier.failed and barrier.dropped == dropped_before

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
                if self._writer_failed or not self._writer.is_alive():
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
                    self._companions_enabled = (
                        connection.execute(
                            "SELECT count(*) FROM sqlite_master WHERE name IN "
                            "('incident_criteria', 'gpu_retention', 'gpu_rollup_busy', 'gpu_retired_memory')"
                        ).fetchone()[0]
                        == 4
                    )
                    cutoff = self._prune(connection, reclaim_pages=0)
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
                self._companions_enabled = self._migrate_companions(connection, cutoff)
                rollup_error = (
                    ensure_rollups(connection, self._busy_pct)
                    if self._companions_enabled
                    else "hourly rollups wait for retention to free space"
                )
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

    @staticmethod
    def _migrate_companions(
        connection: sqlite3.Connection, cutoff: str | None = None
    ) -> bool:
        """Add metadata atomically under the cap; retry after retention frees space."""
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                had_memory = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'gpu_retired_memory'"
                ).fetchone()
                for statement in COMPANION_STATEMENTS:
                    connection.execute(statement)
                if (
                    not had_memory
                    and connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'gpu_hourly'"
                    ).fetchone()
                ):
                    connection.execute(LEGACY_MEMORY_BASELINE)
                if cutoff is not None:
                    prune_gpu(connection, gpu_cutoff(connection, cutoff))
        except sqlite3.OperationalError as exc:
            if not _is_size_error(exc):
                raise
            return False
        return True

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

    def _finish_items(self, items: list[_QueueItem], *, failed: bool = False) -> None:
        for item in items:
            if isinstance(item, _Flush):
                with self._status_lock:
                    item.dropped = self._dropped_writes
                item.failed = failed
                item.completed.set()
            self._queue.task_done()

    def _write_loop(self) -> None:
        connection = None
        items: list[_QueueItem] = []
        uncommitted = 0
        error = "history writer could not open the database"
        try:
            connection = self._connect()
            error = "history writer stopped unexpectedly"
            next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS
            while True:
                try:
                    first = self._queue.get(
                        timeout=min(0.1, max(0.0, next_prune_at - time.monotonic()))
                    )
                except queue.Empty:
                    if self._stop_requested.is_set():
                        break
                    if time.monotonic() >= next_prune_at:
                        self._prune_batch(connection)
                        next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS
                    continue
                items = [first]
                if not isinstance(first, _Flush):
                    for _ in range(_WRITE_BATCH_SIZE - 1):
                        try:
                            item = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        items.append(item)
                        if isinstance(item, _Flush):
                            break
                writes = tuple(item for item in items if not isinstance(item, _Flush))
                uncommitted = len(writes)
                if writes:
                    self._commit_batch(connection, writes)
                uncommitted = 0
                if time.monotonic() >= next_prune_at:
                    self._prune_batch(connection)
                    next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS
                self._finish_items(items)
                items = []
        except Exception:
            # Close admission before draining: a producer must never enqueue
            # behind a writer that has decided to exit but is still alive.
            with self._admission_lock:
                self._writer_failed = True
                pending = []
                while True:
                    try:
                        pending.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                with self._status_lock:
                    self._dropped_writes += uncommitted + sum(
                        not isinstance(item, _Flush) for item in pending
                    )
                    self._last_error = error
                self._finish_items(items + pending, failed=True)
        finally:
            if connection is not None:
                connection.close()

    _commit_batch = commit_batch

    def _prune_batch(self, connection: sqlite3.Connection) -> bool:
        """Prune in a dedicated transaction so failures stay contained."""
        try:
            with connection:
                self._prune(connection)
        except sqlite3.Error:
            self._set_error("history database prune failed")
            return False
        return True

    _write = staticmethod(_write)

    def _prune(
        self,
        connection: sqlite3.Connection,
        *,
        reclaim_pages: int = _VACUUM_PAGES_PER_PRUNE,
    ) -> str:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._config.retention_hours
        )
        cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
        connection.execute("DELETE FROM history WHERE observed_at < ?", (cutoff_text,))
        prune_incidents(connection, cutoff_text, companions=self._companions_enabled)
        if self._companions_enabled:
            prune_gpu(connection, gpu_cutoff(connection, cutoff_text))
        else:
            bootstrap_gpu_retention(connection, cutoff_text, self._config.max_bytes)
        connection.execute(
            "DELETE FROM process_events WHERE observed_at < ?", (cutoff_text,)
        )
        if self._rollups_enabled:
            prune_rollups(connection, self._config.retention_hours)
        if reclaim_pages > 0:
            _reclaim_free_pages(connection, reclaim_pages)
        return cutoff_text

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
