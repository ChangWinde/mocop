"""Writer-owned, transactionally published cache of open incident criteria."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from .persistence_records import _IncidentStateWrite, _IncidentWrite, _Write

CriteriaCache = dict[tuple[str, str], float | None]

LATEST_CRITERION_SQL = """
    SELECT e.state, c.event_id, c.critical_at
    FROM incident_events e LEFT JOIN incident_criteria c ON c.event_id = e.event_id
    WHERE e.host = ? AND e.condition_key = ?
    ORDER BY e.event_id DESC LIMIT 1
"""


def prepare_incident_write(
    connection: sqlite3.Connection, item: _Write, cache: CriteriaCache
) -> _Write:
    """Mutate only a batch-local cache; the caller publishes it after commit."""
    if isinstance(item, _IncidentWrite):
        events = (item.event,)
    elif isinstance(item, _IncidentStateWrite):
        events = item.events
    else:
        return item
    resolved = set()
    for event in events:
        identity = (event.host, event.condition.key)
        if event.state == "resolved":
            cache.pop(identity, None)
            resolved.add(identity)
        else:
            cache[identity] = event.condition.critical_at
            resolved.discard(identity)
    if isinstance(item, _IncidentWrite):
        return item
    changed = []
    for key, critical in item.criteria:
        identity = (item.host, key)
        if identity in resolved:
            continue
        if identity not in cache:
            # Hydrate a restored condition once. Missing metadata must still
            # be written, even when its intended value is None.
            row = connection.execute(LATEST_CRITERION_SQL, identity).fetchone()
            if row is None or row[0] == "resolved":
                continue
            if row[1] is not None:
                cache[identity] = row[2]
        if identity not in cache or cache[identity] != critical:
            changed.append((key, critical))
            cache[identity] = critical
    return replace(item, criteria=tuple(changed))
