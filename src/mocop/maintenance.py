"""Time-bounded maintenance windows and their recurrence arithmetic.

A window silences a host's incidents without touching collection or condition
state (ADR-0007). One-shot windows end at an absolute UTC instant; weekly
windows are defined in UTC and bounded below one week so instances can never
overlap themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True, slots=True)
class MaintenanceWindowConfig:
    """One-shot (absolute `until`) or weekly recurring silence window.

    Recurring windows are defined in UTC: `weekday` follows Python's Monday=0
    convention, `start_minutes` counts from UTC midnight, and the duration is
    bounded below one week so instances can never overlap themselves.
    """

    reason: str
    until: datetime | None = None
    weekday: int | None = None
    start_minutes: int | None = None
    duration_minutes: int | None = None

    @property
    def recurring(self) -> bool:
        return self.weekday is not None

    def _instance_end(self, now: datetime) -> datetime:
        """Return the end of the active instance, or of the next one."""
        assert self.weekday is not None
        assert self.start_minutes is not None
        assert self.duration_minutes is not None
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days_back = (now.weekday() - self.weekday) % 7
        start = (
            midnight - timedelta(days=days_back) + timedelta(minutes=self.start_minutes)
        )
        if start > now:
            start -= timedelta(days=7)
        end = start + timedelta(minutes=self.duration_minutes)
        if end <= now:
            end = start + timedelta(days=7, minutes=self.duration_minutes)
        return end

    def is_active(self, at: datetime | None = None) -> bool:
        now = at or datetime.now(timezone.utc)
        if not self.recurring:
            assert self.until is not None
            return self.until > now
        assert self.duration_minutes is not None
        end = self._instance_end(now)
        return end - timedelta(minutes=self.duration_minutes) <= now < end

    def to_dict(self, at: datetime | None = None) -> dict[str, object]:
        if not self.recurring:
            assert self.until is not None
            return {
                "until": self.until.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
                "reason": self.reason,
            }
        now = at or datetime.now(timezone.utc)
        end = self._instance_end(now)
        return {
            "until": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "reason": self.reason,
            "recurring": True,
        }
