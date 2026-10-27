"""Independent host scheduling, background discovery and completed-result drain."""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
import traceback
import zlib
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from heapq import nsmallest
from typing import TYPE_CHECKING

from .config import MonitorConfig
from .discovery import (
    CancellableHostSource,
    HostDiscoverySnapshot,
    HostSource,
    resolve_host_discovery,
)
from .models import ProbeResult
from .probe import (
    AttendedAwareResourceProbe,
    CancellableResourceProbe,
    InventoryAwareResourceProbe,
    ResourceProbe,
)

if TYPE_CHECKING:
    from .service import StateStore

_MAX_FAILURE_BACKOFF_SECONDS = 60.0
_MAX_PROBE_WORKERS = 64
_LOGGER = logging.getLogger("mocop.service")


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


def run_scheduler(self: MonitorService, stop_event: threading.Event) -> None:
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

    discovery_pool = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="gpu-discovery"
    )
    discovery_future: Future[HostDiscoverySnapshot] | None = None
    discovery_generation = -1

    def discovery_failed(exc: Exception, now: float) -> None:
        nonlocal inventory_refresh_at
        print(f"Host discovery failed: {exc}", file=sys.stderr)
        self._state.set_collector_error(
            "SSH host discovery failed; check the monitor configuration"
        )
        inventory_refresh_at = now + min(self._state.poll_interval_seconds(), 5.0)

    def drain_completed() -> None:
        nonlocal last_completed_batch_id
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
                result = self._state.unexpected_probe_result(scheduled.host)

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
                    interval = self._host_poll_interval(scheduled.host, current_config)
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
                    interval = self._host_poll_interval(scheduled.host, current_config)
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

    try:
        while not stop_event.is_set():
            self._scheduler_wakeup.clear()
            now = time.monotonic()
            config, generation = self._config_snapshot()
            if isinstance(self._probe, AttendedAwareResourceProbe):
                self._probe.set_attended(self._state.dashboard_attended())

            if generation != inventory_generation:
                inventory_refresh_at = 0.0
                inventory_generation = generation
            discovery = None
            if discovery_future is not None and discovery_future.done():
                finished = discovery_future
                discovery_future = None
                if discovery_generation == generation:
                    try:
                        discovery = finished.result()
                    except (OSError, ValueError) as exc:
                        discovery_failed(exc, now)
                    else:
                        inventory_refresh_at = now + self._state.poll_interval_seconds()
            if discovery_future is None and now >= inventory_refresh_at:
                if config.ssh_discovery.mode == "topology":
                    discovery_generation = generation
                    discovery_future = discovery_pool.submit(
                        resolve_host_discovery, self._host_source, config
                    )
                    discovery_future.add_done_callback(self._wake_scheduler)
                else:
                    try:
                        discovery = resolve_host_discovery(self._host_source, config)
                    except (OSError, ValueError) as exc:
                        discovery_failed(exc, now)
                    else:
                        inventory_refresh_at = now + self._state.poll_interval_seconds()
            with self._config_lock:
                if generation != self._config_generation:
                    continue
                if discovery is not None:
                    self._discovered_hosts = tuple(
                        host
                        for host in discovery.hosts
                        if host not in config.exclude_hosts
                    )
                    self._state.set_collector_error(None)
                    self._apply_discovery_metadata(discovery)
                    self._state.set_hosts(self._discovered_hosts)
                active_hosts = self._discovered_hosts
                previous_host_set = active_host_set
                active_host_set = set(active_hosts)
                with self._probe_control_lock:
                    self._runtime_hosts = set(active_hosts)
                    self._manual_probe_requests.intersection_update(active_host_set)
                if active_host_set != previous_host_set:
                    if isinstance(self._probe, InventoryAwareResourceProbe):
                        self._probe.retain_hosts(active_host_set)
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

            self._rebase_failure_backoff(now, config)
            with self._probe_control_lock:
                manually_due = tuple(
                    host
                    for host in self._manual_probe_requests
                    if host in active_host_set and host not in self._runtime_in_flight
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

            drain_completed()

            # A hot config replacement landed after this iteration took its
            # snapshot; drop this round's submissions and restart the loop,
            # where the update's wakeup re-snapshots the config and the
            # inventory branch rebuilds the active host set before probing.
            with self._config_lock:
                if stop_event.is_set():
                    break
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
                                future = pool.submit(self._probe.probe, host, config)
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

            wait_until = (
                float("inf") if discovery_future is not None else inventory_refresh_at
            )
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
            self._scheduler_wakeup.wait(
                None if math.isinf(wait_seconds) else wait_seconds
            )
    except Exception as exc:
        _log_unexpected_collector_exception("scheduler", exc)
        print("Unexpected collector scheduler failure", file=sys.stderr)
        self._state.set_collector_error(
            "Resource collector failed unexpectedly; restart required"
        )
    finally:
        try:
            drain_completed()
        finally:
            discovery_pool.shutdown(wait=False, cancel_futures=True)
            pool.shutdown(wait=False, cancel_futures=True)
        with self._probe_control_lock:
            self._runtime_hosts.clear()
            self._runtime_in_flight.clear()
            self._manual_probe_requests.clear()


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
        self._discovered_hosts: tuple[str, ...] = ()
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
                self._discovered_hosts = tuple(
                    host
                    for host in (
                        discovery.hosts
                        if discovery is not None
                        else self._discovered_hosts
                    )
                    if host not in config.exclude_hosts
                )
                self._state.reconfigure(config, self._discovered_hosts)
                with self._probe_control_lock:
                    self._runtime_hosts.intersection_update(self._discovered_hosts)
                    self._manual_probe_requests.intersection_update(
                        self._discovered_hosts
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
        with self._config_lock:
            if generation != self._config_generation:
                return
            hosts = tuple(host for host in hosts if host not in config.exclude_hosts)
            self._discovered_hosts = hosts
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
                    result = self._state.unexpected_probe_result(host)
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
        """Run independently paced probes without waiting for workers at shutdown."""
        run_scheduler(self, stop_event)

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
