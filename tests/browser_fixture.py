from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from mocop import __version__
from mocop.inventory import InventoryRequestError
from mocop.models import (
    DiskMetrics,
    GpuHealthMetrics,
    GpuMetrics,
    GpuProcess,
    ProbeResult,
    SystemMetrics,
    WorkloadMetadata,
)
from mocop.service import StateStore
from mocop.web import MonitorHttpServer, MonitorRequestHandler

# The dashboard marks every read it initiates so the service keeps the
# attended probe cadence; these level-triggered paths accept unmarked reads
# (curl, scripts) but the dashboard itself must never send one.
_TRACKED_DASHBOARD_PATHS = frozenset(
    {
        "/api/snapshot",
        "/api/history",
        "/api/gpu-history",
        "/api/incidents",
        "/api/usage",
    }
)
_COLLECTOR_SETTINGS_KEYS = frozenset(
    {"pollIntervalSeconds", "probeTimeoutSeconds", "maxWorkers"}
)


class DemoInventory:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.configured = ["atlas-01", "atlas-02", "atlas-03"]
        self.available = ["atlas-04", "atlas-05"]
        self.collector_settings = {
            "pollIntervalSeconds": 5,
            "probeTimeoutSeconds": 15,
            "maxWorkers": 8,
            # Read-only on the new contract: surfaced so the dashboard can
            # explain the probe-timeout lower bound.
            "connectTimeoutSeconds": 5,
        }
        # New contract: planned recurring windows are delivered even outside
        # their live period, flagged by "active".
        self.maintenance_windows: dict[str, dict[str, object]] = {
            "atlas-02": {
                "until": "2030-06-15T02:00:00Z",
                "reason": "Weekly firmware inspection",
                "recurring": True,
                "active": False,
            }
        }
        self.host_groups = {
            "atlas-01": "Training",
            "atlas-02": "Training",
            "atlas-03": "Lab",
        }
        self.incident_actions = []

    def topology(self) -> dict[str, object]:
        return {
            "root": "monitor-console",
            "links": [
                {
                    "source": "monitor-console",
                    "target": "atlas-gateway",
                    "transport": "frp-stcp",
                    "label": "STCP · 7005",
                },
                {
                    "source": "atlas-gateway",
                    "target": "atlas-01",
                    "transport": "ssh",
                },
                {
                    "source": "atlas-gateway",
                    "target": "atlas-02",
                    "transport": "ssh",
                },
                {
                    "source": "atlas-02",
                    "target": "atlas-03",
                    "transport": "ssh",
                },
                {
                    "source": "atlas-gateway",
                    "target": "atlas-06",
                    "transport": "ssh",
                },
            ],
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "configuredHosts": list(self.configured),
            "activeHosts": list(self.configured),
            "availableHosts": list(self.available),
            "localHost": None,
            "autoDiscover": False,
            "ignoredCodeHostCount": 2,
            "excludedHostCount": 1,
            "collectorSettings": dict(self.collector_settings),
            "maintenanceWindows": dict(self.maintenance_windows),
            "hostGroups": dict(self.host_groups),
            "incidentActions": list(self.incident_actions),
            "writable": True,
        }

    def change(self, action: str, host: str) -> dict[str, object]:
        if action == "add" and host in self.available:
            self.available.remove(host)
            self.configured.append(host)
        elif action == "remove" and host in self.configured:
            self.configured.remove(host)
            self.available.append(host)
        else:
            raise InventoryRequestError("stale demo inventory")
        return self.snapshot()

    def update_collector_settings(
        self, settings: dict[str, object]
    ) -> dict[str, object]:
        self.collector_settings.update(settings)
        return dict(self.collector_settings)

    def update_maintenance(
        self, host: str, duration_seconds: int, reason: str
    ) -> dict[str, object]:
        if host not in self.configured:
            raise InventoryRequestError("stale demo inventory")
        if duration_seconds:
            self.maintenance_windows[host] = {
                "until": "2030-06-15T12:30:00Z",
                "reason": reason.strip(),
                "active": True,
            }
        else:
            self.maintenance_windows.pop(host, None)
        return self.snapshot()

    def update_host_group(self, host: str, group: str) -> dict[str, object]:
        if host not in self.configured:
            raise InventoryRequestError("stale demo inventory")
        normalized = group.strip()
        if normalized:
            self.host_groups[host] = normalized
        else:
            self.host_groups.pop(host, None)
        self.state.set_host_groups(tuple(self.host_groups.items()))
        return self.snapshot()

    def update_incident_action(
        self,
        host: str,
        condition_key: str,
        action: str,
        duration_seconds: int,
        reason: str,
    ) -> dict[str, object]:
        del duration_seconds
        self.incident_actions = [
            item
            for item in self.incident_actions
            if (item["host"], item["condition_key"]) != (host, condition_key)
        ]
        if action != "clear":
            self.incident_actions.append(
                {
                    "host": host,
                    "condition_key": condition_key,
                    "action": action,
                    "until": "2030-06-15T12:30:00Z",
                    "reason": reason,
                }
            )
        return self.snapshot()


class DemoNotificationSink:
    """Webhook status sample: one healthy endpoint plus one failing one."""

    def __init__(self, now: datetime) -> None:
        self._endpoints = (
            {
                "name": "ops-webhook",
                "healthy": True,
                "queuedDeliveries": 2,
                "deliveredEvents": 18,
                "droppedDeliveries": 0,
                "lastError": None,
                "lastAttemptAt": _iso(now - timedelta(minutes=5)),
                "lastSuccessAt": _iso(now - timedelta(minutes=5)),
            },
            {
                "name": "sms-bridge",
                "healthy": False,
                "queuedDeliveries": 0,
                "deliveredEvents": 4,
                "droppedDeliveries": 3,
                "lastError": "HTTP 503 from relay",
                "lastAttemptAt": _iso(now - timedelta(minutes=2)),
                "lastSuccessAt": None,
            },
        )

    def publish(self, events: object, correlations: object) -> None:
        del events, correlations

    def set_actionable_check(self, check: object) -> None:
        del check

    def status(self) -> dict[str, object]:
        endpoints = [dict(endpoint) for endpoint in self._endpoints]
        return {
            "enabled": True,
            "healthy": all(bool(endpoint["healthy"]) for endpoint in endpoints),
            "queuedDeliveries": sum(
                int(endpoint["queuedDeliveries"]) for endpoint in endpoints
            ),
            "droppedDeliveries": sum(
                int(endpoint["droppedDeliveries"]) for endpoint in endpoints
            ),
            "endpoints": endpoints,
        }

    def test(self) -> bool:
        return True

    def close(self, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds


class DemoRequestHandler(MonitorRequestHandler):
    """Simulates the in-progress backend contract on top of the current web
    module: /api/meta, subset bodies on /api/settings/collector, and a
    counter for dashboard-path reads that arrive without the viewer marker.
    """

    def _respond_to_read_request(self) -> None:
        path = self.path.split("?", 1)[0]
        if (
            path in _TRACKED_DASHBOARD_PATHS
            and self.headers.get("X-Monitor-Request") != "dashboard"
        ):
            self.monitor_server.unmarked_dashboard_reads += 1
        if path == "/api/meta":
            self._send_json(
                {
                    "apiVersion": 1,
                    "appVersion": __version__,
                    "schemaVersion": 1,
                    "capabilities": {
                        "restartSupported": self.monitor_server.restart is not None,
                    },
                    "endpoints": sorted(_TRACKED_DASHBOARD_PATHS)
                    + ["/api/events", "/api/inventory", "/api/topology"],
                    # Fixture-only diagnostics; extra keys are allowed by the
                    # defensive client parser.
                    "fixture": {
                        "unmarkedDashboardReads": (
                            self.monitor_server.unmarked_dashboard_reads
                        ),
                    },
                }
            )
            return
        super()._respond_to_read_request()

    def _change_collector_settings(self, payload: object) -> None:
        # New contract: a strict subset of the collector fields is accepted;
        # missing fields keep their current values.
        if (
            isinstance(payload, dict)
            and payload
            and set(payload) < _COLLECTOR_SETTINGS_KEYS
        ):
            current = self.monitor_server.inventory.collector_settings
            merged = {key: current[key] for key in _COLLECTOR_SETTINGS_KEYS}
            merged.update(payload)
            payload = merged
        super()._change_collector_settings(payload)


def gpu(
    host: str,
    index: int,
    utilization: float,
    memory_used: float,
    temperature: float,
    unknown_memory_worker: bool = False,
    train_started_at: str | None = None,
    processes_observed_at: str | None = None,
    processes_available: bool = True,
) -> GpuMetrics:
    processes = (
        (
            GpuProcess(
                pid=10_000 + index,
                name="/workspace/train.py",
                used_memory_mib=max(512, memory_used - 1024),
                workload=WorkloadMetadata(
                    kind="slurm",
                    workload_id="4821",
                    name="llm-train",
                    owner="researcher",
                    queue="gpu-long",
                    command="python train.py --config configs/llm-70b.yaml --stage sft",
                    started_at=train_started_at,
                ),
            ),
            # One host-wide PID visible on every busy GPU: the owners view must
            # count it once per host, not once per GPU record. It has no
            # workload, so its runtime comes from monitor-side first_seen_at.
            GpuProcess(
                pid=20_000,
                name="python data_worker.py",
                used_memory_mib=512,
            ),
        )
        if utilization >= 50
        else ()
    )
    if unknown_memory_worker:
        processes = processes + (
            GpuProcess(
                pid=21_000,
                name="python telemetry_probe.py",
                used_memory_mib=None,
            ),
        )
    return GpuMetrics(
        index=index,
        uuid=f"GPU-DEMO-{host}-{index:02d}",
        name="NVIDIA H100 80GB HBM3",
        driver_version="550.90.07",
        pstate="P0" if utilization >= 10 else "P8",
        temperature_c=temperature,
        utilization_gpu_pct=utilization,
        utilization_memory_pct=min(100, utilization + 8),
        memory_total_mib=81_920,
        memory_used_mib=memory_used,
        memory_free_mib=81_920 - memory_used,
        power_draw_w=round(75 + utilization * 4.5, 1),
        power_limit_w=700,
        processes=processes,
        processes_available=processes_available,
        processes_observed_at=processes_observed_at,
        health=GpuHealthMetrics(
            ecc_uncorrected_volatile=0,
            retired_pages_pending=False,
            remapped_rows_pending=False,
            thermal_slowdown=False,
            power_brake_slowdown=False,
            mig_mode="Disabled",
        ),
    )


def system(hostname: str, cpu: float, memory_used: float) -> SystemMetrics:
    disk = DiskMetrics(
        device="/dev/nvme0n1p2",
        filesystem_type="ext4",
        mountpoint="/",
        total_mib=1_907_348,
        used_mib=812_442,
        available_mib=1_094_906,
        used_pct=42.6,
    )
    return SystemMetrics(
        hostname=hostname,
        uptime_seconds=1_428_320,
        load_1m=8.4,
        load_5m=7.8,
        load_15m=7.2,
        cpu_cores=128,
        cpu_usage_pct=cpu,
        memory_total_mib=1_048_576,
        memory_used_mib=memory_used,
        memory_available_mib=1_048_576 - memory_used,
        swap_total_mib=16_384,
        swap_used_mib=512,
        disk_total_mib=disk.total_mib,
        disk_used_mib=disk.used_mib,
        network_rx_bps=186_646_528,
        network_tx_bps=92_274_688,
        disk_read_bps=47_185_920,
        disk_write_bps=18_874_368,
        disks=(disk,),
    )


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def demo_state() -> StateStore:
    now = datetime.now(timezone.utc)
    observed_at = _iso(now)
    # Two probe rounds: the earlier one seeds monitor-side first_seen_at (the
    # data worker has no workload, so its runtime must come from tracking and
    # predate the Slurm job start below for the duration sort to reorder).
    first_observed_at = _iso(now - timedelta(hours=5))
    train_started_at = _iso(now - timedelta(hours=3))
    # Older than the 90-second freshness warning threshold, by a wide margin
    # so slow smoke runs stay deterministic.
    stale_processes_at = _iso(now - timedelta(minutes=30))
    state = StateStore(
        5,
        host_groups=(
            ("atlas-01", "Training"),
            ("atlas-02", "Training"),
            ("atlas-03", "Lab"),
            ("atlas-06", "Lab"),
        ),
        notifications=DemoNotificationSink(now),
    )
    state.set_hosts(("atlas-01", "atlas-02", "atlas-03", "atlas-06"))
    for round_observed_at in (first_observed_at, observed_at):
        state.apply(
            ProbeResult(
                host="atlas-01",
                status="online",
                latency_ms=38,
                gpus=(
                    gpu(
                        "atlas-01",
                        0,
                        96,
                        72_704,
                        74,
                        train_started_at=train_started_at,
                        processes_observed_at=round_observed_at,
                    ),
                    gpu(
                        "atlas-01",
                        1,
                        91,
                        70_912,
                        72,
                        train_started_at=train_started_at,
                        processes_observed_at=round_observed_at,
                    ),
                    gpu(
                        "atlas-01",
                        2,
                        14,
                        38_400,
                        57,
                        processes_observed_at=round_observed_at,
                    ),
                    gpu(
                        "atlas-01",
                        3,
                        2,
                        2_048,
                        35,
                        processes_observed_at=round_observed_at,
                    ),
                ),
                observed_at=round_observed_at,
                system=system("atlas-01", 68, 712_704),
            )
        )
        state.apply(
            ProbeResult(
                host="atlas-02",
                status="online",
                latency_ms=44,
                gpus=(
                    # Process telemetry deliberately lags the GPU sample so the
                    # dashboard shows the stale-freshness warning treatment.
                    gpu(
                        "atlas-02",
                        0,
                        88,
                        68_096,
                        70,
                        train_started_at=train_started_at,
                        processes_observed_at=stale_processes_at,
                    ),
                    gpu(
                        "atlas-02",
                        1,
                        76,
                        61_440,
                        67,
                        unknown_memory_worker=True,
                        train_started_at=train_started_at,
                        processes_observed_at=round_observed_at,
                    ),
                    # Process telemetry unavailable: the dashboard renders "—"
                    # instead of claiming zero tasks.
                    gpu("atlas-02", 2, 8, 12_288, 42, processes_available=False),
                    gpu(
                        "atlas-02",
                        3,
                        0,
                        1_024,
                        32,
                        processes_observed_at=round_observed_at,
                    ),
                ),
                observed_at=round_observed_at,
                system=system("atlas-02", 53, 601_088),
            )
        )
    # atlas-03 succeeded once (leaving last-success GPU processes behind) and
    # then dropped offline: its server entry is stale, and the owners view
    # must exclude those processes from "current" attribution.
    state.apply(
        ProbeResult(
            host="atlas-03",
            status="online",
            latency_ms=41,
            gpus=(
                gpu(
                    "atlas-03",
                    0,
                    97,
                    70_656,
                    73,
                    train_started_at=train_started_at,
                    processes_observed_at=first_observed_at,
                ),
            ),
            observed_at=first_observed_at,
            system=system("atlas-03", 44, 498_688),
        )
    )
    state.apply(
        ProbeResult(
            host="atlas-03",
            status="unreachable",
            latency_ms=5_000,
            message="SSH connection timed out",
            observed_at=observed_at,
        )
    )
    # atlas-06 never produced a sample: a collector-level failure whose exact
    # message must reach the dashboard through the localized failureText map.
    state.apply(
        ProbeResult(
            host="atlas-06",
            status="error",
            latency_ms=12_000,
            message="SSH transport stopped responding",
            observed_at=observed_at,
        )
    )
    state.record_poll_cycle(0.42)
    return state


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: browser_fixture.py PORT")
    state = demo_state()

    def publish_poll_completions() -> None:
        while True:
            time.sleep(1)
            state.record_poll_cycle(0.42)

    threading.Thread(
        target=publish_poll_completions,
        name="mocop-browser-fixture",
        daemon=True,
    ).start()
    server = MonitorHttpServer(
        ("127.0.0.1", int(sys.argv[1])), state, DemoInventory(state)
    )
    server.unmarked_dashboard_reads = 0
    server.RequestHandlerClass = DemoRequestHandler
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
