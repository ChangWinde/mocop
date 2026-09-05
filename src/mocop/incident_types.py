"""The incident vocabulary shared by the tracker, persistence, and delivery.

A condition is what the policy derives from one sample; an event is a
transition of one condition on one host; an open incident is a condition
restored at startup; the policy protocol is everything the tracker and the
state store require of a policy implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .config import IncidentScopeOverrideConfig
from .models import ProbeResult

IncidentSeverity = Literal["warning", "critical"]
IncidentState = Literal["opened", "resolved", "escalated", "deescalated"]


@dataclass(frozen=True, slots=True)
class IncidentCondition:
    key: str
    category: str
    resource: str
    severity: IncidentSeverity
    value: float | None
    threshold: float | None
    observed_at: str
    detail: str | None = None
    open_after_cycles: int = 2
    recovery_cycles: int = 2
    group_key: str | None = None
    # Seconds the condition must have been observed for, on top of the cycle
    # count, before it opens or changes severity; 0 confirms by cycles alone.
    open_after_seconds: float = 0.0

    def active_dict(self, host: str) -> dict[str, object]:
        return {
            "host": host,
            "conditionKey": self.key,
            "category": self.category,
            "resource": self.resource,
            "severity": self.severity,
            "value": self.value,
            "threshold": self.threshold,
            "observedAt": self.observed_at,
            "detail": self.detail,
            "groupKey": self.group_key,
        }


@dataclass(frozen=True, slots=True)
class IncidentEvent:
    event_id: int
    host: str
    condition: IncidentCondition
    state: IncidentState
    observed_at: str

    def to_dict(self) -> dict[str, object]:
        value = self.condition.active_dict(self.host)
        value.update(
            {
                "eventId": self.event_id,
                "state": self.state,
                "observedAt": self.observed_at,
            }
        )
        return value


@dataclass(frozen=True, slots=True)
class OpenIncident:
    """A condition that was still active when the previous process stopped.

    ``condition`` carries the severity and values of its latest transition;
    ``opened_at`` is the generation's first observation, which the incident
    reports as ``firstObservedAt`` and incident actions are bound to.
    """

    host: str
    condition: IncidentCondition
    opened_at: str


class IncidentPolicy(Protocol):
    """Everything ``IncidentTracker`` and ``StateStore`` require of a policy."""

    def conditions(self, result: ProbeResult) -> dict[str, IncidentCondition]: ...

    def observed_domains(self, result: ProbeResult) -> frozenset[str]: ...

    def condition_observed(self, result: ProbeResult, key: str) -> bool: ...

    def update_expected_gpu_counts(
        self, expected_gpu_counts: tuple[tuple[str, int], ...]
    ) -> None: ...

    def update_overrides(
        self,
        host_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...],
        group_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...],
        host_groups: tuple[tuple[str, str], ...],
    ) -> None: ...

    def retain_hosts(self, hosts: set[str]) -> None: ...

    def recovery_cycles(self) -> int:
        """Healthy samples a restored condition needs before it resolves."""
        ...
