from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol

from .config import IncidentConfig, ThresholdConfig
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


class IncidentPolicy(Protocol):
    def conditions(self, result: ProbeResult) -> dict[str, IncidentCondition]: ...


class ThresholdIncidentPolicy:
    """Converts one probe result into stable, actionable incident conditions."""

    def __init__(
        self,
        thresholds: ThresholdConfig,
        expected_gpu_counts: tuple[tuple[str, int], ...] = (),
        incidents: IncidentConfig | None = None,
    ) -> None:
        self._thresholds = thresholds
        self._expected_gpu_counts = dict(expected_gpu_counts)
        self._incidents = incidents or IncidentConfig()

    def update_expected_gpu_counts(
        self, expected_gpu_counts: tuple[tuple[str, int], ...]
    ) -> None:
        self._expected_gpu_counts = dict(expected_gpu_counts)

    @staticmethod
    def _percentage(used: float, total: float) -> float:
        return round((used / total) * 100, 2) if total > 0 else 0.0

    @staticmethod
    def _severity(value: float, critical_at: float = 95) -> IncidentSeverity:
        return "critical" if value >= critical_at else "warning"

    @staticmethod
    def _network_disk_group_key(device: str, filesystem_type: str) -> str | None:
        filesystem = filesystem_type.lower()
        is_network = (
            filesystem in {"nfs", "nfs4", "cifs", "smb3", "sshfs", "ceph", "glusterfs"}
            or filesystem.startswith(("fuse.sshfs", "fuse.glusterfs"))
            or device.startswith("//")
            or (":" in device and not device.startswith("/"))
        )
        return f"{filesystem}|{device}" if is_network else None

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
                    open_after_cycles=1,
                    recovery_cycles=self._incidents.recovery_cycles,
                )
            }

        conditions: dict[str, IncidentCondition] = {}
        gpu_query_failed = result.message in {
            "nvidia-smi is unavailable",
            "nvidia-smi query failed",
        }
        if gpu_query_failed:
            conditions["gpu_availability"] = IncidentCondition(
                key="gpu_availability",
                category="gpu_availability",
                resource="NVIDIA telemetry",
                severity="critical",
                value=None,
                threshold=None,
                observed_at=result.observed_at,
                detail=result.message,
                open_after_cycles=1,
                recovery_cycles=self._incidents.recovery_cycles,
            )
        expected_count = self._expected_gpu_counts.get(result.host)
        if (
            not gpu_query_failed
            and expected_count is not None
            and len(result.gpus) != expected_count
        ):
            conditions["gpu_count"] = IncidentCondition(
                key="gpu_count",
                category="gpu_count",
                resource="GPU inventory",
                severity="critical" if len(result.gpus) < expected_count else "warning",
                value=float(len(result.gpus)),
                threshold=float(expected_count),
                observed_at=result.observed_at,
                detail=f"Expected {expected_count} GPUs; observed {len(result.gpus)}",
                open_after_cycles=1,
                recovery_cycles=self._incidents.recovery_cycles,
            )
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
                    group_key=self._network_disk_group_key(
                        disk.device, disk.filesystem_type
                    ),
                )

        if result.gpus and any(not gpu.processes_available for gpu in result.gpus):
            conditions["gpu_processes"] = IncidentCondition(
                key="gpu_processes",
                category="gpu_processes",
                resource="GPU processes",
                severity="warning",
                value=None,
                threshold=None,
                observed_at=result.observed_at,
                detail="GPU process telemetry is unavailable",
                open_after_cycles=self._incidents.resource_open_cycles,
                recovery_cycles=self._incidents.recovery_cycles,
            )

        for gpu in result.gpus:
            identity = gpu.uuid or str(gpu.index)
            temperature = gpu.temperature_c
            threshold = self._thresholds.gpu_temperature_warning_c
            if temperature is not None and temperature >= threshold:
                conditions[f"gpu_temperature:{identity}"] = IncidentCondition(
                    key=f"gpu_temperature:{identity}",
                    category="gpu_temperature",
                    resource=f"GPU {gpu.index}",
                    severity=self._severity(temperature, threshold + 5),
                    value=round(float(temperature), 2),
                    threshold=threshold,
                    observed_at=result.observed_at,
                    open_after_cycles=self._incidents.resource_open_cycles,
                    recovery_cycles=self._incidents.recovery_cycles,
                )

            memory_pct = self._percentage(
                gpu.memory_used_mib or 0, gpu.memory_total_mib or 0
            )
            if memory_pct >= self._thresholds.gpu_memory_warning_pct:
                key = f"gpu_memory:{identity}"
                conditions[key] = IncidentCondition(
                    key=key,
                    category="gpu_memory",
                    resource=f"GPU {gpu.index} VRAM",
                    severity=self._severity(memory_pct),
                    value=memory_pct,
                    threshold=self._thresholds.gpu_memory_warning_pct,
                    observed_at=result.observed_at,
                    open_after_cycles=self._incidents.resource_open_cycles,
                    recovery_cycles=self._incidents.recovery_cycles,
                )
            utilization = gpu.utilization_gpu_pct
            if (
                memory_pct >= self._thresholds.gpu_idle_memory_pct
                and utilization is not None
                and utilization < self._thresholds.gpu_busy_pct
                and (gpu.memory_used_mib or 0) > 0
            ):
                key = f"gpu_idle_memory:{identity}"
                conditions[key] = IncidentCondition(
                    key=key,
                    category="gpu_idle_memory",
                    resource=f"GPU {gpu.index} VRAM",
                    severity="warning",
                    value=memory_pct,
                    threshold=self._thresholds.gpu_idle_memory_pct,
                    observed_at=result.observed_at,
                    detail=f"GPU utilization is {round(utilization, 2)}%",
                    open_after_cycles=self._incidents.gpu_idle_memory_cycles,
                    recovery_cycles=self._incidents.recovery_cycles,
                )
            health = gpu.health
            if health is None:
                continue
            if (health.ecc_uncorrected_volatile or 0) > 0:
                key = f"gpu_ecc:{identity}"
                conditions[key] = IncidentCondition(
                    key=key,
                    category="gpu_ecc",
                    resource=f"GPU {gpu.index} ECC",
                    severity="critical",
                    value=float(health.ecc_uncorrected_volatile or 0),
                    threshold=0,
                    observed_at=result.observed_at,
                    detail="Volatile uncorrected ECC errors detected",
                    open_after_cycles=self._incidents.resource_open_cycles,
                    recovery_cycles=self._incidents.recovery_cycles,
                )
            if health.retired_pages_pending or health.remapped_rows_pending:
                key = f"gpu_memory_repair:{identity}"
                conditions[key] = IncidentCondition(
                    key=key,
                    category="gpu_memory_repair",
                    resource=f"GPU {gpu.index} memory",
                    severity="critical",
                    value=None,
                    threshold=None,
                    observed_at=result.observed_at,
                    detail="GPU memory repair is pending",
                    open_after_cycles=self._incidents.resource_open_cycles,
                    recovery_cycles=self._incidents.recovery_cycles,
                )
            if health.thermal_slowdown or health.power_brake_slowdown:
                key = f"gpu_slowdown:{identity}"
                causes = []
                if health.thermal_slowdown:
                    causes.append("thermal")
                if health.power_brake_slowdown:
                    causes.append("power brake")
                conditions[key] = IncidentCondition(
                    key=key,
                    category="gpu_slowdown",
                    resource=f"GPU {gpu.index}",
                    severity="critical" if health.thermal_slowdown else "warning",
                    value=None,
                    threshold=None,
                    observed_at=result.observed_at,
                    detail=f"Hardware slowdown active: {', '.join(causes)}",
                    open_after_cycles=self._incidents.resource_open_cycles,
                    recovery_cycles=self._incidents.recovery_cycles,
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
        group_key: str | None = None,
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
            open_after_cycles=self._incidents.resource_open_cycles,
            recovery_cycles=self._incidents.recovery_cycles,
            group_key=group_key,
        )


class IncidentTracker:
    """Tracks active conditions and a bounded transition log.

    Callers provide synchronization. Resource conditions survive a failed probe so
    missing telemetry cannot be mistaken for recovery.
    """

    def __init__(self, policy: IncidentPolicy, history_points: int) -> None:
        self._policy = policy
        self._active: dict[str, dict[str, IncidentCondition]] = {}
        self._candidates: dict[str, dict[str, tuple[IncidentCondition, int]]] = {}
        self._recoveries: dict[str, dict[str, int]] = {}
        self._initialized: set[str] = set()
        self._events: deque[IncidentEvent] = deque(maxlen=history_points)
        self._version = 0
        self._next_event_id = 1

    @property
    def version(self) -> int:
        return self._version

    def remove_hosts(self, desired: set[str]) -> None:
        for host in set(self._active) - desired:
            had_active_conditions = bool(self._active[host])
            del self._active[host]
            self._candidates.pop(host, None)
            self._recoveries.pop(host, None)
            self._initialized.discard(host)
            if had_active_conditions:
                self._version += 1

    def update(self, result: ProbeResult) -> None:
        previous = self._active.get(result.host, {})
        observed = self._policy.conditions(result)

        initialized = result.host in self._initialized
        if not initialized:
            self._initialized.add(result.host)
            immediate = {
                key: condition
                for key, condition in observed.items()
                if condition.open_after_cycles == 1
            }
            self._active[result.host] = immediate
            self._candidates[result.host] = {
                key: (condition, 1)
                for key, condition in observed.items()
                if condition.open_after_cycles > 1
            }
            self._recoveries[result.host] = {}
            if result.status != "online":
                for key in sorted(immediate):
                    self._append(
                        result.host, immediate[key], "opened", result.observed_at
                    )
            elif immediate:
                # Initial conditions are visible without fabricating historical events.
                self._version += 1
            return

        candidates = self._candidates.setdefault(result.host, {})
        recoveries = self._recoveries.setdefault(result.host, {})
        current = dict(previous)
        if result.status == "online":
            for key in set(candidates) - set(observed):
                del candidates[key]

        for key, new in observed.items():
            recoveries.pop(key, None)
            old = previous.get(key)
            if old is not None:
                candidates.pop(key, None)
                if old.severity != new.severity:
                    state: IncidentState = (
                        "escalated" if new.severity == "critical" else "deescalated"
                    )
                    self._append(result.host, new, state, result.observed_at)
                    current[key] = new
                continue

            candidate, count = candidates.get(key, (new, 0))
            if candidate.severity != new.severity:
                count = 0
            count += 1
            if count >= new.open_after_cycles:
                candidates.pop(key, None)
                current[key] = new
                self._append(result.host, new, "opened", result.observed_at)
            else:
                candidates[key] = (new, count)

        if result.status == "online":
            for key in set(previous) - set(observed):
                old = previous[key]
                recovery_count = recoveries.get(key, 0) + 1
                if recovery_count >= old.recovery_cycles:
                    recoveries.pop(key, None)
                    current.pop(key, None)
                    self._append(result.host, old, "resolved", result.observed_at)
                else:
                    recoveries[key] = recovery_count

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

    def counts(self) -> tuple[int, int, frozenset[str]]:
        conditions = [
            condition
            for active in self._active.values()
            for condition in active.values()
        ]
        return (
            len(conditions),
            sum(condition.severity == "critical" for condition in conditions),
            frozenset(host for host, active in self._active.items() if active),
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
