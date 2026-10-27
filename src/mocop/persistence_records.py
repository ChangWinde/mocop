"""Immutable queue payloads for transactional telemetry writes."""

from __future__ import annotations

from dataclasses import dataclass

from .incident_types import IncidentEvent


@dataclass(frozen=True, slots=True)
class _HistoryWrite:
    host: str
    point: dict[str, object]


@dataclass(frozen=True, slots=True)
class _IncidentWrite:
    event: IncidentEvent


@dataclass(frozen=True, slots=True)
class _GpuTelemetryWrite:
    host: str
    points: tuple[dict[str, object], ...]
    process_events: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class _IncidentStateWrite:
    host: str
    events: tuple[IncidentEvent, ...]
    criteria: tuple[tuple[str, float | None], ...]


_Write = _HistoryWrite | _IncidentWrite | _GpuTelemetryWrite | _IncidentStateWrite
