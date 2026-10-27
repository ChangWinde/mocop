"""Incident transition decoding and generation-preserving retention."""

from __future__ import annotations

import sqlite3

from .incident_types import IncidentCondition, IncidentEvent
from .persistence_transitions import _is_optional_finite_number
from .persistence_validation import INCIDENT_SEVERITIES, INCIDENT_STATES


def legacy_critical_at(category: str, severity: str, threshold: object) -> float | None:
    """Recover the numeric policy line when a legacy event has no companion."""
    if severity != "critical":
        return None
    if category in {"cpu", "memory", "disk", "gpu_memory"}:
        return 95.0
    if category == "swap":
        return 90.0
    if isinstance(threshold, int | float):
        if category == "pressure":
            return min(100.0, threshold * 2)
        if category == "gpu_temperature":
            return threshold + 5
    return None


def prune_incidents(
    connection: sqlite3.Connection, cutoff: str, *, companions: bool = True
) -> None:
    connection.execute(
        """
        WITH latest AS (
            SELECT host, condition_key, max(event_id) AS id
            FROM incident_events GROUP BY host, condition_key
        ), active AS (
            SELECT latest.* FROM latest JOIN incident_events e ON e.event_id = id
            WHERE e.state != 'resolved'
        ), protected AS (
            SELECT id FROM active
            UNION
            SELECT max(e.event_id) FROM incident_events e JOIN active a
              ON e.host = a.host AND e.condition_key = a.condition_key
            WHERE e.state = 'opened' GROUP BY e.host, e.condition_key
        )
        DELETE FROM incident_events
        WHERE observed_at < ? AND event_id NOT IN (SELECT * FROM protected)
        """,
        (cutoff,),
    )
    if not companions:
        return
    connection.execute(
        "DELETE FROM incident_criteria WHERE event_id NOT IN "
        "(SELECT event_id FROM incident_events)"
    )


def event_from_row(row: tuple[object, ...]) -> IncidentEvent | None:
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
        critical_at,
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
        or not _is_optional_finite_number(critical_at)
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
            critical_at=(
                float(critical_at)
                if critical_at is not None
                else legacy_critical_at(category, severity, threshold)
            ),
        ),
        state=state,
        observed_at=observed_at,
    )
