"""Capture confirmed incident criteria under the store lock and persist them.

The projection reads the tracker's confirmed conditions, including the criterion
held during pending severity changes; it does not derive incident conditions.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .incident_types import IncidentEvent

if TYPE_CHECKING:
    from .service import StateStore


def persist_incidents(
    store: StateStore, host: str, events: tuple[IncidentEvent, ...]
) -> None:
    """Called under StateStore's condition, before another sample can overtake it."""
    if not store._persistence_enabled:
        return
    criteria = tuple(
        (key, condition.critical_at)
        for key, condition in store._incidents._active.get(host, {}).items()
    )
    record = getattr(store._persistence, "record_incident_state", None)
    if record is not None:
        record(host, events, criteria)
    elif events:
        # Preserve the transition-only contract for older in-process adapters.
        store._persistence.record_incidents(events)


def write_criteria(
    connection: sqlite3.Connection,
    host: str,
    criteria: tuple[tuple[str, float | None], ...],
) -> int:
    """Coalesce unchanged criteria without inventing public transitions."""
    if not criteria:
        return 0
    cursor = connection.executemany(
        """
        INSERT INTO incident_criteria (event_id, critical_at)
        SELECT event_id, ? FROM incident_events
        WHERE host = ? AND condition_key = ? AND state != 'resolved'
          AND event_id = (
              SELECT max(event_id) FROM incident_events WHERE host = ? AND condition_key = ?
          )
        ON CONFLICT(event_id) DO UPDATE SET critical_at = excluded.critical_at
        WHERE critical_at IS NOT excluded.critical_at
        """,
        ((critical, host, key, host, key) for key, critical in criteria),
    )
    return max(0, cursor.rowcount)
