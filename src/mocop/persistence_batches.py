"""Batch transactions, loss accounting, and commit-bound incident criteria."""

from __future__ import annotations

import sqlite3
from datetime import timedelta, timezone
from typing import TYPE_CHECKING

from .persistence_criteria import prepare_incident_write
from .persistence_gpu import filter_gpu_points, gpu_cutoff
from .persistence_records import (
    _GpuTelemetryWrite,
    _IncidentStateWrite,
    _IncidentWrite,
    _Write,
)

if TYPE_CHECKING:
    from .persistence import SqliteTelemetryPersistence


def commit_batch(
    self: SqliteTelemetryPersistence,
    connection: sqlite3.Connection,
    writes: tuple[_Write, ...],
) -> bool:
    from .persistence import _is_size_error, datetime

    pruned = False
    while True:
        try:
            criteria_cache = self._incident_criteria.copy()
            written_records = 0
            dropped_writes = 0
            with connection:
                if not self._companions_enabled and any(
                    isinstance(
                        item,
                        _GpuTelemetryWrite | _IncidentWrite | _IncidentStateWrite,
                    )
                    for item in writes
                ):
                    raise sqlite3.OperationalError("history metadata waits for space")
                if not self._rollups_enabled and any(
                    isinstance(item, _GpuTelemetryWrite) and item.points
                    for item in writes
                ):
                    raise sqlite3.OperationalError("hourly rollups are unavailable")
                cutoff = None
                if any(
                    isinstance(item, _GpuTelemetryWrite) and item.points
                    for item in writes
                ):
                    now = datetime.now(timezone.utc) - timedelta(
                        hours=self._config.retention_hours
                    )
                    cutoff = gpu_cutoff(
                        connection,
                        now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    )
                for item in writes:
                    if isinstance(item, _GpuTelemetryWrite) and cutoff is not None:
                        retained = filter_gpu_points(item.points, cutoff)
                        dropped_writes += retained != item.points
                        item = _GpuTelemetryWrite(
                            item.host, retained, item.process_events
                        )
                    item = prepare_incident_write(connection, item, criteria_cache)
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
        self._incident_criteria = criteria_cache
        with self._status_lock:
            self._written_records += written_records
            self._dropped_writes += dropped_writes
            self._last_error = (
                "history write outside retention window" if dropped_writes else None
            )
        return True
