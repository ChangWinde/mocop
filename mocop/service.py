from __future__ import annotations

import copy
import math
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import __version__
from .config import IncidentConfig, MonitorConfig, ThresholdConfig
from .discovery import HostSource
from .incidents import IncidentPolicy, IncidentTracker, ThresholdIncidentPolicy
from .models import ProbeResult, ServerState, utc_after, utc_now
from .probe import ResourceProbe

_MAX_FAILURE_BACKOFF_SECONDS = 60.0
_MIN_RUNTIME_POLL_INTERVAL_SECONDS = 2.0
_MAX_RUNTIME_POLL_INTERVAL_SECONDS = 60.0


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
    ) -> None:
        self._condition = threading.Condition()
        self._servers: dict[str, ServerState] = {}
        self._history: dict[str, deque[dict[str, object]]] = {}
        self._version = 0
        self._collector_error: str | None = None
        self._poll_interval_seconds = poll_interval_seconds
        self._collection_stale_cycles = collection_stale_cycles
        self._collection_stale_after_seconds = (
            poll_interval_seconds * collection_stale_cycles
        )
        self._poll_interval_changed = threading.Event()
        self._thresholds = thresholds or ThresholdConfig()
        self._history_points = history_points
        self._incidents = IncidentTracker(
            incident_policy
            or ThresholdIncidentPolicy(
                self._thresholds,
                expected_gpu_counts=expected_gpu_counts,
                incidents=incidents,
            ),
            incident_history_points,
        )
        self._started_at = utc_now()
        self._last_poll_completed_at: str | None = None
        self._last_poll_duration_ms: int | None = None

    def set_hosts(self, hosts: tuple[str, ...]) -> None:
        desired = set(hosts)
        with self._condition:
            changed = False
            for host in list(self._servers):
                if host not in desired:
                    del self._servers[host]
                    self._history.pop(host, None)
                    changed = True
            for host in hosts:
                if host not in self._servers:
                    self._servers[host] = ServerState(host=host)
                    self._history[host] = deque(maxlen=self._history_points)
                    changed = True
            self._incidents.remove_hosts(desired)
            if changed:
                self._publish_locked()

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
            raise ValueError("poll interval must be between 2 and 60 seconds")
        with self._condition:
            if interval == self._poll_interval_seconds:
                return interval
            self._poll_interval_seconds = interval
            self._collection_stale_after_seconds = (
                interval * self._collection_stale_cycles
            )
            self._poll_interval_changed.set()
            self._publish_locked()
        return interval

    def wait_for_poll_interval_change(self, timeout_seconds: float) -> bool:
        changed = self._poll_interval_changed.wait(max(0.0, timeout_seconds))
        if changed:
            self._poll_interval_changed.clear()
        return changed

    def begin_poll(self, hosts: tuple[str, ...]) -> None:
        with self._condition:
            for host in hosts:
                state = self._servers.get(host)
                if state is not None:
                    state.polling = True

    def apply(
        self,
        result: ProbeResult,
        retry_after_seconds: float | None = None,
    ) -> None:
        with self._condition:
            state = self._servers.get(result.host)
            if state is None:
                return
            self._incidents.update(result)
            next_retry_at = (
                utc_after(retry_after_seconds)
                if retry_after_seconds is not None
                else None
            )
            state.apply(result, next_retry_at=next_retry_at)
            if result.status == "online" and result.system is not None:
                self._history[result.host].append(self._history_point(result))
            self._publish_locked()

    def reschedule_retry(self, host: str, retry_after_seconds: float) -> None:
        """Publish a rebased retry deadline after the runtime cadence changes."""
        with self._condition:
            state = self._servers.get(host)
            if state is None or state.status == "online":
                return
            state.next_retry_at = utc_after(retry_after_seconds)
            self._publish_locked()

    def record_poll_cycle(self, duration_seconds: float) -> None:
        """Record and publish the authoritative completion time for this cycle."""
        with self._condition:
            self._last_poll_completed_at = utc_now()
            self._last_poll_duration_ms = max(0, round(duration_seconds * 1000))
            self._publish_locked()

    def history(self, host: str, limit: int) -> dict[str, object] | None:
        with self._condition:
            points = self._history.get(host)
            if points is None:
                return None
            return {
                "host": host,
                "pollIntervalSeconds": self._poll_interval_seconds,
                "maxPoints": self._history_points,
                "points": copy.deepcopy(list(points)[-limit:]),
            }

    def incidents(self, limit: int) -> dict[str, object]:
        with self._condition:
            return copy.deepcopy(self._incidents.snapshot(limit))

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
                reason = "host discovery failed"
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
                "version": self._version,
                "startedAt": self._started_at,
            }

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return self._snapshot_locked()

    def wait_for_update(
        self, after_version: int, timeout_seconds: float
    ) -> dict[str, object] | None:
        with self._condition:
            if self._version <= after_version:
                self._condition.wait_for(
                    lambda: self._version > after_version, timeout=timeout_seconds
                )
            if self._version <= after_version:
                return None
            return self._snapshot_locked()

    def _publish_locked(self) -> None:
        self._version += 1
        self._condition.notify_all()

    @staticmethod
    def _history_point(result: ProbeResult) -> dict[str, object]:
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
        gpu_memory_used = sum(gpu.memory_used_mib or 0 for gpu in result.gpus)
        gpu_memory_total = sum(gpu.memory_total_mib or 0 for gpu in result.gpus)
        temperatures = [
            gpu.temperature_c for gpu in result.gpus if gpu.temperature_c is not None
        ]
        return {
            "observedAt": result.observed_at,
            "cpuUsagePct": system.cpu_usage_pct,
            "memoryUsagePct": percentage(
                system.memory_used_mib, system.memory_total_mib
            ),
            "swapUsagePct": percentage(system.swap_used_mib, system.swap_total_mib),
            "diskUsagePct": percentage(system.disk_used_mib, system.disk_total_mib),
            "networkRxBps": system.network_rx_bps,
            "networkTxBps": system.network_tx_bps,
            "diskReadBps": system.disk_read_bps,
            "diskWriteBps": system.disk_write_bps,
            "gpuUsagePct": round(sum(gpu_usage) / len(gpu_usage), 2)
            if gpu_usage
            else None,
            "gpuMemoryUsagePct": percentage(gpu_memory_used, gpu_memory_total),
            "gpuTemperatureC": max(temperatures) if temperatures else None,
        }

    def _snapshot_locked(self) -> dict[str, object]:
        servers = [state.to_dict() for state in self._servers.values()]
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
        active_incidents, critical_incidents, active_incident_hosts = (
            self._incidents.counts()
        )
        non_online_hosts = {
            str(server["host"]) for server in servers if server["status"] != "online"
        }
        return copy.deepcopy(
            {
                "version": self._version,
                "appVersion": __version__,
                "incidentVersion": self._incidents.version,
                "generatedAt": utc_now(),
                "startedAt": self._started_at,
                "pollIntervalSeconds": self._poll_interval_seconds,
                "collectionStaleAfterSeconds": self._collection_stale_after_seconds,
                "lastPollCompletedAt": self._last_poll_completed_at,
                "lastPollDurationMs": self._last_poll_duration_ms,
                "collectorError": self._collector_error,
                "thresholds": self._thresholds.to_dict(),
                "stats": {
                    "servers": len(servers),
                    "onlineServers": online,
                    "issueServers": len(non_online_hosts | active_incident_hosts),
                    "incidentServers": len(active_incident_hosts),
                    "staleServers": sum(bool(server["stale"]) for server in servers),
                    "pollingServers": sum(
                        bool(server["polling"]) for server in servers
                    ),
                    "activeIncidents": active_incidents,
                    "criticalIncidents": critical_incidents,
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
        )


class MonitorService:
    def __init__(
        self,
        config: MonitorConfig,
        host_source: HostSource,
        probe: ResourceProbe,
        state: StateStore,
    ) -> None:
        self._config = config
        self._host_source = host_source
        self._probe = probe
        self._state = state
        self._failure_counts: dict[str, int] = {}
        self._next_probe_at: dict[str, float] = {}
        self._backoff_intervals: dict[str, float] = {}

    @staticmethod
    def _backoff_delay(interval_seconds: float, failures: int) -> float:
        multiplier = 2 ** min(failures - 1, 10)
        return min(_MAX_FAILURE_BACKOFF_SECONDS, interval_seconds * multiplier)

    def _host_poll_interval(self, host: str) -> float:
        override = self._config.host_override(host)
        if override and override.poll_interval_seconds is not None:
            return override.poll_interval_seconds
        return self._state.poll_interval_seconds()

    def _rebase_failure_backoff(self, now: float) -> None:
        for host, failures in self._failure_counts.items():
            current_interval = self._host_poll_interval(host)
            previous_deadline = self._next_probe_at.get(host)
            if previous_deadline is None:
                continue
            previous_interval = self._backoff_intervals.get(host, current_interval)
            if current_interval == previous_interval:
                continue
            previous_delay = self._backoff_delay(previous_interval, failures)
            failed_at = previous_deadline - previous_delay
            new_deadline = failed_at + self._backoff_delay(current_interval, failures)
            self._next_probe_at[host] = new_deadline
            self._backoff_intervals[host] = current_interval
            self._state.reschedule_retry(host, max(0.0, new_deadline - now))

    def poll_once(self) -> None:
        try:
            hosts = self._host_source.hosts(self._config)
        except (OSError, ValueError) as exc:
            print(f"Host discovery failed: {exc}", file=sys.stderr)
            self._state.set_collector_error(
                "SSH host discovery failed; check the monitor configuration"
            )
            return

        self._state.set_collector_error(None)
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
        self._backoff_intervals = {
            host: interval
            for host, interval in self._backoff_intervals.items()
            if host in active_hosts
        }
        now = time.monotonic()
        self._rebase_failure_backoff(now)
        due_hosts = tuple(
            host for host in hosts if self._next_probe_at.get(host, 0) <= now
        )
        self._state.begin_poll(due_hosts)
        if not due_hosts:
            return

        with ThreadPoolExecutor(
            max_workers=min(self._config.max_workers, len(due_hosts)),
            thread_name_prefix="gpu-probe",
        ) as pool:
            futures = {
                pool.submit(self._probe.probe, host, self._config): host
                for host in due_hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    result = future.result()
                except Exception:
                    result = ProbeResult(
                        host=host,
                        status="error",
                        latency_ms=0,
                        message="Unexpected collector error",
                    )
                if result.status == "online":
                    self._failure_counts.pop(host, None)
                    self._backoff_intervals.pop(host, None)
                    override = self._config.host_override(host)
                    if override and override.poll_interval_seconds is not None:
                        self._next_probe_at[host] = now + override.poll_interval_seconds
                    else:
                        self._next_probe_at.pop(host, None)
                    retry_after_seconds = None
                else:
                    failures = self._failure_counts.get(host, 0) + 1
                    self._failure_counts[host] = failures
                    interval = self._host_poll_interval(host)
                    delay = self._backoff_delay(interval, failures)
                    self._next_probe_at[host] = time.monotonic() + delay
                    self._backoff_intervals[host] = interval
                    retry_after_seconds = delay
                self._state.apply(result, retry_after_seconds=retry_after_seconds)

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                print("Unexpected collector cycle failure", file=sys.stderr)
                self._state.set_collector_error(
                    "Resource collector failed unexpectedly; retrying automatically"
                )
            elapsed = max(0, time.monotonic() - started)
            self._state.record_poll_cycle(elapsed)
            deadline = time.monotonic() + max(
                0,
                self._state.poll_interval_seconds() - elapsed,
            )
            while not stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self._state.wait_for_poll_interval_change(min(1.0, remaining)):
                    break
