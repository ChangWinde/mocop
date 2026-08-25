from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from .config import IncidentConfig, IncidentScopeOverrideConfig, ThresholdConfig
from .models import ProbeResult

IncidentSeverity = Literal["warning", "critical"]
IncidentState = Literal["opened", "resolved", "escalated", "deescalated"]

_GPU_QUERY_FAILURE_MESSAGES = frozenset(
    {"nvidia-smi is unavailable", "nvidia-smi query failed"}
)
_SYSTEM_CATEGORIES = frozenset({"cpu", "memory", "swap", "disk", "pressure"})
_GPU_HEALTH_CATEGORIES = frozenset({"gpu_ecc", "gpu_memory_repair", "gpu_slowdown"})


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
        host_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...] = (),
        group_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...] = (),
        host_groups: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._threshold_values = thresholds.to_dict()
        self._expected_gpu_counts = dict(expected_gpu_counts)
        self._incidents = incidents or IncidentConfig()
        self._host_overrides = dict(host_overrides)
        self._group_overrides = dict(group_overrides)
        self._host_groups = dict(host_groups)
        self._effective_thresholds: dict[str, dict[str, float]] = {}
        self._effective_disk_exclusions: dict[str, frozenset[str]] = {}

    def update_expected_gpu_counts(
        self, expected_gpu_counts: tuple[tuple[str, int], ...]
    ) -> None:
        self._expected_gpu_counts = dict(expected_gpu_counts)

    def update_overrides(
        self,
        host_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...],
        group_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...],
        host_groups: tuple[tuple[str, str], ...],
    ) -> None:
        self._host_overrides = dict(host_overrides)
        self._group_overrides = dict(group_overrides)
        self._host_groups = dict(host_groups)
        self._effective_thresholds.clear()
        self._effective_disk_exclusions.clear()

    def retain_hosts(self, hosts: set[str]) -> None:
        if (
            self._effective_thresholds.keys() <= hosts
            and self._effective_disk_exclusions.keys() <= hosts
        ):
            return
        self._effective_thresholds = {
            host: values
            for host, values in self._effective_thresholds.items()
            if host in hosts
        }
        self._effective_disk_exclusions = {
            host: mounts
            for host, mounts in self._effective_disk_exclusions.items()
            if host in hosts
        }

    def _thresholds_for(self, host: str) -> dict[str, float]:
        cached = self._effective_thresholds.get(host)
        if cached is not None:
            return cached
        values = dict(self._threshold_values)
        group = self._host_groups.get(host)
        group_override = self._group_overrides.get(group) if group else None
        if group_override is not None:
            values.update(group_override.thresholds)
        host_override = self._host_overrides.get(host)
        if host_override is not None:
            values.update(host_override.thresholds)
        self._effective_thresholds[host] = values
        return values

    def _excluded_disk_mounts(self, host: str) -> frozenset[str]:
        cached = self._effective_disk_exclusions.get(host)
        if cached is not None:
            return cached
        excluded: set[str] = set()
        group = self._host_groups.get(host)
        if group and group in self._group_overrides:
            excluded.update(self._group_overrides[group].exclude_disk_mounts)
        if host in self._host_overrides:
            excluded.update(self._host_overrides[host].exclude_disk_mounts)
        result = frozenset(excluded)
        self._effective_disk_exclusions[host] = result
        return result

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
        thresholds = self._thresholds_for(result.host)

        def threshold(name: str) -> float:
            return thresholds[name]

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
        gpu_query_failed = result.message in _GPU_QUERY_FAILURE_MESSAGES
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
                threshold=threshold("cpu_warning_pct"),
                observed_at=result.observed_at,
            )
            self._add_percentage(
                conditions,
                key="memory",
                category="memory",
                resource="RAM",
                value=self._percentage(system.memory_used_mib, system.memory_total_mib),
                threshold=threshold("memory_warning_pct"),
                observed_at=result.observed_at,
            )
            if system.swap_total_mib > 0:
                self._add_percentage(
                    conditions,
                    key="swap",
                    category="swap",
                    resource="Swap",
                    value=self._percentage(system.swap_used_mib, system.swap_total_mib),
                    threshold=threshold("swap_warning_pct"),
                    observed_at=result.observed_at,
                    critical_at=90,
                )
            pressure = system.pressure
            if pressure is not None:
                # Utilization alone cannot see reclaim or I/O stalls, so the
                # PSI "some avg60" window alerts on sustained task stalls; CPU
                # pressure is intentionally excluded because the load averages
                # already cover runnable-queue contention.
                for psi_resource, sample, threshold_name, label in (
                    ("memory", pressure.memory, "psi_memory_some_pct", "Memory"),
                    ("io", pressure.io, "psi_io_some_pct", "I/O"),
                ):
                    if sample is None:
                        continue
                    psi_threshold = threshold(threshold_name)
                    self._add_percentage(
                        conditions,
                        key=f"pressure:{psi_resource}",
                        category="pressure",
                        resource=f"{label} pressure",
                        value=sample.some_avg60,
                        threshold=psi_threshold,
                        observed_at=result.observed_at,
                        critical_at=min(100.0, psi_threshold * 2),
                        detail=(
                            f"Tasks stalled on {psi_resource} for "
                            f"{round(sample.some_avg60, 2)}% of the last minute"
                        ),
                    )
            minimum_free_mib = threshold("disk_min_free_gib") * 1024
            for disk in system.disks:
                if disk.mountpoint in self._excluded_disk_mounts(result.host):
                    continue
                # Percentage decides whether a filesystem alerts at all; the
                # absolute headroom decides how urgent it is. Escalation is
                # gated on already being over the percentage threshold so a
                # small, mostly empty partition such as /boot/efi stays quiet.
                starved = disk.available_mib < minimum_free_mib
                self._add_percentage(
                    conditions,
                    key=f"disk:{disk.device}:{disk.mountpoint}",
                    category="disk",
                    resource=disk.mountpoint,
                    value=disk.used_pct,
                    threshold=threshold("disk_warning_pct"),
                    observed_at=result.observed_at,
                    critical_at=0 if starved else 95,
                    group_key=self._network_disk_group_key(
                        disk.device, disk.filesystem_type
                    ),
                )

        if result.gpus and any(
            gpu.processes_sampled and not gpu.processes_available for gpu in result.gpus
        ):
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
            temperature_threshold = threshold("gpu_temperature_warning_c")
            if temperature is not None and temperature >= temperature_threshold:
                conditions[f"gpu_temperature:{identity}"] = IncidentCondition(
                    key=f"gpu_temperature:{identity}",
                    category="gpu_temperature",
                    resource=f"GPU {gpu.index}",
                    severity=self._severity(temperature, temperature_threshold + 5),
                    value=round(float(temperature), 2),
                    threshold=temperature_threshold,
                    observed_at=result.observed_at,
                    open_after_cycles=self._incidents.resource_open_cycles,
                    recovery_cycles=self._incidents.recovery_cycles,
                )

            memory_pct = (
                self._percentage(gpu.memory_used_mib, gpu.memory_total_mib)
                if gpu.memory_used_mib is not None
                and gpu.memory_total_mib is not None
                and gpu.memory_total_mib > 0
                else None
            )
            memory_threshold = threshold("gpu_memory_warning_pct")
            if memory_pct is not None and memory_pct >= memory_threshold:
                key = f"gpu_memory:{identity}"
                conditions[key] = IncidentCondition(
                    key=key,
                    category="gpu_memory",
                    resource=f"GPU {gpu.index} VRAM",
                    severity=self._severity(memory_pct),
                    value=memory_pct,
                    threshold=memory_threshold,
                    observed_at=result.observed_at,
                    open_after_cycles=self._incidents.resource_open_cycles,
                    recovery_cycles=self._incidents.recovery_cycles,
                )
            utilization = gpu.utilization_gpu_pct
            if (
                memory_pct is not None
                and memory_pct >= threshold("gpu_idle_memory_pct")
                and utilization is not None
                and utilization < threshold("gpu_busy_pct")
                and (gpu.memory_used_mib or 0) > 0
            ):
                key = f"gpu_idle_memory:{identity}"
                conditions[key] = IncidentCondition(
                    key=key,
                    category="gpu_idle_memory",
                    resource=f"GPU {gpu.index} VRAM",
                    severity="warning",
                    value=memory_pct,
                    threshold=threshold("gpu_idle_memory_pct"),
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

    def observed_domains(self, result: ProbeResult) -> frozenset[str]:
        """Telemetry domains for which this sample carries fresh, valid data.

        The tracker only advances recovery for conditions whose domains were
        all observed. Unknown domains freeze recovery instead of feigning
        health: a failed GPU query still reports the host online, but says
        nothing about GPU temperature, memory, health, or processes.
        """
        if result.status != "online":
            return frozenset()
        domains: set[str] = set()
        if result.system is not None:
            domains.add("system")
            if result.system.cpu_usage_pct is not None:
                domains.add("system_cpu")
            if result.system.pressure is not None:
                if result.system.pressure.memory is not None:
                    domains.add("pressure:memory")
                if result.system.pressure.io is not None:
                    domains.add("pressure:io")
        if result.message not in _GPU_QUERY_FAILURE_MESSAGES:
            domains.add("gpu_query")
            domains.add("gpu_inventory")
            if all(gpu.processes_sampled for gpu in result.gpus):
                domains.add("gpu_processes")
            for gpu in result.gpus:
                identity = gpu.uuid or str(gpu.index)
                domains.add(f"gpu_present:{identity}")
                if gpu.temperature_c is not None:
                    domains.add(f"gpu_temperature:{identity}")
                if (
                    gpu.memory_used_mib is not None
                    and gpu.memory_total_mib is not None
                    and gpu.memory_total_mib > 0
                ):
                    domains.add(f"gpu_memory:{identity}")
                if gpu.utilization_gpu_pct is not None:
                    domains.add(f"gpu_utilization:{identity}")
                if gpu.health is not None:
                    domains.add(f"gpu_health:{identity}")
        return frozenset(domains)

    def condition_observed(self, result: ProbeResult, key: str) -> bool:
        """Whether this sample can authoritatively declare ``key`` absent."""
        if key == "connectivity":
            return True
        category = key.partition(":")[0]
        domains = self.observed_domains(result)
        return all(
            domain in domains
            for domain in _condition_domains_for_category(category, key)
        )

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
        detail: str | None = None,
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
            detail=detail,
            open_after_cycles=self._incidents.resource_open_cycles,
            recovery_cycles=self._incidents.recovery_cycles,
            group_key=group_key,
        )


def _condition_domains(condition: IncidentCondition, key: str) -> tuple[str, ...]:
    """Telemetry domains a condition needs before its recovery may advance."""
    return _condition_domains_for_category(condition.category, key)


def _condition_domains_for_category(category: str, key: str) -> tuple[str, ...]:
    if category == "cpu":
        return ("system", "system_cpu")
    if category == "pressure":
        return ("system", key)
    if category in _SYSTEM_CATEGORIES:
        return ("system",)
    if category in {"gpu_availability", "gpu_count"}:
        return ("gpu_query",)
    identity = key.partition(":")[2]
    if category in _GPU_HEALTH_CATEGORIES:
        return (f"gpu_present:{identity}", f"gpu_health:{identity}")
    if category == "gpu_processes":
        return ("gpu_processes",)
    if category == "gpu_temperature":
        return (f"gpu_present:{identity}", f"gpu_temperature:{identity}")
    if category == "gpu_memory":
        return (f"gpu_present:{identity}", f"gpu_memory:{identity}")
    if category == "gpu_idle_memory":
        return (
            f"gpu_present:{identity}",
            f"gpu_memory:{identity}",
            f"gpu_utilization:{identity}",
        )
    return ()


_PER_IDENTITY_GPU_DOMAINS = frozenset(
    {"gpu_present", "gpu_health", "gpu_temperature", "gpu_memory", "gpu_utilization"}
)


def _telemetry_unknown(
    condition: IncidentCondition,
    key: str,
    observed_domains: frozenset[str] | None,
) -> bool:
    """True when the sample carried no fresh telemetry for this condition."""
    if observed_domains is None:
        return False
    missing = [
        domain
        for domain in _condition_domains(condition, key)
        if domain not in observed_domains
    ]
    if not missing:
        return False
    # A fully observed GPU inventory is authoritative about absence: when a
    # device identity has left a complete inventory (a replaced or renumbered
    # card), its per-identity domains can never be observed again. Freezing
    # would pin the ghost condition and its counts forever, so recovery may
    # advance instead. A failed GPU query never reaches this branch because
    # it does not observe ``gpu_inventory``.
    if "gpu_inventory" in observed_domains:
        identities = set()
        for domain in missing:
            prefix, _, identity = domain.partition(":")
            if prefix not in _PER_IDENTITY_GPU_DOMAINS:
                return True
            identities.add(identity)
        if all(
            f"gpu_present:{identity}" not in observed_domains for identity in identities
        ):
            return False
    return True


class IncidentTracker:
    """Tracks active conditions and a bounded transition log.

    Callers provide synchronization. Resource conditions survive a failed probe
    and telemetry domains the policy reports as unobserved, so missing
    telemetry cannot be mistaken for recovery.
    """

    def __init__(
        self,
        policy: IncidentPolicy,
        history_points: int,
        historical_events: tuple[IncidentEvent, ...] = (),
    ) -> None:
        self._policy = policy
        self._active: dict[str, dict[str, IncidentCondition]] = {}
        self._candidates: dict[str, dict[str, tuple[IncidentCondition, int]]] = {}
        self._recoveries: dict[str, dict[str, int]] = {}
        self._severity_changes: dict[str, dict[str, tuple[IncidentSeverity, int]]] = {}
        self._opened_at: dict[str, dict[str, str]] = {}
        self._last_observed_at: dict[str, dict[str, str]] = {}
        self._initialized: set[str] = set()
        retained_events = historical_events[-history_points:]
        self._events: deque[IncidentEvent] = deque(
            retained_events, maxlen=history_points
        )
        last_event_id = max((event.event_id for event in retained_events), default=0)
        self._version = last_event_id
        self._next_event_id = last_event_id + 1

    @property
    def version(self) -> int:
        return self._version

    def remove_hosts(self, desired: set[str]) -> None:
        for host in set(self._active) - desired:
            had_active_conditions = bool(self._active[host])
            del self._active[host]
            self._candidates.pop(host, None)
            self._recoveries.pop(host, None)
            self._severity_changes.pop(host, None)
            self._opened_at.pop(host, None)
            self._last_observed_at.pop(host, None)
            self._initialized.discard(host)
            if had_active_conditions:
                self._version += 1

    def update(self, result: ProbeResult) -> tuple[IncidentEvent, ...]:
        created: list[IncidentEvent] = []
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
            self._severity_changes[result.host] = {}
            self._opened_at[result.host] = {
                key: condition.observed_at for key, condition in immediate.items()
            }
            self._last_observed_at[result.host] = {
                key: condition.observed_at for key, condition in immediate.items()
            }
            # First activations are real transitions even on an online sample;
            # without an opened event they would never persist or notify.
            for key in sorted(immediate):
                created.append(
                    self._append(
                        result.host, immediate[key], "opened", result.observed_at
                    )
                )
            return tuple(created)

        candidates = self._candidates[result.host]
        recoveries = self._recoveries[result.host]
        severity_changes = self._severity_changes[result.host]
        opened_at = self._opened_at[result.host]
        last_observed_at = self._last_observed_at[result.host]
        current = dict(previous)
        observed_domains = self._observed_domains(result)
        # Opening a condition requires consecutively confirmable samples: an
        # unreachable probe or a telemetry blind spot breaks the confirmation
        # chain and discards the candidate, so two isolated spikes separated
        # by an arbitrarily long blind gap can never add up to an opened
        # incident. Active conditions keep their documented freeze semantics
        # in the recovery loop below.
        for key in set(candidates) - set(observed):
            del candidates[key]

        for key, new in observed.items():
            recoveries.pop(key, None)
            old = previous.get(key)
            if old is not None:
                candidates.pop(key, None)
                last_observed_at[key] = new.observed_at
                if old.severity == new.severity:
                    severity_changes.pop(key, None)
                    current[key] = new
                    continue
                pending_severity, count = severity_changes.get(key, (new.severity, 0))
                if pending_severity != new.severity:
                    count = 0
                count += 1
                if count < new.open_after_cycles:
                    # Debounce severity flapping: keep the confirmed severity
                    # until the change is sustained, mirroring open cycles.
                    severity_changes[key] = (new.severity, count)
                    current[key] = replace(new, severity=old.severity)
                    continue
                severity_changes.pop(key, None)
                current[key] = new
                state: IncidentState = (
                    "escalated" if new.severity == "critical" else "deescalated"
                )
                created.append(
                    self._append(result.host, new, state, result.observed_at)
                )
                continue

            candidate, count = candidates.get(key, (new, 0))
            if candidate.severity != new.severity:
                count = 0
            count += 1
            if count >= new.open_after_cycles:
                candidates.pop(key, None)
                current[key] = new
                opened_at[key] = new.observed_at
                last_observed_at[key] = new.observed_at
                created.append(
                    self._append(result.host, new, "opened", result.observed_at)
                )
            else:
                candidates[key] = (new, count)

        if result.status == "online":
            for key in set(previous) - set(observed):
                old = previous[key]
                if _telemetry_unknown(old, key, observed_domains):
                    # The sample carried no fresh data for this domain, so it
                    # can neither advance nor reset the recovery count.
                    continue
                severity_changes.pop(key, None)
                recovery_count = recoveries.get(key, 0) + 1
                if recovery_count >= old.recovery_cycles:
                    recoveries.pop(key, None)
                    current.pop(key, None)
                    opened_at.pop(key, None)
                    last_observed_at.pop(key, None)
                    created.append(
                        self._append(result.host, old, "resolved", result.observed_at)
                    )
                else:
                    recoveries[key] = recovery_count

        self._active[result.host] = current
        return tuple(created)

    def _observed_domains(self, result: ProbeResult) -> frozenset[str] | None:
        """None means the policy predates domain reporting: treat as observed."""
        observed_domains = getattr(self._policy, "observed_domains", None)
        if observed_domains is None:
            return None
        return frozenset(observed_domains(result))

    def snapshot(self, limit: int) -> dict[str, object]:
        active = []
        for host in sorted(self._active):
            for key, condition in sorted(self._active[host].items()):
                item = condition.active_dict(host)
                item["firstObservedAt"] = self._opened_at.get(host, {}).get(
                    key, condition.observed_at
                )
                item["lastObservedAt"] = self._last_observed_at.get(host, {}).get(
                    key, condition.observed_at
                )
                active.append(item)
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

    def counts(
        self, excluded_hosts: frozenset[str] = frozenset()
    ) -> tuple[int, int, frozenset[str]]:
        conditions = [
            condition
            for host, active in self._active.items()
            if host not in excluded_hosts
            for condition in active.values()
        ]
        return (
            len(conditions),
            sum(condition.severity == "critical" for condition in conditions),
            frozenset(
                host
                for host, active in self._active.items()
                if active and host not in excluded_hosts
            ),
        )

    def counts_by_host(self) -> dict[str, tuple[int, int]]:
        return {
            host: (
                len(active),
                sum(condition.severity == "critical" for condition in active.values()),
            )
            for host, active in self._active.items()
            if active
        }

    def has_active(self, host: str) -> bool:
        return bool(self._active.get(host))

    def has_active_condition(self, host: str, condition_key: str) -> bool:
        """Return whether one exact condition is active for the host instance."""
        return condition_key in self._active.get(host, {})

    def has_pending_condition(self, host: str, condition_key: str) -> bool:
        """Return whether live telemetry is still confirming this condition."""
        return condition_key in self._candidates.get(host, {})

    def active_started_at(self, host: str, condition_key: str) -> str | None:
        if condition_key not in self._active.get(host, {}):
            return None
        return self._opened_at.get(host, {}).get(condition_key)

    def active_signature(self, host: str) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                key,
                condition.severity,
                condition.value,
                condition.threshold,
                condition.detail,
                condition.resource,
            )
            for key, condition in sorted(self._active.get(host, {}).items())
        )

    def _append(
        self,
        host: str,
        condition: IncidentCondition,
        state: IncidentState,
        observed_at: str,
    ) -> IncidentEvent:
        event = IncidentEvent(
            event_id=self._next_event_id,
            host=host,
            condition=condition,
            state=state,
            observed_at=observed_at,
        )
        self._events.append(event)
        self._next_event_id += 1
        self._version += 1
        return event
