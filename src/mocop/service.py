from __future__ import annotations

import copy
import logging
import math
import sys
import threading
import time
import traceback
import zlib
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from heapq import nsmallest
from struct import Struct
from typing import Protocol

from . import __version__
from .config import (
    ConnectionTopologyConfig,
    IncidentActionConfig,
    IncidentConfig,
    IncidentScopeOverrideConfig,
    MaintenanceWindowConfig,
    MonitorConfig,
    ThresholdConfig,
)
from .correlation import create_incident_correlator
from .diagnostics import diagnose_condition, sanitized_bundle
from .discovery import (
    CancellableHostSource,
    HostDiscoverySnapshot,
    HostSource,
    resolve_host_discovery,
)
from .incidents import IncidentPolicy, IncidentTracker, ThresholdIncidentPolicy
from .models import GpuProcess, ProbeResult, ServerState, utc_after, utc_now
from .notifications import DisabledNotificationSink, IncidentNotificationSink
from .persistence import (
    DisabledPersistence,
    LoadedTelemetry,
    TelemetryPersistence,
)
from .probe import (
    AttendedAwareResourceProbe,
    CancellableResourceProbe,
    InventoryAwareResourceProbe,
    ResourceProbe,
)
from .usage import aggregate_usage

_MAX_FAILURE_BACKOFF_SECONDS = 60.0
_MAX_PROBE_WORKERS = 64
# SSE heartbeats arrive every ~15 seconds and poll fallback every ~5, so half
# a minute of silence reliably means no dashboard is open.
_DASHBOARD_ATTENDED_WINDOW_SECONDS = 30.0
_MIN_RUNTIME_POLL_INTERVAL_SECONDS = 1.0
_MAX_RUNTIME_POLL_INTERVAL_SECONDS = 3600.0
# Mirrors the probe-side GPU cap so a host that keeps reporting fresh GPU
# identities (UUID churn) cannot grow per-identity telemetry without bound,
# while briefly absent GPUs keep their displayable history.
_MAX_GPU_IDENTITIES_PER_HOST = 256
_HOST_HISTORY_VALUES = Struct("<12d")
_GPU_HISTORY_VALUES = Struct("<i5d")
_LOGGER = logging.getLogger(__name__)


def _log_unexpected_collector_exception(scope: str, exc: BaseException) -> None:
    """Log type and stack without echoing potentially secret exception text."""
    stack = "\n".join(
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    )
    _LOGGER.error(
        "Unexpected collector exception in %s (%s)\n%s",
        scope,
        type(exc).__name__,
        stack,
    )


def _bounded_restored_gpu_keys(restored: LoadedTelemetry) -> set[tuple[str, str]]:
    """Keep the most recently observed GPU identities for each restored host."""
    latest: dict[tuple[str, str], str] = {}
    for records in (restored.gpu_history, restored.process_events):
        for key, points in records.items():
            observed_at = max(
                (str(point.get("observedAt", "")) for point in points), default=""
            )
            latest[key] = max(latest.get(key, ""), observed_at)
    retained: set[tuple[str, str]] = set()
    hosts = {host for host, _gpu_id in latest}
    for host in hosts:
        candidates = sorted(
            (key for key in latest if key[0] == host),
            key=lambda key: (latest[key], key[1]),
            reverse=True,
        )
        retained.update(candidates[:_MAX_GPU_IDENTITIES_PER_HOST])
    return retained


@dataclass(frozen=True, slots=True)
class _ScheduledProbe:
    host: str
    started_at: float
    batch_id: int
    incarnation: int


@dataclass(slots=True)
class _ProbeBatch:
    started_at: float
    remaining: int


@dataclass(frozen=True, slots=True)
class _HostHistoryPoint:
    observed_at: str
    values: bytes

    @classmethod
    def create(
        cls,
        observed_at: str,
        cpu_usage_pct: float | None,
        memory_usage_pct: float,
        swap_usage_pct: float,
        disk_usage_pct: float,
        network_rx_bps: float | None,
        network_tx_bps: float | None,
        disk_read_bps: float | None,
        disk_write_bps: float | None,
        gpu_usage_pct: float | None,
        gpu_memory_usage_pct: float | None,
        gpu_temperature_c: float | None,
        transport_retried: bool = False,
    ) -> _HostHistoryPoint:
        return cls(
            observed_at,
            _HOST_HISTORY_VALUES.pack(
                _packed_optional_float(cpu_usage_pct),
                memory_usage_pct,
                swap_usage_pct,
                disk_usage_pct,
                _packed_optional_float(network_rx_bps),
                _packed_optional_float(network_tx_bps),
                _packed_optional_float(disk_read_bps),
                _packed_optional_float(disk_write_bps),
                _packed_optional_float(gpu_usage_pct),
                _packed_optional_float(gpu_memory_usage_pct),
                _packed_optional_float(gpu_temperature_c),
                float(transport_retried),
            ),
        )

    @classmethod
    def from_dict(cls, point: dict[str, object]) -> _HostHistoryPoint:
        return cls.create(
            observed_at=str(point["observedAt"]),
            cpu_usage_pct=_optional_float(point.get("cpuUsagePct")),
            memory_usage_pct=float(point["memoryUsagePct"]),
            swap_usage_pct=float(point["swapUsagePct"]),
            disk_usage_pct=float(point["diskUsagePct"]),
            network_rx_bps=_optional_float(point.get("networkRxBps")),
            network_tx_bps=_optional_float(point.get("networkTxBps")),
            disk_read_bps=_optional_float(point.get("diskReadBps")),
            disk_write_bps=_optional_float(point.get("diskWriteBps")),
            gpu_usage_pct=_optional_float(point.get("gpuUsagePct")),
            gpu_memory_usage_pct=_optional_float(point.get("gpuMemoryUsagePct")),
            gpu_temperature_c=_optional_float(point.get("gpuTemperatureC")),
            transport_retried=bool(point.get("transportRetried")),
        )

    def to_dict(self) -> dict[str, object]:
        (
            cpu_usage_pct,
            memory_usage_pct,
            swap_usage_pct,
            disk_usage_pct,
            network_rx_bps,
            network_tx_bps,
            disk_read_bps,
            disk_write_bps,
            gpu_usage_pct,
            gpu_memory_usage_pct,
            gpu_temperature_c,
            transport_retried,
        ) = _HOST_HISTORY_VALUES.unpack(self.values)
        return {
            "observedAt": self.observed_at,
            "cpuUsagePct": _unpacked_optional_float(cpu_usage_pct),
            "memoryUsagePct": memory_usage_pct,
            "swapUsagePct": swap_usage_pct,
            "diskUsagePct": disk_usage_pct,
            "networkRxBps": _unpacked_optional_float(network_rx_bps),
            "networkTxBps": _unpacked_optional_float(network_tx_bps),
            "diskReadBps": _unpacked_optional_float(disk_read_bps),
            "diskWriteBps": _unpacked_optional_float(disk_write_bps),
            "gpuUsagePct": _unpacked_optional_float(gpu_usage_pct),
            "gpuMemoryUsagePct": _unpacked_optional_float(gpu_memory_usage_pct),
            "gpuTemperatureC": _unpacked_optional_float(gpu_temperature_c),
            "transportRetried": bool(transport_retried),
        }


@dataclass(frozen=True, slots=True)
class _GpuHistoryPoint:
    observed_at: str
    values: bytes

    @classmethod
    def create(
        cls,
        observed_at: str,
        index: int,
        utilization_gpu_pct: float | None,
        memory_used_mib: float | None,
        memory_total_mib: float | None,
        temperature_c: float | None,
        power_draw_w: float | None,
    ) -> _GpuHistoryPoint:
        return cls(
            observed_at,
            _GPU_HISTORY_VALUES.pack(
                index,
                _packed_optional_float(utilization_gpu_pct),
                _packed_optional_float(memory_used_mib),
                _packed_optional_float(memory_total_mib),
                _packed_optional_float(temperature_c),
                _packed_optional_float(power_draw_w),
            ),
        )

    @classmethod
    def from_dict(cls, point: dict[str, object]) -> _GpuHistoryPoint:
        return cls.create(
            observed_at=str(point["observedAt"]),
            index=int(point["index"]),
            utilization_gpu_pct=_optional_float(point.get("utilizationGpuPct")),
            memory_used_mib=_optional_float(point.get("memoryUsedMiB")),
            memory_total_mib=_optional_float(point.get("memoryTotalMiB")),
            temperature_c=_optional_float(point.get("temperatureC")),
            power_draw_w=_optional_float(point.get("powerDrawW")),
        )

    def to_dict(self, gpu_id: str) -> dict[str, object]:
        (
            index,
            utilization_gpu_pct,
            memory_used_mib,
            memory_total_mib,
            temperature_c,
            power_draw_w,
        ) = _GPU_HISTORY_VALUES.unpack(self.values)
        return {
            "observedAt": self.observed_at,
            "gpuId": gpu_id,
            "index": index,
            "utilizationGpuPct": _unpacked_optional_float(utilization_gpu_pct),
            "memoryUsedMiB": _unpacked_optional_float(memory_used_mib),
            "memoryTotalMiB": _unpacked_optional_float(memory_total_mib),
            "temperatureC": _unpacked_optional_float(temperature_c),
            "powerDrawW": _unpacked_optional_float(power_draw_w),
        }


@dataclass(frozen=True, slots=True)
class _GpuProcessTransition:
    observed_at: str
    gpu_id: str
    index: int
    event: str
    pid: int
    name: str
    used_memory_mib: float | None
    workload: dict[str, object] | None
    visible: bool = True

    @classmethod
    def from_dict(cls, event: dict[str, object]) -> _GpuProcessTransition:
        workload = event.get("workload")
        return cls(
            observed_at=str(event["observedAt"]),
            gpu_id=str(event["gpuId"]),
            index=int(event["index"]),
            event=str(event["event"]),
            pid=int(event["pid"]),
            name=str(event["name"]),
            used_memory_mib=_optional_float(event.get("usedMemoryMiB")),
            workload=dict(workload) if isinstance(workload, dict) else None,
            visible=event.get("_visible") is not False,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "observedAt": self.observed_at,
            "gpuId": self.gpu_id,
            "index": self.index,
            "event": self.event,
            "pid": self.pid,
            "name": self.name,
            "usedMemoryMiB": self.used_memory_mib,
            "workload": dict(self.workload) if self.workload is not None else None,
        }

    def persistence_dict(self) -> dict[str, object]:
        value = self.to_dict()
        value["_visible"] = self.visible
        return value


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _packed_optional_float(value: float | None) -> float:
    return math.nan if value is None else value


def _unpacked_optional_float(value: float) -> float | None:
    return None if math.isnan(value) else value


class ProbeControl(Protocol):
    def request_probe(self, host: str) -> dict[str, object]: ...


class StateStore:
    """Thread-safe current-state store and notification point for SSE clients."""

    def __init__(
        self,
        poll_interval_seconds: float,
        thresholds: ThresholdConfig | None = None,
        history_points: int = 720,
        incident_history_points: int = 500,
        collection_stale_cycles: int = 3,
        incident_policy: IncidentPolicy | None = None,
        expected_gpu_counts: tuple[tuple[str, int], ...] = (),
        incidents: IncidentConfig | None = None,
        incident_actions: tuple[IncidentActionConfig, ...] = (),
        host_incident_overrides: tuple[
            tuple[str, IncidentScopeOverrideConfig], ...
        ] = (),
        group_incident_overrides: tuple[
            tuple[str, IncidentScopeOverrideConfig], ...
        ] = (),
        maintenance_windows: tuple[tuple[str, MaintenanceWindowConfig], ...] = (),
        host_groups: tuple[tuple[str, str], ...] = (),
        host_display_names: tuple[tuple[str, str], ...] = (),
        utc_clock: Callable[[], datetime] | None = None,
        persistence: TelemetryPersistence | None = None,
        restored: LoadedTelemetry | None = None,
        topology: ConnectionTopologyConfig | None = None,
        notifications: IncidentNotificationSink | None = None,
    ) -> None:
        self._condition = threading.Condition()
        self._servers: dict[str, ServerState] = {}
        self._host_incarnations: dict[str, int] = {}
        self._next_host_incarnation = 1
        self._history: dict[str, deque[_HostHistoryPoint]] = {}
        self._gpu_history: dict[tuple[str, str], deque[_GpuHistoryPoint]] = {}
        self._process_events: dict[tuple[str, str], deque[_GpuProcessTransition]] = {}
        self._active_gpu_processes: dict[
            tuple[str, str], dict[tuple[int, str], GpuProcess]
        ] = {}
        self._process_last_observed_at: dict[tuple[str, str], str] = {}
        # Per-host insertion-ordered map of GPU identities, oldest observation
        # first; bounds identity churn without dropping recent history.
        self._gpu_identities: dict[str, dict[str, None]] = {}
        self._process_inventory_initialized: dict[str, set[str]] = {}
        self._inventory_initialized = False
        self._version = 0
        self._collector_error: str | None = None
        self._transport_retries_total = 0
        # Monotonic timestamp of the last dashboard reader; written and read
        # without a lock because a float store is atomic and staleness by one
        # poll cycle is harmless.
        self._dashboard_last_seen: float | None = None
        self._poll_interval_seconds = poll_interval_seconds
        self._collection_stale_cycles = collection_stale_cycles
        self._collection_stale_after_seconds = (
            poll_interval_seconds * collection_stale_cycles
        )
        self._thresholds = thresholds or ThresholdConfig()
        self._history_points = history_points
        selected_policy = incident_policy or ThresholdIncidentPolicy(
            self._thresholds,
            expected_gpu_counts=expected_gpu_counts,
            incidents=incidents,
            host_overrides=host_incident_overrides,
            group_overrides=group_incident_overrides,
            host_groups=host_groups,
        )
        self._incident_policy = selected_policy
        self._persistence = persistence or DisabledPersistence()
        self._persistence_enabled = self._persistence.is_enabled()
        restored_telemetry = restored or LoadedTelemetry({}, ())
        retained_gpu_keys = _bounded_restored_gpu_keys(restored_telemetry)
        self._restored_history = dict(restored_telemetry.history)
        self._gpu_history = {
            key: deque(
                (_GpuHistoryPoint.from_dict(point) for point in points),
                maxlen=history_points,
            )
            for key, points in restored_telemetry.gpu_history.items()
            if key in retained_gpu_keys
        }
        self._process_events = {
            key: deque(
                (_GpuProcessTransition.from_dict(point) for point in points),
                maxlen=incident_history_points,
            )
            for key, points in restored_telemetry.process_events.items()
            if key in retained_gpu_keys
        }
        self._process_reconciliation_pending = set(self._process_events)
        restored_latest = {
            key: max(
                (
                    str(point.get("observedAt", ""))
                    for point in (
                        *restored_telemetry.gpu_history.get(key, ()),
                        *restored_telemetry.process_events.get(key, ()),
                    )
                ),
                default="",
            )
            for key in retained_gpu_keys
        }
        for host, gpu_id in sorted(
            retained_gpu_keys,
            key=lambda key: (key[0], restored_latest[key], key[1]),
        ):
            self._gpu_identities.setdefault(host, {})[gpu_id] = None
        self._process_event_points = incident_history_points
        self._incidents = IncidentTracker(
            selected_policy,
            incident_history_points,
            historical_events=restored_telemetry.incident_events,
        )
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self._maintenance_windows = dict(maintenance_windows)
        self._incident_actions = {
            (action.host, action.condition_key): action for action in incident_actions
        }
        # Active incident state is intentionally not restored. A durable,
        # generation-scoped action may nevertheless bind once to the first
        # matching live condition after startup. Recovery consumes that
        # allowance, so a later recurrence cannot inherit the old action.
        self._startup_action_candidates = {
            key
            for key, action in self._incident_actions.items()
            if action.incident_started_at is not None
        }
        self._startup_action_bindings: dict[tuple[str, str], str] = {}
        self._host_groups = dict(host_groups)
        self._display_names = dict(host_display_names)
        self._host_incident_overrides = host_incident_overrides
        self._group_incident_overrides = group_incident_overrides
        self._topology = topology
        self._incident_correlator = create_incident_correlator(topology)
        self._notifications = notifications or DisabledNotificationSink()
        initial_persistence_status = self._persistence.status()
        initial_notification_status = self._notifications.status()
        self._adapter_statuses = (
            initial_persistence_status,
            initial_notification_status,
        )
        self._adapter_status_signature = (
            repr(initial_persistence_status),
            repr(initial_notification_status),
        )
        self._notification_event_incarnations: dict[int, int] = {}
        self._notification_event_order: deque[int] = deque()
        self._notification_event_capacity = max(1_024, incident_history_points * 4)

        def _notification_event_actionable(event) -> bool:
            with self._condition:
                if event.state == "resolved":
                    # The worker independently requires a successfully delivered
                    # open before sending a resolve, so preserve that pairing.
                    return True
                expected_incarnation = self._notification_event_incarnations.get(
                    event.event_id
                )
                return (
                    expected_incarnation is not None
                    and self._host_incarnations.get(event.host) == expected_incarnation
                    and self._incidents.has_active_condition(
                        event.host, event.condition.key
                    )
                    and event.host not in self._active_maintenance_locked()
                    and not (
                        self._condition_is_silenced_locked(
                            event.host, event.condition.key
                        )
                    )
                )

        self._notifications.set_actionable_check(_notification_event_actionable)
        self._active_maintenance_signature = self._maintenance_signature_locked()
        self._active_action_signature = self._incident_action_signature_locked()
        self._incident_revision = self._incidents.version
        self._tracker_version = self._incidents.version
        self._started_at = utc_now()
        self._last_poll_completed_at: str | None = None
        self._last_poll_duration_ms: int | None = None
        self._snapshot_cache_key: tuple[object, ...] | None = None
        self._snapshot_cache: dict[str, object] | None = None

    def set_hosts(self, hosts: tuple[str, ...]) -> None:
        desired = set(hosts)
        with self._condition:
            changed = False
            initial_inventory = not self._inventory_initialized
            removed_hosts = set(self._servers) - desired
            for host in removed_hosts:
                del self._servers[host]
                self._host_incarnations.pop(host, None)
                self._history.pop(host, None)
                changed = True
            if initial_inventory or removed_hosts:

                def stale(host: str) -> bool:
                    return (
                        host not in desired
                        if initial_inventory
                        else host in removed_hosts
                    )

                for records in (
                    self._gpu_history,
                    self._process_events,
                    self._active_gpu_processes,
                    self._process_last_observed_at,
                ):
                    for key in tuple(records):
                        if stale(key[0]):
                            records.pop(key, None)
                self._gpu_identities = {
                    host: identities
                    for host, identities in self._gpu_identities.items()
                    if not stale(host)
                }
                self._process_inventory_initialized = {
                    host: gpu_ids
                    for host, gpu_ids in self._process_inventory_initialized.items()
                    if not stale(host)
                }
                self._process_reconciliation_pending = {
                    key
                    for key in self._process_reconciliation_pending
                    if not stale(key[0])
                }
            if initial_inventory:
                self._restored_history = {
                    host: points
                    for host, points in self._restored_history.items()
                    if host in desired
                }
                self._inventory_initialized = True
            for host in hosts:
                if host not in self._servers:
                    self._servers[host] = ServerState(host=host)
                    self._host_incarnations[host] = self._next_host_incarnation
                    self._next_host_incarnation += 1
                    self._history[host] = deque(
                        (
                            _HostHistoryPoint.from_dict(point)
                            for point in self._restored_history.pop(host, ())
                        ),
                        maxlen=self._history_points,
                    )
                    changed = True
            self._incidents.remove_hosts(desired)
            self._incident_policy.retain_hosts(desired)
            self._sync_tracker_revision_locked()
            if changed:
                self._publish_locked()

    def host_incarnation(self, host: str) -> int | None:
        with self._condition:
            return self._host_incarnations.get(host)

    def set_collector_error(self, message: str | None) -> None:
        with self._condition:
            if message != self._collector_error:
                self._collector_error = message
                self._publish_locked()

    def poll_interval_seconds(self) -> float:
        with self._condition:
            return self._poll_interval_seconds

    def set_poll_interval_seconds(self, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("poll interval must be a number")
        interval = float(value)
        if (
            not math.isfinite(interval)
            or not _MIN_RUNTIME_POLL_INTERVAL_SECONDS
            <= interval
            <= _MAX_RUNTIME_POLL_INTERVAL_SECONDS
        ):
            raise ValueError("poll interval must be between 1 and 3600 seconds")
        with self._condition:
            if interval == self._poll_interval_seconds:
                return interval
            self._poll_interval_seconds = interval
            self._collection_stale_after_seconds = (
                interval * self._collection_stale_cycles
            )
            self._publish_locked()
        return interval

    def update_expected_gpu_counts(
        self, expected_gpu_counts: tuple[tuple[str, int], ...]
    ) -> None:
        with self._condition:
            self._incident_policy.update_expected_gpu_counts(expected_gpu_counts)

    def set_maintenance_windows(
        self,
        maintenance_windows: tuple[tuple[str, MaintenanceWindowConfig], ...],
    ) -> None:
        with self._condition:
            updated = dict(maintenance_windows)
            if updated == self._maintenance_windows:
                return
            self._maintenance_windows = updated
            self._active_maintenance_signature = self._maintenance_signature_locked()
            self._incident_revision += 1
            self._publish_locked()

    def set_incident_actions(
        self, incident_actions: tuple[IncidentActionConfig, ...]
    ) -> None:
        with self._condition:
            updated = {
                (action.host, action.condition_key): action
                for action in incident_actions
            }
            if updated == self._incident_actions:
                return
            previous = self._incident_actions
            self._incident_actions = updated
            retained = {
                key for key, action in updated.items() if previous.get(key) == action
            }
            self._startup_action_candidates.intersection_update(retained)
            self._startup_action_bindings = {
                key: started_at
                for key, started_at in self._startup_action_bindings.items()
                if key in retained
            }
            self._active_action_signature = self._incident_action_signature_locked()
            self._incident_revision += 1
            self._publish_locked()

    def set_host_display_names(
        self, host_display_names: tuple[tuple[str, str], ...]
    ) -> None:
        with self._condition:
            updated = dict(host_display_names)
            if updated == self._display_names:
                return
            self._display_names = updated
            self._publish_locked()

    def set_host_groups(self, host_groups: tuple[tuple[str, str], ...]) -> None:
        with self._condition:
            updated = dict(host_groups)
            if updated == self._host_groups:
                return
            self._host_groups = updated
            self._incident_policy.update_overrides(
                self._host_incident_overrides,
                self._group_incident_overrides,
                host_groups,
            )
            self._publish_locked()

    def set_incident_overrides(
        self,
        host_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...],
        group_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...],
        host_groups: tuple[tuple[str, str], ...],
    ) -> None:
        with self._condition:
            if (
                host_overrides == self._host_incident_overrides
                and group_overrides == self._group_incident_overrides
            ):
                return
            self._host_incident_overrides = host_overrides
            self._group_incident_overrides = group_overrides
            self._incident_policy.update_overrides(
                host_overrides, group_overrides, host_groups
            )
            self._incident_revision += 1
            self._publish_locked()

    def set_topology(self, topology: ConnectionTopologyConfig | None) -> None:
        with self._condition:
            if topology == self._topology:
                return
            self._topology = topology
            self._incident_correlator = create_incident_correlator(topology)
            self._incident_revision += 1
            self._publish_locked()

    def reconfigure(self, config: MonitorConfig, hosts: tuple[str, ...] | None) -> None:
        """Install one immutable config generation as an atomic state change."""
        with self._condition:
            self.set_poll_interval_seconds(config.poll_interval_seconds)
            self.update_expected_gpu_counts(config.expected_gpu_counts)
            self.set_maintenance_windows(config.maintenance_windows)
            self.set_incident_actions(config.incident_actions)
            self.set_incident_overrides(
                config.host_incident_overrides,
                config.group_incident_overrides,
                config.host_groups,
            )
            self.set_host_groups(config.host_groups)
            self.set_host_display_names(config.host_display_names())
            self.set_topology(config.topology)
            if hosts is not None:
                self.set_hosts(hosts)

    def begin_poll(self, hosts: tuple[str, ...]) -> None:
        with self._condition:
            changed = False
            for host in hosts:
                state = self._servers.get(host)
                if state is not None and not state.polling:
                    state.polling = True
                    changed = True
            if changed:
                self._publish_locked()

    def apply(
        self,
        result: ProbeResult,
        retry_after_seconds: float | None = None,
        poll_cycle_duration_seconds: float | None = None,
        expected_incarnation: int | None = None,
    ) -> bool:
        history_point: _HostHistoryPoint | None = None
        gpu_history_points: tuple[tuple[str, _GpuHistoryPoint], ...] = ()
        process_events: tuple[_GpuProcessTransition, ...] = ()
        incident_events = ()
        notification_events = ()
        correlations: tuple[dict[str, object], ...] = ()
        with self._condition:
            # Folding the batch completion into this apply publishes both in
            # one revision instead of a separate record_poll_cycle publish.
            if poll_cycle_duration_seconds is not None:
                self._record_poll_cycle_locked(poll_cycle_duration_seconds)
            state = self._servers.get(result.host)
            if state is None or (
                expected_incarnation is not None
                and self._host_incarnations.get(result.host) != expected_incarnation
            ):
                if poll_cycle_duration_seconds is not None:
                    self._publish_locked()
                return False
            previous_incident_signature = self._incidents.active_signature(result.host)
            incident_events = self._incidents.update(result)
            self._refresh_startup_action_bindings_locked(result)
            self._sync_tracker_revision_locked()
            if (
                previous_incident_signature
                and self._incidents.active_signature(result.host)
                != previous_incident_signature
                and not incident_events
            ):
                # Active values and last-observed timestamps are live data even
                # when no open/resolve transition was emitted.
                self._incident_revision += 1
            next_retry_at = (
                utc_after(retry_after_seconds)
                if retry_after_seconds is not None
                else None
            )
            self._transport_retries_total += result.transport_retries
            if result.status == "online":
                gpu_history_points, process_events = self._track_gpu_telemetry_locked(
                    result
                )
                result = self._first_seen_annotated_locked(result)
            else:
                process_events = self._invalidate_process_inventory_locked(result.host)
            state.apply(result, next_retry_at=next_retry_at)
            if result.status == "online" and result.system is not None:
                history_point = self._history_point(result)
                self._history[result.host].append(history_point)
            if incident_events:
                now = self._utc_clock()
                active_windows = self._active_maintenance_locked(now)
                notification_events = tuple(
                    event
                    for event in incident_events
                    # A resolved transition always reaches the queue: the
                    # delivery worker pairs it with the opened event it
                    # already sent, and suppressing it here would leave the
                    # receiver's alert hanging forever when a condition
                    # recovers inside a maintenance window or silence.
                    if event.state == "resolved"
                    or (
                        event.host not in active_windows
                        and not self._condition_is_silenced_locked(
                            event.host, event.condition.key
                        )
                    )
                )
            if notification_events:
                incarnation = self._host_incarnations[result.host]
                for event in notification_events:
                    self._notification_event_incarnations[event.event_id] = incarnation
                    self._notification_event_order.append(event.event_id)
                while (
                    len(self._notification_event_order)
                    > self._notification_event_capacity
                ):
                    expired_id = self._notification_event_order.popleft()
                    self._notification_event_incarnations.pop(expired_id, None)
                # The correlator consumes only actionable conditions, so this
                # list carries the same maintenance/silence/acknowledge
                # decoration as the incident views.
                active = self._incidents.snapshot(1)["active"]
                active_actions = self._active_incident_actions_locked(now)
                for item in active:
                    self._decorate_incident_locked(
                        item,
                        active_maintenance=active_windows,
                        active_actions=active_actions,
                        at=now,
                    )
                correlations = self._incident_correlator.correlate(
                    active,
                    frozenset(self._servers),
                )
            self._publish_locked()
        if history_point is not None and self._persistence_enabled:
            self._persistence.record_history(result.host, history_point.to_dict())
        if self._persistence_enabled and incident_events:
            self._persistence.record_incidents(incident_events)
        if self._persistence_enabled and (gpu_history_points or process_events):
            self._persistence.record_gpu_telemetry(
                result.host,
                tuple(point.to_dict(gpu_id) for gpu_id, point in gpu_history_points),
                tuple(event.persistence_dict() for event in process_events),
            )
        if notification_events:
            self._notifications.publish(notification_events, correlations)
        return True

    def reschedule_retry(self, host: str, retry_after_seconds: float) -> None:
        """Publish a rebased retry deadline after the runtime cadence changes."""
        with self._condition:
            state = self._servers.get(host)
            if state is None or state.status == "online":
                return
            state.next_retry_at = utc_after(retry_after_seconds)
            self._publish_locked()

    def record_dashboard_activity(self) -> None:
        """Mark a live dashboard reader so probes keep the attended cadence."""
        self._dashboard_last_seen = time.monotonic()

    def dashboard_attended(self) -> bool:
        """Return whether a dashboard reader was seen within the window."""
        last_seen = self._dashboard_last_seen
        return (
            last_seen is not None
            and time.monotonic() - last_seen < _DASHBOARD_ATTENDED_WINDOW_SECONDS
        )

    def record_poll_cycle(self, duration_seconds: float) -> None:
        """Record and publish the latest completed scheduler submission batch."""
        with self._condition:
            self._record_poll_cycle_locked(duration_seconds)
            self._publish_locked()

    def _record_poll_cycle_locked(self, duration_seconds: float) -> None:
        self._last_poll_completed_at = utc_now()
        self._last_poll_duration_ms = max(0, round(duration_seconds * 1000))

    def history(self, host: str, limit: int) -> dict[str, object] | None:
        with self._condition:
            points = self._history.get(host)
            if points is None:
                return None
            return {
                "host": host,
                "pollIntervalSeconds": self._poll_interval_seconds,
                "maxPoints": self._history_points,
                "points": [point.to_dict() for point in list(points)[-limit:]],
            }

    def gpu_history(
        self, host: str, gpu_id: str, limit: int
    ) -> dict[str, object] | None:
        with self._condition:
            if host not in self._servers:
                return None
            key = (host, gpu_id)
            points = self._gpu_history.get(key)
            if points is None:
                return None
            events = self._process_events.get(key, ())
            return {
                "host": host,
                "gpuId": gpu_id,
                "pollIntervalSeconds": self._poll_interval_seconds,
                "maxPoints": self._history_points,
                "points": [point.to_dict(gpu_id) for point in list(points)[-limit:]],
                "processEvents": [event.to_dict() for event in events if event.visible][
                    -limit:
                ],
            }

    def usage(self, window_hours: int, owner_limit: int) -> dict[str, object]:
        """Per-owner GPU occupancy over the window; see ``usage.aggregate_usage``.

        Only the consistent copy of the timeline is taken under the lock; the
        aggregation itself runs without it.
        """
        with self._condition:
            now = self._utc_clock()
            events_by_gpu = {
                key: list(events) for key, events in self._process_events.items()
            }
            active_by_gpu = {
                key: dict(processes)
                for key, processes in self._active_gpu_processes.items()
            }
            utilization_by_gpu = {
                key: [
                    (
                        point.observed_at,
                        _unpacked_optional_float(
                            _GPU_HISTORY_VALUES.unpack(point.values)[1]
                        ),
                    )
                    for point in points
                ]
                for key, points in self._gpu_history.items()
            }
            busy_pct = self._thresholds.gpu_busy_pct
        return aggregate_usage(
            now=now,
            window_hours=window_hours,
            owner_limit=owner_limit,
            busy_pct=busy_pct,
            events_by_gpu=events_by_gpu,
            active_by_gpu=active_by_gpu,
            utilization_by_gpu=utilization_by_gpu,
        )

    def incidents(self, limit: int) -> dict[str, object]:
        with self._condition:
            self._refresh_maintenance_expiry_locked()
            self._refresh_incident_action_expiry_locked()
            snapshot = self._incidents.snapshot(limit)
            servers = {host: state.to_dict() for host, state in self._servers.items()}
            now = self._utc_clock()
            active_maintenance = self._active_maintenance_locked(now)
            active_actions = self._active_incident_actions_locked(now)
            for item in snapshot["active"]:
                self._decorate_incident_locked(
                    item,
                    active_maintenance=active_maintenance,
                    active_actions=active_actions,
                    at=now,
                )
                item["diagnosis"] = diagnose_condition(
                    item, servers.get(str(item["host"]))
                )
            snapshot["active"].sort(
                key=lambda item: (
                    not bool(item["actionable"]),
                    item["severity"] != "critical",
                    str(item["host"]),
                    str(item["conditionKey"]),
                )
            )
            snapshot["correlations"] = list(
                self._incident_correlator.correlate(
                    snapshot["active"],
                    frozenset(self._servers),
                )
            )
            snapshot["version"] = self._incident_revision
        # The projection is rebuilt per call, so only the copy must leave the
        # lock: result publication and SSE wakeups never wait on it.
        return copy.deepcopy(snapshot)

    def active_incident_started_at(self, host: str, condition_key: str) -> str | None:
        """Return the stable identity of the currently active condition."""
        with self._condition:
            return self._incidents.active_started_at(host, condition_key)

    def diagnostic_bundle(self, host: str | None = None) -> dict[str, object] | None:
        with self._condition:
            if host is not None and host not in self._servers:
                return None
            self._refresh_maintenance_expiry_locked()
            self._refresh_incident_action_expiry_locked()
            snapshot = self._snapshot_locked()
            incidents = self._incidents.snapshot(self._process_event_points)
            now = self._utc_clock()
            active_maintenance = self._active_maintenance_locked(now)
            active_actions = self._active_incident_actions_locked(now)
            for item in incidents["active"]:
                self._decorate_incident_locked(
                    item,
                    active_maintenance=active_maintenance,
                    active_actions=active_actions,
                    at=now,
                )
            return sanitized_bundle(snapshot, incidents, host)

    def test_notifications(self) -> bool:
        return self._notifications.test()

    def health(self) -> dict[str, object]:
        with self._condition:
            discovered = len(self._servers)
            successful = sum(
                state.last_success_at is not None for state in self._servers.values()
            )
            ready = discovered > 0 and successful > 0
            if ready:
                reason = None
            elif self._collector_error:
                reason = (
                    "host discovery failed"
                    if self._collector_error.startswith("SSH host discovery")
                    else "collector failed unexpectedly"
                )
            elif discovered == 0:
                reason = "no monitoring targets discovered"
            else:
                reason = "waiting for first successful collection"
            return {
                "status": "ready" if ready else "not_ready",
                "ready": ready,
                "reason": reason,
                "targets": discovered,
                "targetsWithSuccessfulSample": successful,
                "transportRetries": self._transport_retries_total,
                "version": self._version,
                "startedAt": self._started_at,
            }

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            self._refresh_maintenance_expiry_locked()
            self._refresh_incident_action_expiry_locked()
            projection = self._snapshot_locked()
        # The cached projection is never mutated in place after publication
        # (rebuilds replace it wholesale), so the expensive deep copy can run
        # outside the lock without observing a torn state.
        return copy.deepcopy(projection)

    def snapshot_view(self) -> dict[str, object]:
        """Return the shared snapshot projection without a deep copy.

        The returned dict has exactly the same structure and content as
        ``snapshot()`` and may be the same object across calls until state
        changes. Callers must treat it as strictly read-only; mutating it
        would corrupt every concurrent reader of the cached projection.
        """
        with self._condition:
            self._refresh_maintenance_expiry_locked()
            self._refresh_incident_action_expiry_locked()
            return self._snapshot_locked()

    def wait_for_update(
        self, after_version: int, timeout_seconds: float
    ) -> dict[str, object] | None:
        with self._condition:
            self._refresh_maintenance_expiry_locked()
            self._refresh_incident_action_expiry_locked()
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            while self._version <= after_version:
                self._refresh_adapter_status_locked()
                if self._version > after_version:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Adapters are deliberately decoupled from collection and do
                # not call into StateStore. A short bounded poll still gives
                # their queue/health transitions a revision and wakes SSE.
                self._condition.wait(min(remaining, 0.25))
            if self._version <= after_version:
                return None
            return self._snapshot_locked()

    def _publish_locked(self) -> None:
        self._refresh_maintenance_expiry_locked()
        self._version += 1
        self._condition.notify_all()

    def _sync_tracker_revision_locked(self) -> None:
        tracker_version = self._incidents.version
        if tracker_version == self._tracker_version:
            return
        self._tracker_version = tracker_version
        self._incident_revision += 1

    def _active_maintenance_locked(
        self, at: datetime | None = None
    ) -> dict[str, MaintenanceWindowConfig]:
        if not self._maintenance_windows:
            return {}
        now = at or self._utc_clock()
        return {
            host: window
            for host, window in self._maintenance_windows.items()
            if window.is_active(now)
        }

    def _maintenance_signature_locked(self) -> tuple[tuple[str, str, str], ...]:
        now = self._utc_clock()
        return tuple(
            (host, str(window.to_dict(now)["until"]), window.reason)
            for host, window in sorted(self._active_maintenance_locked(now).items())
        )

    def _refresh_maintenance_expiry_locked(self) -> None:
        if not self._maintenance_windows:
            return
        signature = self._maintenance_signature_locked()
        if signature == self._active_maintenance_signature:
            return
        self._active_maintenance_signature = signature
        self._incident_revision += 1
        self._version += 1
        self._condition.notify_all()

    def _active_incident_actions_locked(
        self, at: datetime | None = None
    ) -> dict[tuple[str, str], IncidentActionConfig]:
        if not self._incident_actions:
            return {}
        now = at or self._utc_clock()
        return {
            key: action
            for key, action in self._incident_actions.items()
            if action.is_active(now)
        }

    def _incident_action_signature_locked(
        self,
    ) -> tuple[tuple[str, str, str, str, str | None], ...]:
        return tuple(
            (
                host,
                condition_key,
                action.action,
                action.to_dict()["until"],
                action.incident_started_at,
            )
            for (host, condition_key), action in sorted(
                self._active_incident_actions_locked().items()
            )
        )

    def _refresh_incident_action_expiry_locked(self) -> None:
        if not self._incident_actions:
            return
        signature = self._incident_action_signature_locked()
        if signature == self._active_action_signature:
            return
        self._active_action_signature = signature
        self._incident_revision += 1
        self._version += 1
        self._condition.notify_all()

    def _condition_is_silenced_locked(self, host: str, condition_key: str) -> bool:
        action = self._active_incident_actions_locked().get((host, condition_key))
        return (
            action is not None
            and action.action == "silenced"
            and self._incident_action_matches_locked(action, host, condition_key)
        )

    def _incident_action_matches_locked(
        self, action: IncidentActionConfig, host: str, condition_key: str
    ) -> bool:
        if action.incident_started_at is None:
            return True
        key = (host, condition_key)
        expected = self._startup_action_bindings.get(key, action.incident_started_at)
        return expected == self._incidents.active_started_at(host, condition_key)

    def _refresh_startup_action_bindings_locked(self, result: ProbeResult) -> None:
        host = result.host
        keys = {
            key
            for key in (
                *self._startup_action_candidates,
                *self._startup_action_bindings,
            )
            if key[0] == host
        }
        active_actions = self._active_incident_actions_locked()
        for key in keys:
            action = active_actions.get(key)
            started_at = self._incidents.active_started_at(*key)
            if action is None:
                self._startup_action_candidates.discard(key)
                self._startup_action_bindings.pop(key, None)
            elif key in self._startup_action_candidates and started_at is not None:
                self._startup_action_candidates.discard(key)
                self._startup_action_bindings[key] = started_at
            elif (
                key in self._startup_action_candidates
                and not self._incidents.has_pending_condition(*key)
                and self._incident_policy.condition_observed(result, key[1])
            ):
                # The first authoritative live sample was healthy for this
                # condition. Fail open: do not let an action from before the
                # restart suppress a later recurrence.
                self._startup_action_candidates.discard(key)
            elif key in self._startup_action_bindings and started_at is None:
                self._startup_action_bindings.pop(key, None)

    def _decorate_incident_locked(
        self,
        item: dict[str, object],
        *,
        active_maintenance: dict[str, MaintenanceWindowConfig] | None = None,
        active_actions: dict[tuple[str, str], IncidentActionConfig] | None = None,
        at: datetime | None = None,
    ) -> None:
        host = str(item["host"])
        condition_key = str(item["conditionKey"])
        maintenance = (
            active_maintenance
            if active_maintenance is not None
            else self._active_maintenance_locked(at)
        )
        actions = (
            active_actions
            if active_actions is not None
            else self._active_incident_actions_locked(at)
        )
        window = maintenance.get(host)
        action = actions.get((host, condition_key))
        if action is not None and not self._incident_action_matches_locked(
            action, host, condition_key
        ):
            action = None
        item["maintenanceSilenced"] = window is not None
        item["silenced"] = window is not None or (
            action is not None and action.action == "silenced"
        )
        item["acknowledged"] = action is not None and action.action == "acknowledged"
        item["actionable"] = not (item["silenced"] or item["acknowledged"])
        item["action"] = action.action if action is not None else None
        item["actionUntil"] = action.to_dict()["until"] if action is not None else None
        item["actionReason"] = action.reason if action is not None else None
        if window is not None:
            # Reuse the caller's clock sample so the serialized window bounds
            # match the instance that made this incident silenced.
            item["maintenanceUntil"] = window.to_dict(at or self._utc_clock())["until"]
            item["maintenanceReason"] = window.reason

    def _track_gpu_telemetry_locked(
        self, result: ProbeResult
    ) -> tuple[
        tuple[tuple[str, _GpuHistoryPoint], ...],
        tuple[_GpuProcessTransition, ...],
    ]:
        captured_points: list[tuple[str, _GpuHistoryPoint]] | None = (
            [] if self._persistence_enabled else None
        )
        captured_transitions: list[_GpuProcessTransition] | None = (
            [] if self._persistence_enabled else None
        )
        observed_gpu_ids: set[str] = set()
        initialized_gpu_ids = self._process_inventory_initialized.get(result.host)
        if initialized_gpu_ids is None:
            initialized_gpu_ids = set()
            self._process_inventory_initialized[result.host] = initialized_gpu_ids
        identities = self._gpu_identities.setdefault(result.host, {})
        for gpu in result.gpus:
            gpu_id = gpu.uuid or f"index:{gpu.index}"
            key = (result.host, gpu_id)
            observed_gpu_ids.add(gpu_id)
            identities.pop(gpu_id, None)
            identities[gpu_id] = None
            point = _GpuHistoryPoint.create(
                result.observed_at,
                gpu.index,
                gpu.utilization_gpu_pct,
                gpu.memory_used_mib,
                gpu.memory_total_mib,
                gpu.temperature_c,
                gpu.power_draw_w,
            )
            gpu_history = self._gpu_history.get(key)
            previous_gpu_observed_at = (
                gpu_history[-1].observed_at if gpu_history else None
            )
            if gpu_history is None:
                gpu_history = deque(maxlen=self._history_points)
                self._gpu_history[key] = gpu_history
            gpu_history.append(point)
            if captured_points is not None:
                captured_points.append((gpu_id, point))
            if not gpu.processes_sampled:
                continue
            if not gpu.processes_available:
                unavailable_transitions = self._close_unobservable_inventory_locked(
                    key, gpu.index
                )
                if captured_transitions is not None:
                    captured_transitions.extend(unavailable_transitions)
                initialized_gpu_ids.discard(gpu_id)
                continue
            self._process_last_observed_at[key] = result.observed_at
            if not gpu.processes:
                if gpu_id not in initialized_gpu_ids:
                    initialized_gpu_ids.add(gpu_id)
                    initial_transitions = (
                        self._reconcile_initial_processes_locked(
                            key,
                            gpu.index,
                            {},
                            result.observed_at,
                            previous_gpu_observed_at,
                        )
                        if key in self._process_reconciliation_pending
                        else ()
                    )
                    self._process_reconciliation_pending.discard(key)
                    if initial_transitions:
                        event_history = self._process_events.setdefault(
                            key, deque(maxlen=self._process_event_points)
                        )
                        event_history.extend(initial_transitions)
                        if captured_transitions is not None:
                            captured_transitions.extend(initial_transitions)
                    continue
                previous = self._active_gpu_processes.pop(key, None)
                if not previous:
                    continue
                gpu_transitions = [
                    self._process_transition(
                        result.observed_at,
                        gpu_id,
                        gpu.index,
                        "stopped",
                        previous[process_key],
                    )
                    for process_key in sorted(previous)
                ]
                event_history = self._process_events.setdefault(
                    key, deque(maxlen=self._process_event_points)
                )
                event_history.extend(gpu_transitions)
                if captured_transitions is not None:
                    captured_transitions.extend(gpu_transitions)
                continue
            previous = self._active_gpu_processes.get(key, {})
            # Carry the monitor-relative first-seen timestamp across samples
            # and stamp newcomers, so operators get a runtime lower bound
            # without any extra remote reads. A same-key process whose
            # workload start time changed is a new instance behind a reused
            # PID: it restarts the observation instead of inheriting it.
            current: dict[tuple[int, str], GpuProcess] = {}
            replaced_instances: set[tuple[int, str]] = set()
            for process in gpu.processes:
                process_key = (process.pid, process.name)
                prior = previous.get(process_key)
                if prior is not None and self._same_process_instance(prior, process):
                    first_seen_at = prior.first_seen_at or result.observed_at
                else:
                    if prior is not None:
                        replaced_instances.add(process_key)
                    first_seen_at = result.observed_at
                current[process_key] = replace(process, first_seen_at=first_seen_at)
            if gpu_id not in initialized_gpu_ids:
                self._active_gpu_processes[key] = current
                initialized_gpu_ids.add(gpu_id)
                if key in self._process_reconciliation_pending:
                    initial_transitions = self._reconcile_initial_processes_locked(
                        key,
                        gpu.index,
                        current,
                        result.observed_at,
                        previous_gpu_observed_at,
                    )
                    self._process_reconciliation_pending.discard(key)
                else:
                    initial_transitions = tuple(
                        self._process_transition(
                            result.observed_at,
                            gpu_id,
                            gpu.index,
                            "started",
                            current[process_key],
                            visible=False,
                        )
                        for process_key in sorted(current)
                    )
                if initial_transitions:
                    event_history = self._process_events.setdefault(
                        key, deque(maxlen=self._process_event_points)
                    )
                    event_history.extend(initial_transitions)
                    if captured_transitions is not None:
                        captured_transitions.extend(initial_transitions)
                continue
            if not replaced_instances and current.keys() == previous.keys():
                self._active_gpu_processes[key] = current
                continue
            gpu_transitions: list[_GpuProcessTransition] = []
            for process_key in sorted(current.keys() - previous.keys()):
                gpu_transitions.append(
                    self._process_transition(
                        result.observed_at,
                        gpu_id,
                        gpu.index,
                        "started",
                        current[process_key],
                    )
                )
            for process_key in sorted(previous.keys() - current.keys()):
                gpu_transitions.append(
                    self._process_transition(
                        result.observed_at,
                        gpu_id,
                        gpu.index,
                        "stopped",
                        previous[process_key],
                    )
                )
            for process_key in sorted(replaced_instances):
                gpu_transitions.append(
                    self._process_transition(
                        result.observed_at,
                        gpu_id,
                        gpu.index,
                        "stopped",
                        previous[process_key],
                    )
                )
                gpu_transitions.append(
                    self._process_transition(
                        result.observed_at,
                        gpu_id,
                        gpu.index,
                        "started",
                        current[process_key],
                    )
                )
            self._active_gpu_processes[key] = current
            if gpu_transitions:
                event_history = self._process_events.setdefault(
                    key, deque(maxlen=self._process_event_points)
                )
                event_history.extend(gpu_transitions)
                if captured_transitions is not None:
                    captured_transitions.extend(gpu_transitions)
        while len(identities) > _MAX_GPU_IDENTITIES_PER_HOST:
            evicted_gpu_id = next(iter(identities))
            del identities[evicted_gpu_id]
            evicted_key = (result.host, evicted_gpu_id)
            self._gpu_history.pop(evicted_key, None)
            self._process_events.pop(evicted_key, None)
            self._active_gpu_processes.pop(evicted_key, None)
            self._process_last_observed_at.pop(evicted_key, None)
            initialized_gpu_ids.discard(evicted_gpu_id)
        for gpu_id in initialized_gpu_ids - observed_gpu_ids:
            # A device that vanished from an online host takes its process
            # table with it; the occupancy confirmed up to its last process
            # sample closes there, as it does when the process query fails.
            closed = self._close_unobservable_inventory_locked((result.host, gpu_id))
            if captured_transitions is not None:
                captured_transitions.extend(closed)
        initialized_gpu_ids.intersection_update(observed_gpu_ids)
        if not initialized_gpu_ids:
            self._process_inventory_initialized.pop(result.host, None)
        if captured_points is None:
            return (), ()
        return tuple(captured_points), tuple(captured_transitions or ())

    def _invalidate_process_inventory_locked(
        self, host: str
    ) -> tuple[_GpuProcessTransition, ...]:
        """Close confirmed occupancy at its last successful process sample."""
        transitions: list[_GpuProcessTransition] = []
        for gpu_id in self._process_inventory_initialized.pop(host, ()):
            transitions.extend(
                self._close_unobservable_inventory_locked((host, gpu_id))
            )
        return tuple(transitions)

    def _close_unobservable_inventory_locked(
        self, key: tuple[str, str], gpu_index: int | None = None
    ) -> tuple[_GpuProcessTransition, ...]:
        """Close a GPU's confirmed occupancy once its process table is gone.

        The process query failed, the device vanished from an online host, or
        the host stopped answering: the occupancy confirmed up to the last
        process sample ends there as bookkeeping. Nothing was observed to
        stop, so the transitions stay out of the dashboard's event list.
        """
        if gpu_index is None:
            history = self._gpu_history.get(key)
            gpu_index = (
                int(_GPU_HISTORY_VALUES.unpack(history[-1].values)[0]) if history else 0
            )
        closed = self._close_process_inventory_locked(
            key, gpu_index, self._process_last_observed_at.pop(key, None)
        )
        if not closed:
            return ()
        closed = tuple(replace(event, visible=False) for event in closed)
        self._process_events.setdefault(
            key, deque(maxlen=self._process_event_points)
        ).extend(closed)
        return closed

    def _close_process_inventory_locked(
        self,
        key: tuple[str, str],
        gpu_index: int,
        observed_at: str | None,
    ) -> tuple[_GpuProcessTransition, ...]:
        previous = self._active_gpu_processes.pop(key, None)
        if not previous or observed_at is None:
            return ()
        return tuple(
            self._process_transition(
                observed_at,
                key[1],
                gpu_index,
                "stopped",
                previous[process_key],
            )
            for process_key in sorted(previous)
        )

    def _reconcile_initial_processes_locked(
        self,
        key: tuple[str, str],
        gpu_index: int,
        current: dict[tuple[int, str], GpuProcess],
        observed_at: str,
        previous_gpu_observed_at: str | None,
    ) -> tuple[_GpuProcessTransition, ...]:
        """Reconcile restored open transitions with the first live sample."""
        open_events: dict[tuple[int, str], _GpuProcessTransition] = {}
        for event in self._process_events.get(key, ()):
            process_key = (event.pid, event.name)
            if event.event == "started":
                open_events[process_key] = event
            else:
                open_events.pop(process_key, None)

        transitions: list[_GpuProcessTransition] = []
        close_at = previous_gpu_observed_at or observed_at
        for process_key, event in sorted(open_events.items()):
            process = current.get(process_key)
            if process is not None and self._transition_matches_process(event, process):
                current[process_key] = replace(process, first_seen_at=event.observed_at)
                continue
            transitions.append(replace(event, observed_at=close_at, event="stopped"))
        for process_key, process in sorted(current.items()):
            event = open_events.get(process_key)
            if event is not None and self._transition_matches_process(event, process):
                continue
            transitions.append(
                self._process_transition(
                    observed_at, key[1], gpu_index, "started", process
                )
            )
        return tuple(transitions)

    @staticmethod
    def _transition_matches_process(
        event: _GpuProcessTransition, process: GpuProcess
    ) -> bool:
        event_start = (
            event.workload.get("started_at")
            if isinstance(event.workload, dict)
            else None
        )
        process_start = process.workload.started_at if process.workload else None
        return not event_start or not process_start or event_start == process_start

    def _first_seen_annotated_locked(self, result: ProbeResult) -> ProbeResult:
        """Return the result with processes carrying their first-seen stamp."""
        if not any(gpu.processes for gpu in result.gpus):
            return result
        annotated_gpus = []
        for gpu in result.gpus:
            if not gpu.processes:
                annotated_gpus.append(gpu)
                continue
            tracked = self._active_gpu_processes.get(
                (result.host, gpu.uuid or f"index:{gpu.index}"), {}
            )
            annotated_gpus.append(
                replace(
                    gpu,
                    processes=tuple(
                        tracked.get((process.pid, process.name), process)
                        for process in gpu.processes
                    ),
                )
            )
        return replace(result, gpus=tuple(annotated_gpus))

    @staticmethod
    def _same_process_instance(previous: GpuProcess, current: GpuProcess) -> bool:
        """False only when both samples carry workload start times that differ.

        The workload's real start time (V7 protocol) distinguishes a new
        process behind a reused PID from a continuously running one. When
        either side lacks it (workloads disabled), the (pid, name) key alone
        keeps identifying the process and first-seen stays a documented
        lower bound.
        """
        previous_started = previous.workload.started_at if previous.workload else None
        current_started = current.workload.started_at if current.workload else None
        if previous_started is None or current_started is None:
            return True
        return previous_started == current_started

    @staticmethod
    def _process_transition(
        observed_at: str,
        gpu_id: str,
        gpu_index: int,
        event: str,
        process: GpuProcess,
        *,
        visible: bool = True,
    ) -> _GpuProcessTransition:
        return _GpuProcessTransition(
            observed_at=observed_at,
            gpu_id=gpu_id,
            index=gpu_index,
            event=event,
            pid=process.pid,
            name=process.name,
            used_memory_mib=process.used_memory_mib,
            workload=process.workload.to_dict() if process.workload else None,
            visible=visible,
        )

    @staticmethod
    def _history_point(result: ProbeResult) -> _HostHistoryPoint:
        system = result.system
        if system is None:
            raise ValueError("successful history points require system metrics")

        def percentage(used: float, total: float) -> float:
            return round((used / total) * 100, 2) if total > 0 else 0.0

        gpu_usage = [
            gpu.utilization_gpu_pct
            for gpu in result.gpus
            if gpu.utilization_gpu_pct is not None
        ]
        # Memory ratios only make sense for GPUs reporting both sides; without
        # any valid denominator the usage is unknown rather than a real 0%.
        gpu_memory = [
            (gpu.memory_used_mib, gpu.memory_total_mib)
            for gpu in result.gpus
            if gpu.memory_used_mib is not None and gpu.memory_total_mib is not None
        ]
        gpu_memory_used = sum(used for used, _ in gpu_memory)
        gpu_memory_total = sum(total for _, total in gpu_memory)
        temperatures = [
            gpu.temperature_c for gpu in result.gpus if gpu.temperature_c is not None
        ]
        return _HostHistoryPoint.create(
            result.observed_at,
            system.cpu_usage_pct,
            percentage(system.memory_used_mib, system.memory_total_mib),
            percentage(system.swap_used_mib, system.swap_total_mib),
            percentage(system.disk_used_mib, system.disk_total_mib),
            system.network_rx_bps,
            system.network_tx_bps,
            system.disk_read_bps,
            system.disk_write_bps,
            round(sum(gpu_usage) / len(gpu_usage), 2) if gpu_usage else None,
            percentage(gpu_memory_used, gpu_memory_total)
            if gpu_memory_total > 0
            else None,
            max(temperatures) if temperatures else None,
            transport_retried=result.transport_retries > 0,
        )

    def _snapshot_locked(self) -> dict[str, object]:
        persistence_status, notification_status = self._refresh_adapter_status_locked()
        cache_key = (
            self._version,
            self._incident_revision,
        )
        if cache_key == self._snapshot_cache_key and self._snapshot_cache is not None:
            return self._snapshot_cache

        servers = [state.to_dict() for state in self._servers.values()]
        # One clock sample rules the whole assembly so the active-window set,
        # incident decoration, and serialized window bounds cannot straddle a
        # recurring instance boundary within a single snapshot.
        now = self._utc_clock()
        active_maintenance = self._active_maintenance_locked(now)
        active_actions = self._active_incident_actions_locked(now)
        active_conditions = self._incidents.snapshot(1)["active"]
        for condition in active_conditions:
            self._decorate_incident_locked(
                condition,
                active_maintenance=active_maintenance,
                active_actions=active_actions,
                at=now,
            )
        host_incidents: dict[str, list[dict[str, object]]] = {}
        for condition in active_conditions:
            host = str(condition["host"])
            conditions = host_incidents.get(host)
            if conditions is None:
                conditions = []
                host_incidents[host] = conditions
            conditions.append(condition)
        for server in servers:
            host = str(server["host"])
            window = active_maintenance.get(host)
            server["maintenance"] = window.to_dict(now) if window else None
            server["group"] = self._host_groups.get(host)
            server["displayName"] = self._display_names.get(host)
            conditions = host_incidents.get(host, [])
            incident_count = len(conditions)
            critical_count = sum(
                condition["severity"] == "critical" for condition in conditions
            )
            actionable = [
                condition for condition in conditions if condition["actionable"]
            ]
            server["incidents"] = {
                "active": incident_count,
                "critical": critical_count,
                "actionable": len(actionable),
                "actionableCritical": sum(
                    condition["severity"] == "critical" for condition in actionable
                ),
            }
        online = sum(server["status"] == "online" for server in servers)
        current_servers = [server for server in servers if server["status"] == "online"]
        gpus = [gpu for server in current_servers for gpu in server["gpus"]]
        systems = [server["system"] for server in current_servers if server["system"]]
        memory_total = sum(float(gpu["memory_total_mib"] or 0) for gpu in gpus)
        memory_used = sum(float(gpu["memory_used_mib"] or 0) for gpu in gpus)
        busy = sum(
            float(gpu["utilization_gpu_pct"] or 0) >= self._thresholds.gpu_busy_pct
            for gpu in gpus
        )
        cpu_values = [
            float(system["cpu_usage_pct"])
            for system in systems
            if system["cpu_usage_pct"] is not None
        ]
        system_memory_total = sum(
            float(system["memory_total_mib"]) for system in systems
        )
        system_memory_used = sum(float(system["memory_used_mib"]) for system in systems)
        swap_total = sum(float(system["swap_total_mib"]) for system in systems)
        swap_used = sum(float(system["swap_used_mib"]) for system in systems)
        disk_total = sum(float(system["disk_total_mib"]) for system in systems)
        disk_used = sum(float(system["disk_used_mib"]) for system in systems)
        network_rx = sum(float(system["network_rx_bps"] or 0) for system in systems)
        network_tx = sum(float(system["network_tx_bps"] or 0) for system in systems)
        disk_read = sum(float(system["disk_read_bps"] or 0) for system in systems)
        disk_write = sum(float(system["disk_write_bps"] or 0) for system in systems)
        active_incidents = len(active_conditions)
        critical_incidents = sum(
            condition["severity"] == "critical" for condition in active_conditions
        )
        active_incident_hosts = frozenset(host_incidents)
        maintenance_hosts = frozenset(active_maintenance)
        actionable_conditions = [
            condition for condition in active_conditions if condition["actionable"]
        ]
        actionable_incidents = len(actionable_conditions)
        actionable_critical = sum(
            condition["severity"] == "critical" for condition in actionable_conditions
        )
        actionable_incident_hosts = frozenset(
            str(condition["host"]) for condition in actionable_conditions
        )
        non_online_hosts = {
            str(server["host"]) for server in servers if server["status"] != "online"
        }
        untracked_non_online_hosts = {
            host for host in non_online_hosts if host not in active_incident_hosts
        }
        actionable_issue_hosts = (
            untracked_non_online_hosts - maintenance_hosts
        ) | actionable_incident_hosts
        snapshot = {
            "version": self._version,
            "appVersion": __version__,
            "incidentVersion": self._incident_revision,
            "generatedAt": utc_now(),
            "startedAt": self._started_at,
            "pollIntervalSeconds": self._poll_interval_seconds,
            "collectionStaleAfterSeconds": self._collection_stale_after_seconds,
            "lastPollCompletedAt": self._last_poll_completed_at,
            "lastPollDurationMs": self._last_poll_duration_ms,
            "collectorError": self._collector_error,
            "persistence": persistence_status,
            "notifications": notification_status,
            "thresholds": self._thresholds.to_dict(),
            "stats": {
                "servers": len(servers),
                "onlineServers": online,
                "issueServers": len(non_online_hosts | active_incident_hosts),
                "incidentServers": len(active_incident_hosts),
                "actionableIssueServers": len(actionable_issue_hosts),
                "actionableIncidentServers": len(actionable_incident_hosts),
                "maintenanceServers": len(maintenance_hosts),
                "staleServers": sum(bool(server["stale"]) for server in servers),
                "pollingServers": sum(bool(server["polling"]) for server in servers),
                "activeIncidents": active_incidents,
                "criticalIncidents": critical_incidents,
                "actionableIncidents": actionable_incidents,
                "actionableCriticalIncidents": actionable_critical,
                "gpus": len(gpus),
                "busyGpus": busy,
                "memoryTotalMiB": round(memory_total, 1),
                "memoryUsedMiB": round(memory_used, 1),
                "cpuAveragePct": round(sum(cpu_values) / len(cpu_values), 2)
                if cpu_values
                else None,
                "cpuCores": sum(int(system["cpu_cores"]) for system in systems),
                "systemMemoryTotalMiB": round(system_memory_total, 1),
                "systemMemoryUsedMiB": round(system_memory_used, 1),
                "swapTotalMiB": round(swap_total, 1),
                "swapUsedMiB": round(swap_used, 1),
                "diskTotalMiB": round(disk_total, 1),
                "diskUsedMiB": round(disk_used, 1),
                "networkRxBps": round(network_rx, 1),
                "networkTxBps": round(network_tx, 1),
                "diskReadBps": round(disk_read, 1),
                "diskWriteBps": round(disk_write, 1),
            },
            "servers": servers,
        }
        self._snapshot_cache_key = cache_key
        self._snapshot_cache = snapshot
        return snapshot

    def _refresh_adapter_status_locked(
        self,
    ) -> tuple[dict[str, object], dict[str, object]]:
        persistence_status = self._persistence.status()
        notification_status = self._notifications.status()
        signature = (repr(persistence_status), repr(notification_status))
        if signature != self._adapter_status_signature:
            self._adapter_status_signature = signature
            self._adapter_statuses = (persistence_status, notification_status)
            self._version += 1
            self._snapshot_cache_key = None
            self._snapshot_cache = None
            self._condition.notify_all()
        return self._adapter_statuses


class MonitorService:
    def __init__(
        self,
        config: MonitorConfig,
        host_source: HostSource,
        probe: ResourceProbe,
        state: StateStore,
    ) -> None:
        self._config = config
        self._config_lock = threading.Lock()
        self._config_update_lock = threading.Lock()
        self._config_generation = 0
        self._host_source = host_source
        self._probe = probe
        self._state = state
        self._failure_counts: dict[str, int] = {}
        self._next_probe_at: dict[str, float] = {}
        self._backoff_policies: dict[str, tuple[float, float]] = {}
        self._scheduler_wakeup = threading.Event()
        self._probe_control_lock = threading.Lock()
        self._runtime_hosts: set[str] = set()
        self._runtime_in_flight: set[str] = set()
        self._manual_probe_requests: set[str] = set()
        self._last_manual_probe_at: dict[str, float] = {}

    def _apply_discovery_metadata(self, discovery: HostDiscoverySnapshot) -> None:
        self._state.set_host_groups(discovery.host_groups)
        self._state.set_topology(discovery.topology)

    def request_probe(self, host: str) -> dict[str, object]:
        """Queue one bounded host probe without changing the periodic schedule."""
        now = time.monotonic()
        config, _ = self._config_snapshot()
        cooldown = config.manual_probe_cooldown_seconds
        with self._probe_control_lock:
            if host not in self._runtime_hosts:
                return {"status": "unknown_host", "accepted": False, "host": host}
            if host in self._runtime_in_flight:
                return {"status": "in_progress", "accepted": False, "host": host}
            if host in self._manual_probe_requests:
                return {"status": "queued", "accepted": True, "host": host}
            elapsed = now - self._last_manual_probe_at.get(host, float("-inf"))
            if elapsed < cooldown:
                return {
                    "status": "rate_limited",
                    "accepted": False,
                    "host": host,
                    "retryAfterSeconds": round(cooldown - elapsed, 2),
                }
            self._manual_probe_requests.add(host)
            self._last_manual_probe_at[host] = now
        self._scheduler_wakeup.set()
        return {"status": "queued", "accepted": True, "host": host}

    @staticmethod
    def _backoff_delay(
        interval_seconds: float,
        failures: int,
        host: str,
        jitter_pct: float,
    ) -> float:
        multiplier = 2 ** min(failures - 1, 10)
        base_delay = min(
            _MAX_FAILURE_BACKOFF_SECONDS,
            interval_seconds * multiplier,
        )
        if jitter_pct <= 0:
            return base_delay
        jitter_unit = zlib.crc32(f"{host}\0{failures}".encode()) / 0xFFFFFFFF
        return base_delay * (1 - jitter_pct / 100 * jitter_unit)

    def update_config(self, config: MonitorConfig) -> None:
        """Atomically replace configuration used by future probes."""
        with self._config_update_lock:
            try:
                discovery: HostDiscoverySnapshot | None = resolve_host_discovery(
                    self._host_source, config
                )
            except (OSError, ValueError):
                discovery = None
            with self._config_lock:
                self._state.reconfigure(
                    config, discovery.hosts if discovery is not None else None
                )
                if discovery is not None:
                    self._apply_discovery_metadata(discovery)
                self._config = config
                self._config_generation += 1
            self._scheduler_wakeup.set()

    def _config_snapshot(self) -> tuple[MonitorConfig, int]:
        with self._config_lock:
            return self._config, self._config_generation

    def shutdown_timeout_seconds(self) -> float:
        """Return a bounded wait that covers every currently configured probe."""
        config, _ = self._config_snapshot()
        host_timeouts = (
            override.probe_timeout_seconds
            for _, override in config.host_overrides
            if override.probe_timeout_seconds is not None
        )
        return max((config.probe_timeout_seconds, *host_timeouts)) + 1

    def stop(self) -> None:
        """Wake the scheduler and cancel probe-owned child processes, when supported."""
        self._scheduler_wakeup.set()
        if isinstance(self._host_source, CancellableHostSource):
            self._host_source.cancel()
        if isinstance(self._probe, CancellableResourceProbe):
            self._probe.cancel()

    def _host_poll_interval(self, host: str, config: MonitorConfig) -> float:
        override = config.host_override(host)
        if override and override.poll_interval_seconds is not None:
            return override.poll_interval_seconds
        return self._state.poll_interval_seconds()

    def _rebase_failure_backoff(self, now: float, config: MonitorConfig) -> None:
        for host, failures in self._failure_counts.items():
            current_interval = self._host_poll_interval(host, config)
            current_policy = (current_interval, config.retry_jitter_pct)
            previous_deadline = self._next_probe_at.get(host)
            if previous_deadline is None:
                continue
            previous_policy = self._backoff_policies.get(host, current_policy)
            if current_policy == previous_policy:
                continue
            previous_delay = self._backoff_delay(
                previous_policy[0], failures, host, previous_policy[1]
            )
            failed_at = previous_deadline - previous_delay
            new_deadline = failed_at + self._backoff_delay(
                current_interval,
                failures,
                host,
                config.retry_jitter_pct,
            )
            self._next_probe_at[host] = new_deadline
            self._backoff_policies[host] = current_policy
            self._state.reschedule_retry(host, max(0.0, new_deadline - now))

    def poll_once(self) -> None:
        config, generation = self._config_snapshot()
        try:
            discovery = resolve_host_discovery(self._host_source, config)
            hosts = discovery.hosts
        except (OSError, ValueError) as exc:
            print(f"Host discovery failed: {exc}", file=sys.stderr)
            self._state.set_collector_error(
                "SSH host discovery failed; check the monitor configuration"
            )
            return
        if isinstance(self._probe, InventoryAwareResourceProbe):
            self._probe.retain_hosts(set(hosts))
        self._state.set_collector_error(None)
        self._apply_discovery_metadata(discovery)
        self._state.set_hosts(hosts)
        active_hosts = set(hosts)
        self._failure_counts = {
            host: failures
            for host, failures in self._failure_counts.items()
            if host in active_hosts
        }
        self._next_probe_at = {
            host: retry_at
            for host, retry_at in self._next_probe_at.items()
            if host in active_hosts
        }
        self._backoff_policies = {
            host: policy
            for host, policy in self._backoff_policies.items()
            if host in active_hosts
        }
        now = time.monotonic()
        self._rebase_failure_backoff(now, config)
        due_hosts = tuple(
            host for host in hosts if self._next_probe_at.get(host, 0) <= now
        )
        if not due_hosts:
            return

        with ThreadPoolExecutor(
            max_workers=min(config.max_workers, len(due_hosts)),
            thread_name_prefix="gpu-probe",
        ) as pool:
            with self._config_lock:
                if generation != self._config_generation:
                    return
                self._state.begin_poll(due_hosts)
                futures = {
                    pool.submit(self._probe.probe, host, config): (
                        host,
                        self._state.host_incarnation(host),
                    )
                    for host in due_hosts
                }
            for future in as_completed(futures):
                host, incarnation = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    _log_unexpected_collector_exception(host, exc)
                    self._state.set_collector_error(
                        "Resource collector encountered an internal probe failure; "
                        "inspect service logs"
                    )
                    result = ProbeResult(
                        host=host,
                        status="error",
                        latency_ms=0,
                        message="Unexpected collector error",
                    )
                if (
                    incarnation is None
                    or self._state.host_incarnation(host) != incarnation
                ):
                    continue
                if result.status == "online":
                    self._failure_counts.pop(host, None)
                    self._backoff_policies.pop(host, None)
                    override = config.host_override(host)
                    if override and override.poll_interval_seconds is not None:
                        self._next_probe_at[host] = now + override.poll_interval_seconds
                    else:
                        self._next_probe_at.pop(host, None)
                    retry_after_seconds = None
                else:
                    failures = self._failure_counts.get(host, 0) + 1
                    self._failure_counts[host] = failures
                    interval = self._host_poll_interval(host, config)
                    delay = self._backoff_delay(
                        interval,
                        failures,
                        host,
                        config.retry_jitter_pct,
                    )
                    self._next_probe_at[host] = time.monotonic() + delay
                    self._backoff_policies[host] = (
                        interval,
                        config.retry_jitter_pct,
                    )
                    retry_after_seconds = delay
                self._state.apply(
                    result,
                    retry_after_seconds=retry_after_seconds,
                    expected_incarnation=incarnation,
                )

    def run(self, stop_event: threading.Event) -> None:
        """Run independently paced host probes on one bounded worker pool."""
        pool = ThreadPoolExecutor(
            max_workers=_MAX_PROBE_WORKERS,
            thread_name_prefix="gpu-probe",
        )
        in_flight: dict[Future[ProbeResult], _ScheduledProbe] = {}
        in_flight_hosts: set[str] = set()
        batches: dict[int, _ProbeBatch] = {}
        healthy_started_at: dict[str, float] = {}
        healthy_intervals: dict[str, float] = {}
        active_hosts: tuple[str, ...] = ()
        active_host_set: set[str] = set()
        inventory_refresh_at = 0.0
        inventory_generation = -1
        next_batch_id = 1
        last_completed_batch_id = 0

        try:
            while not stop_event.is_set():
                self._scheduler_wakeup.clear()
                now = time.monotonic()
                config, generation = self._config_snapshot()
                if isinstance(self._probe, AttendedAwareResourceProbe):
                    self._probe.set_attended(self._state.dashboard_attended())

                if now >= inventory_refresh_at or generation != inventory_generation:
                    try:
                        discovery = resolve_host_discovery(self._host_source, config)
                        discovered = discovery.hosts
                    except (OSError, ValueError) as exc:
                        print(f"Host discovery failed: {exc}", file=sys.stderr)
                        self._state.set_collector_error(
                            "SSH host discovery failed; check the monitor configuration"
                        )
                        inventory_refresh_at = now + min(
                            self._state.poll_interval_seconds(), 5.0
                        )
                        inventory_generation = generation
                    else:
                        if generation != self._config_snapshot()[1]:
                            continue
                        previous_host_set = active_host_set
                        active_hosts = discovered
                        active_host_set = set(discovered)
                        if active_host_set != previous_host_set and isinstance(
                            self._probe, InventoryAwareResourceProbe
                        ):
                            self._probe.retain_hosts(active_host_set)
                        with self._probe_control_lock:
                            self._runtime_hosts = set(discovered)
                            self._manual_probe_requests.intersection_update(
                                active_host_set
                            )
                        self._state.set_collector_error(None)
                        self._apply_discovery_metadata(discovery)
                        self._state.set_hosts(discovered)
                        self._prune_schedules(active_host_set)
                        healthy_started_at = {
                            host: started
                            for host, started in healthy_started_at.items()
                            if host in active_host_set
                        }
                        healthy_intervals = {
                            host: interval
                            for host, interval in healthy_intervals.items()
                            if host in active_host_set
                        }
                        inventory_generation = generation
                        inventory_refresh_at = now + self._state.poll_interval_seconds()

                self._rebase_failure_backoff(now, config)
                with self._probe_control_lock:
                    manually_due = tuple(
                        host
                        for host in self._manual_probe_requests
                        if host in active_host_set
                        and host not in self._runtime_in_flight
                    )
                for host in manually_due:
                    self._next_probe_at[host] = 0.0
                for host, started_at in healthy_started_at.items():
                    if host in self._failure_counts or host in in_flight_hosts:
                        continue
                    interval = self._host_poll_interval(host, config)
                    if healthy_intervals.get(host) == interval:
                        continue
                    self._next_probe_at[host] = started_at + interval
                    healthy_intervals[host] = interval

                completed = tuple(future for future in in_flight if future.done())
                for future in completed:
                    scheduled = in_flight.pop(future)
                    in_flight_hosts.discard(scheduled.host)
                    with self._probe_control_lock:
                        self._runtime_in_flight.discard(scheduled.host)
                    completed_at = time.monotonic()
                    try:
                        result = future.result()
                    except Exception as exc:
                        _log_unexpected_collector_exception(scheduled.host, exc)
                        self._state.set_collector_error(
                            "Resource collector encountered an internal probe failure; "
                            "inspect service logs"
                        )
                        result = ProbeResult(
                            host=scheduled.host,
                            status="error",
                            latency_ms=0,
                            message="Unexpected collector error",
                        )

                    batch = batches.get(scheduled.batch_id)
                    batch_duration_seconds: float | None = None
                    if batch is not None:
                        batch.remaining -= 1
                        if batch.remaining == 0:
                            if scheduled.batch_id > last_completed_batch_id:
                                batch_duration_seconds = completed_at - batch.started_at
                                last_completed_batch_id = scheduled.batch_id
                            del batches[scheduled.batch_id]

                    if (
                        scheduled.host in active_host_set
                        and self._state.host_incarnation(scheduled.host)
                        == scheduled.incarnation
                    ):
                        current_config, _ = self._config_snapshot()
                        if result.status == "online":
                            self._failure_counts.pop(scheduled.host, None)
                            self._backoff_policies.pop(scheduled.host, None)
                            interval = self._host_poll_interval(
                                scheduled.host, current_config
                            )
                            self._next_probe_at[scheduled.host] = max(
                                scheduled.started_at + interval,
                                completed_at,
                            )
                            healthy_started_at[scheduled.host] = scheduled.started_at
                            healthy_intervals[scheduled.host] = interval
                            retry_after_seconds = None
                        else:
                            healthy_started_at.pop(scheduled.host, None)
                            healthy_intervals.pop(scheduled.host, None)
                            failures = self._failure_counts.get(scheduled.host, 0) + 1
                            self._failure_counts[scheduled.host] = failures
                            interval = self._host_poll_interval(
                                scheduled.host, current_config
                            )
                            delay = self._backoff_delay(
                                interval,
                                failures,
                                scheduled.host,
                                current_config.retry_jitter_pct,
                            )
                            self._next_probe_at[scheduled.host] = completed_at + delay
                            self._backoff_policies[scheduled.host] = (
                                interval,
                                current_config.retry_jitter_pct,
                            )
                            retry_after_seconds = delay
                        self._state.apply(
                            result,
                            retry_after_seconds=retry_after_seconds,
                            poll_cycle_duration_seconds=batch_duration_seconds,
                            expected_incarnation=scheduled.incarnation,
                        )
                    else:
                        if isinstance(self._probe, InventoryAwareResourceProbe):
                            # The removed host may have refreshed its rate
                            # baseline while its already-running probe was
                            # completing.
                            self._probe.retain_hosts(active_host_set)
                        if batch_duration_seconds is not None:
                            self._state.record_poll_cycle(batch_duration_seconds)

                # A hot config replacement landed after this iteration took its
                # snapshot; drop this round's submissions and restart the loop,
                # where the update's wakeup re-snapshots the config and the
                # inventory branch rebuilds the active host set before probing.
                with self._config_lock:
                    if generation != self._config_generation:
                        continue
                    available_workers = max(0, config.max_workers - len(in_flight))
                    if available_workers:
                        due_hosts = tuple(
                            item[2]
                            for item in nsmallest(
                                available_workers,
                                (
                                    (
                                        self._next_probe_at.get(host, 0.0),
                                        order,
                                        host,
                                    )
                                    for order, host in enumerate(active_hosts)
                                    if host not in in_flight_hosts
                                    and self._next_probe_at.get(host, 0.0) <= now
                                ),
                            )
                        )
                        if due_hosts:
                            batch_id = next_batch_id
                            next_batch_id += 1
                            batch_started_at = time.monotonic()
                            batches[batch_id] = _ProbeBatch(
                                started_at=batch_started_at,
                                remaining=len(due_hosts),
                            )
                            self._state.begin_poll(due_hosts)
                            for host in due_hosts:
                                in_flight_hosts.add(host)
                                with self._probe_control_lock:
                                    self._runtime_in_flight.add(host)
                                    self._manual_probe_requests.discard(host)
                                try:
                                    future = pool.submit(
                                        self._probe.probe, host, config
                                    )
                                except Exception:
                                    in_flight_hosts.discard(host)
                                    with self._probe_control_lock:
                                        self._runtime_in_flight.discard(host)
                                    raise
                                incarnation = self._state.host_incarnation(host)
                                assert incarnation is not None
                                in_flight[future] = _ScheduledProbe(
                                    host=host,
                                    started_at=batch_started_at,
                                    batch_id=batch_id,
                                    incarnation=incarnation,
                                )
                                future.add_done_callback(self._wake_scheduler)

                wait_until = inventory_refresh_at
                if len(in_flight) < config.max_workers:
                    deadlines = (
                        self._next_probe_at.get(host, 0.0)
                        for host in active_hosts
                        if host not in in_flight_hosts
                    )
                    wait_until = min(
                        wait_until,
                        min(deadlines, default=wait_until),
                    )
                wait_seconds = max(0.0, wait_until - time.monotonic())
                # A stop() racing with the top-of-loop clear() may have had its
                # wakeup consumed; recheck before committing to a long wait.
                if stop_event.is_set():
                    break
                self._scheduler_wakeup.wait(wait_seconds)
        except Exception as exc:
            _log_unexpected_collector_exception("scheduler", exc)
            print("Unexpected collector scheduler failure", file=sys.stderr)
            self._state.set_collector_error(
                "Resource collector failed unexpectedly; restart required"
            )
        finally:
            with self._probe_control_lock:
                self._runtime_hosts.clear()
                self._runtime_in_flight.clear()
                self._manual_probe_requests.clear()
            pool.shutdown(wait=True, cancel_futures=True)

    def _prune_schedules(self, active_hosts: set[str]) -> None:
        self._failure_counts = {
            host: failures
            for host, failures in self._failure_counts.items()
            if host in active_hosts
        }
        self._next_probe_at = {
            host: retry_at
            for host, retry_at in self._next_probe_at.items()
            if host in active_hosts
        }
        self._backoff_policies = {
            host: policy
            for host, policy in self._backoff_policies.items()
            if host in active_hosts
        }
        with self._probe_control_lock:
            self._manual_probe_requests.intersection_update(active_hosts)
            self._last_manual_probe_at = {
                host: requested_at
                for host, requested_at in self._last_manual_probe_at.items()
                if host in active_hosts
            }

    def _wake_scheduler(self, _future: Future[ProbeResult]) -> None:
        self._scheduler_wakeup.set()
