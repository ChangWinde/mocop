"""The persistence contract the state store programs against.

``TelemetryPersistence`` is everything the store and the HTTP layer require
of a history backend; ``DisabledPersistence`` is the in-memory answer when
history is off, so callers never branch on a None backend.
"""

from __future__ import annotations

from typing import Protocol

from .incident_types import IncidentEvent
from .persistence_restore import LoadedTelemetry
from .reports import ReportInputs


class PersistenceError(RuntimeError):
    """Raised when explicitly enabled persistence cannot start safely."""


class TelemetryPersistence(Protocol):
    def is_enabled(self) -> bool: ...

    def load(self, history_points: int, incident_points: int) -> LoadedTelemetry: ...

    def report_inputs(self, since_hour: str) -> ReportInputs | None: ...

    def record_history(self, host: str, point: dict[str, object]) -> None: ...

    def record_incidents(self, events: tuple[IncidentEvent, ...]) -> None: ...

    def record_gpu_telemetry(
        self,
        host: str,
        points: tuple[dict[str, object], ...],
        process_events: tuple[dict[str, object], ...],
    ) -> None: ...

    def status(self) -> dict[str, object]: ...

    def close(self, timeout_seconds: float = 5.0) -> None: ...


class DisabledPersistence:
    def is_enabled(self) -> bool:
        return False

    def report_inputs(self, since_hour: str) -> ReportInputs | None:
        del since_hour
        return None

    def load(self, history_points: int, incident_points: int) -> LoadedTelemetry:
        del history_points, incident_points
        return LoadedTelemetry({}, ())

    def record_history(self, host: str, point: dict[str, object]) -> None:
        del host, point

    def record_incidents(self, events: tuple[IncidentEvent, ...]) -> None:
        del events

    def record_gpu_telemetry(
        self,
        host: str,
        points: tuple[dict[str, object], ...],
        process_events: tuple[dict[str, object], ...],
    ) -> None:
        del host, points, process_events

    def status(self) -> dict[str, object]:
        return {
            "enabled": False,
            "backend": "memory",
            "healthy": True,
            "queuedWrites": 0,
            "droppedWrites": 0,
            "lastError": None,
        }

    def close(self, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds
