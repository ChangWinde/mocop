"""Pure rules the incident tracker applies, kept out of its lock-owned state.

Each function reads only its arguments: the duration floor that confirms a
condition, the hold that keeps an established critical severity inside the
recovery margin, the lowest transition id a newly started tracker may assign,
and whether restored transitions show that an incident generation ended.
"""

from __future__ import annotations

from datetime import datetime

from .incident_types import IncidentCondition, IncidentEvent
from .models import epoch_seconds


def holds_severity(new: IncidentCondition, previous: IncidentCondition) -> bool:
    """Whether an established critical severity survives a warning reading.

    The reading must sit inside the recovery margin below the critical line,
    and the escalation criterion must be unchanged: a filesystem that was
    critical for lack of absolute headroom, not percentage, de-escalates by
    the usual confirmation once headroom returns.
    """
    return (
        previous.severity == "critical"
        and new.severity == "warning"
        and new.value is not None
        and new.critical_at is not None
        and previous.critical_at == new.critical_at
        and new.value >= new.critical_at - new.recovery_margin
    )


def sustained(since: str, condition: IncidentCondition) -> bool:
    """Whether the condition has been observed for its required duration.

    Unparsable timestamps fall back to confirmation by cycles alone.
    """
    if condition.open_after_seconds <= 0:
        return True
    started = epoch_seconds(since)
    now = epoch_seconds(condition.observed_at)
    if started is None or now is None:
        return True
    return now - started >= condition.open_after_seconds


def event_id_floor(started_at: datetime) -> int:
    """Lowest transition id a tracker started at ``started_at`` may assign.

    Webhook receivers deduplicate on ``eventId``, so ids must not repeat
    across restarts, including restarts without history persistence and
    restarts after the newest transitions were never written. Unix
    milliseconds at startup exceed every id an earlier run assigned unless
    that run assigned more ids than milliseconds have passed since it
    started, or the wall clock moved backwards between the two starts.
    """
    return int(started_at.timestamp() * 1000)


def generation_ended(
    events: tuple[IncidentEvent, ...], host: str, key: str, started_at: str
) -> bool:
    """Whether restored transitions show the generation begun at ``started_at``
    resolved: a condition has one open generation at a time, so any resolve
    at or after its start ends it. Unknown history (none retained) is False.
    """
    start = epoch_seconds(started_at)
    return start is not None and any(
        event.host == host
        and event.condition.key == key
        and event.state == "resolved"
        and (epoch_seconds(event.observed_at) or float("-inf")) >= start
        for event in events
    )
