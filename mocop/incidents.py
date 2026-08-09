from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol

from .config import ThresholdConfig
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


class IncidentPolicy(Protocol):
    def conditions(self, result: ProbeResult) -> dict[str, IncidentCondition]: ...


class ThresholdIncidentPolicy:
    """Converts one probe result into stable, actionable incident conditions."""

    def __init__(self, thresholds: ThresholdConfig) -> None:
        self._thresholds = thresholds

    @staticmethod
    def _percentage(used: float, total: float) -> float:
        return round((used / total) * 100, 2) if total > 0 else 0.0

    @staticmethod
    def _severity(value: float, critical_at: float = 95) -> IncidentSeverity:
        return "critical" if value >= critical_at else "warning"

    def conditions(self, result: ProbeResult) -> dict[str, IncidentCondition]:
        if result.status != "online":
            return {
                "connectivity": IncidentCondition(
                    key="connectivity",
                    category="connectivity",
                    resource="SSH",
                    severity="critical",
                    value=None,
                    threshold=None,
                    observed_at=result.observed_at,
                    detail=result.message,
                )
            }

        conditions: dict[str, IncidentCondition] = {}
        system = result.system
        if system is not None:
            self._add_percentage(
                conditions,
                key="cpu",
                category="cpu",
                resource="CPU",
                value=system.cpu_usage_pct,
                threshold=self._thresholds.cpu_warning_pct,
                observed_at=result.observed_at,
            )
            self._add_percentage(
                conditions,
                key="memory",
                category="memory",
                resource="RAM",
                value=self._percentage(system.memory_used_mib, system.memory_total_mib),
                threshold=self._thresholds.memory_warning_pct,
                observed_at=result.observed_at,
            )
            if system.swap_total_mib > 0:
                self._add_percentage(
                    conditions,
                    key="swap",
                    category="swap",
                    resource="Swap",
                    value=self._percentage(system.swap_used_mib, system.swap_total_mib),
                    threshold=self._thresholds.swap_warning_pct,
                    observed_at=result.observed_at,
                    critical_at=90,
                )
            for disk in system.disks:
                self._add_percentage(
                    conditions,
                    key=f"disk:{disk.device}:{disk.mountpoint}",
                    category="disk",
                    resource=disk.mountpoint,
                    value=disk.used_pct,
                    threshold=self._thresholds.disk_warning_pct,
                    observed_at=result.observed_at,
                )

        for gpu in result.gpus:
            temperature = gpu.temperature_c
            threshold = self._thresholds.gpu_temperature_warning_c
            if temperature is None or temperature < threshold:
                continue
            identity = gpu.uuid or str(gpu.index)
            conditions[f"gpu_temperature:{identity}"] = IncidentCondition(
                key=f"gpu_temperature:{identity}",
                category="gpu_temperature",
                resource=f"GPU {gpu.index}",
                severity=self._severity(temperature, threshold + 5),
                value=round(float(temperature), 2),
                threshold=threshold,
                observed_at=result.observed_at,
            )
        return conditions

    def _add_percentage(
        self,
        conditions: dict[str, IncidentCondition],
        *,
        key: str,
        category: str,
        resource: str,
        value: float | None,
        threshold: float,
        observed_at: str,
        critical_at: float = 95,
    ) -> None:
        if value is None or value < threshold:
            return
        rounded = round(float(value), 2)
        conditions[key] = IncidentCondition(
            key=key,
            category=category,
            resource=resource,
            severity=self._severity(rounded, critical_at),
            value=rounded,
            threshold=threshold,
            observed_at=observed_at,
        )


class IncidentTracker:
    """Tracks active conditions and a bounded transition log.

    Callers provide synchronization. Resource conditions survive a failed probe so
    missing telemetry cannot be mistaken for recovery.
    """

    def __init__(self, policy: IncidentPolicy, history_points: int) -> None:
        self._policy = policy
        self._active: dict[str, dict[str, IncidentCondition]] = {}
        self._initialized: set[str] = set()
        self._events: deque[IncidentEvent] = deque(maxlen=history_points)
        self._version = 0
        self._next_event_id = 1

    @property
    def version(self) -> int:
        return self._version

    def remove_hosts(self, desired: set[str]) -> None:
        for host in set(self._active) - desired:
            del self._active[host]
            self._initialized.discard(host)

    def update(self, result: ProbeResult) -> None:
        previous = self._active.get(result.host, {})
        current = self._policy.conditions(result)
        if result.status != "online":
            current = {
                **{
                    key: condition
                    for key, condition in previous.items()
                    if key != "connectivity"
                },
                **current,
            }

        initialized = result.host in self._initialized
        if not initialized:
            self._initialized.add(result.host)
            self._active[result.host] = current
            if result.status != "online":
                for key in sorted(current):
                    self._append(
                        result.host, current[key], "opened", result.observed_at
                    )
            return

        previous_keys = set(previous)
        current_keys = set(current)
        for key in sorted(previous_keys - current_keys):
            self._append(result.host, previous[key], "resolved", result.observed_at)
        for key in sorted(current_keys - previous_keys):
            self._append(result.host, current[key], "opened", result.observed_at)
        for key in sorted(previous_keys & current_keys):
            old = previous[key]
            new = current[key]
            if old.severity == new.severity:
                continue
            state: IncidentState = (
                "escalated" if new.severity == "critical" else "deescalated"
            )
            self._append(result.host, new, state, result.observed_at)
        self._active[result.host] = current

    def snapshot(self, limit: int) -> dict[str, object]:
        active = [
            condition.active_dict(host)
            for host in sorted(self._active)
            for _, condition in sorted(self._active[host].items())
        ]
        active.sort(
            key=lambda item: (
                item["severity"] != "critical",
                str(item["host"]),
                str(item["conditionKey"]),
            )
        )
        events = list(self._events)
        return {
            "version": self._version,
            "active": active,
            "events": [event.to_dict() for event in reversed(events[-limit:])],
        }

    def counts(self) -> tuple[int, int]:
        conditions = [
            condition
            for active in self._active.values()
            for condition in active.values()
        ]
        return (
            len(conditions),
            sum(condition.severity == "critical" for condition in conditions),
        )

    def _append(
        self,
        host: str,
        condition: IncidentCondition,
        state: IncidentState,
        observed_at: str,
    ) -> None:
        self._events.append(
            IncidentEvent(
                event_id=self._next_event_id,
                host=host,
                condition=condition,
                state=state,
                observed_at=observed_at,
            )
        )
        self._next_event_id += 1
        self._version += 1
