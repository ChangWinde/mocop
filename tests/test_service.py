from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from mocop.config import (
    ConnectionTopologyConfig,
    HostOverrideConfig,
    IncidentActionConfig,
    MaintenanceWindowConfig,
    MonitorConfig,
    TopologyLinkConfig,
)
from mocop.models import (
    DiskMetrics,
    GpuMetrics,
    GpuProcess,
    ProbeResult,
    SystemMetrics,
    WorkloadMetadata,
)
from mocop.service import MonitorService, StateStore


class StateStoreTests(unittest.TestCase):
    def test_disabled_persistence_skips_every_write_path(self) -> None:
        persistence = Mock()
        persistence.is_enabled.return_value = False
        gpu = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=50,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
        )
        system = SystemMetrics(
            hostname="gpu-1",
            uptime_seconds=100,
            load_1m=1,
            load_5m=1,
            load_15m=1,
            cpu_cores=8,
            cpu_usage_pct=20,
            memory_total_mib=16000,
            memory_used_mib=4000,
            memory_available_mib=12000,
            swap_total_mib=1000,
            swap_used_mib=0,
            disk_total_mib=100000,
            disk_used_mib=20000,
            network_rx_bps=100,
            network_tx_bps=200,
        )
        store = StateStore(5, persistence=persistence)
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (gpu,),
                system=system,
                observed_at="2026-08-10T00:00:00Z",
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1",
                "unreachable",
                5000,
                observed_at="2026-08-10T00:00:05Z",
            )
        )

        persistence.record_history.assert_not_called()
        persistence.record_gpu_telemetry.assert_not_called()
        persistence.record_incidents.assert_not_called()

    def test_condition_action_changes_actionable_counts_and_diagnosis(self) -> None:
        def clock() -> datetime:
            return datetime(2026, 8, 10, tzinfo=timezone.utc)

        action = IncidentActionConfig(
            host="offline",
            condition_key="connectivity",
            action="acknowledged",
            until=datetime(2026, 8, 11, tzinfo=timezone.utc),
            reason="owner notified",
        )
        store = StateStore(5, incident_actions=(action,), utc_clock=clock)
        store.set_hosts(("offline",))
        store.apply(
            ProbeResult(
                "offline",
                "unreachable",
                5000,
                message="SSH connection timed out",
                observed_at="2026-08-10T00:00:00Z",
            )
        )

        snapshot = store.snapshot()
        condition = store.incidents(10)["active"][0]

        self.assertEqual(snapshot["stats"]["activeIncidents"], 1)
        self.assertEqual(snapshot["stats"]["actionableIncidents"], 0)
        self.assertTrue(condition["acknowledged"])
        self.assertFalse(condition["actionable"])
        self.assertEqual(condition["actionReason"], "owner notified")
        self.assertEqual(condition["diagnosis"]["title"], "Collection path unavailable")

    def test_tracks_per_gpu_history_and_only_real_process_transitions(self) -> None:
        first = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=50,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
            processes=(GpuProcess(10, "train.py", 250),),
        )
        second = replace(
            first,
            utilization_gpu_pct=70,
            processes=(GpuProcess(11, "eval.py", 200),),
        )
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (first,), observed_at="2026-08-10T00:00:00Z"
            )
        )
        self.assertEqual(store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"], [])

        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (second,), observed_at="2026-08-10T00:00:05Z"
            )
        )
        history = store.gpu_history("gpu-1", "GPU-1", 10)

        self.assertEqual(len(history["points"]), 2)
        self.assertEqual(history["points"][-1]["utilizationGpuPct"], 70)
        self.assertEqual(
            {(event["event"], event["pid"]) for event in history["processEvents"]},
            {("started", 11), ("stopped", 10)},
        )

    def test_empty_gpu_process_samples_preserve_transition_semantics(self) -> None:
        idle = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=0,
            utilization_memory_pct=0,
            memory_total_mib=1000,
            memory_used_mib=0,
            memory_free_mib=1000,
            power_draw_w=40,
            power_limit_w=200,
        )
        busy = replace(idle, processes=(GpuProcess(10, "train.py", 250),))
        store = StateStore(5)
        store.set_hosts(("gpu-1",))

        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (idle,), observed_at="2026-08-10T00:00:00Z"
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (busy,), observed_at="2026-08-10T00:00:05Z"
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (idle,), observed_at="2026-08-10T00:00:10Z"
            )
        )

        events = store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"]
        self.assertEqual(
            [(event["event"], event["pid"]) for event in events],
            [("started", 10), ("stopped", 10)],
        )

    def test_history_preserves_missing_optional_metrics(self) -> None:
        gpu = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate=None,
            temperature_c=None,
            utilization_gpu_pct=None,
            utilization_memory_pct=None,
            memory_total_mib=None,
            memory_used_mib=None,
            memory_free_mib=None,
            power_draw_w=None,
            power_limit_w=None,
        )
        system = SystemMetrics(
            hostname="gpu-1",
            uptime_seconds=100,
            load_1m=1,
            load_5m=1,
            load_15m=1,
            cpu_cores=8,
            cpu_usage_pct=None,
            memory_total_mib=16000,
            memory_used_mib=4000,
            memory_available_mib=12000,
            swap_total_mib=0,
            swap_used_mib=0,
            disk_total_mib=100000,
            disk_used_mib=20000,
            network_rx_bps=None,
            network_tx_bps=None,
            disk_read_bps=None,
            disk_write_bps=None,
        )
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (gpu,),
                observed_at="2026-08-10T00:00:00Z",
                system=system,
            )
        )

        host_point = store.history("gpu-1", 1)["points"][0]
        gpu_point = store.gpu_history("gpu-1", "GPU-1", 1)["points"][0]
        self.assertIsNone(host_point["cpuUsagePct"])
        self.assertIsNone(host_point["networkRxBps"])
        self.assertIsNone(host_point["diskWriteBps"])
        self.assertIsNone(host_point["gpuUsagePct"])
        self.assertIsNone(host_point["gpuTemperatureC"])
        self.assertEqual(gpu_point["gpuId"], "GPU-1")
        self.assertIsNone(gpu_point["utilizationGpuPct"])
        self.assertIsNone(gpu_point["memoryTotalMiB"])
        self.assertIsNone(gpu_point["powerDrawW"])

    def test_process_transitions_require_consecutive_available_samples(self) -> None:
        gpu = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=50,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
            processes=(GpuProcess(10, "before.py", 250),),
        )
        after_gap = replace(gpu, processes=(GpuProcess(11, "after.py", 200),))
        consecutive = replace(gpu, processes=(GpuProcess(12, "next.py", 180),))
        store = StateStore(5)
        store.set_hosts(("gpu-1",))

        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (gpu,), observed_at="2026-08-10T00:00:00Z"
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1", "unreachable", 5000, observed_at="2026-08-10T00:00:05Z"
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (after_gap,),
                observed_at="2026-08-10T00:00:10Z",
            )
        )
        self.assertEqual(store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"], [])

        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (consecutive,),
                observed_at="2026-08-10T00:00:15Z",
            )
        )
        events = store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"]
        self.assertEqual(
            {(event["event"], event["pid"]) for event in events},
            {("started", 12), ("stopped", 11)},
        )

        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (replace(consecutive, processes=(), processes_available=False),),
                observed_at="2026-08-10T00:00:20Z",
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (replace(gpu, processes=(GpuProcess(13, "returned.py", 160),)),),
                observed_at="2026-08-10T00:00:25Z",
            )
        )
        self.assertEqual(
            store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"], events
        )
        store.apply(
            ProbeResult("gpu-1", "online", 1, (), observed_at="2026-08-10T00:00:30Z")
        )
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (replace(gpu, processes=(GpuProcess(14, "reappeared.py", 140),)),),
                observed_at="2026-08-10T00:00:35Z",
            )
        )
        self.assertEqual(
            store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"], events
        )

    def test_skipped_process_sample_preserves_transition_baseline(self) -> None:
        initial = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=50,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
            processes=(GpuProcess(10, "initial.py", 250),),
            processes_observed_at="2026-08-10T00:00:00Z",
        )
        skipped = replace(
            initial,
            processes=(),
            processes_sampled=False,
        )
        changed = replace(
            initial,
            processes=(GpuProcess(11, "changed.py", 200),),
            processes_observed_at="2026-08-10T00:00:10Z",
        )
        store = StateStore(5)
        store.set_hosts(("gpu-1",))

        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (initial,), observed_at="2026-08-10T00:00:00Z"
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (skipped,), observed_at="2026-08-10T00:00:05Z"
            )
        )

        self.assertEqual(
            store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"],
            [],
        )

        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (changed,), observed_at="2026-08-10T00:00:10Z"
            )
        )
        events = store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"]
        self.assertEqual(
            [(event["event"], event["pid"]) for event in events],
            [("started", 11), ("stopped", 10)],
        )

    def test_aggregates_server_gpu_and_memory_stats(self) -> None:
        gpu = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=50,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
        )
        system = SystemMetrics(
            hostname="node-a",
            uptime_seconds=1000,
            load_1m=1,
            load_5m=0.5,
            load_15m=0.25,
            cpu_cores=8,
            cpu_usage_pct=25,
            memory_total_mib=16000,
            memory_used_mib=8000,
            memory_available_mib=8000,
            swap_total_mib=2000,
            swap_used_mib=500,
            disk_total_mib=100000,
            disk_used_mib=40000,
            network_rx_bps=1000,
            network_tx_bps=2000,
            disk_read_bps=3000,
            disk_write_bps=4000,
            disks=(DiskMetrics("/dev/a", "ext4", "/", 100000, 40000, 60000, 40),),
        )
        store = StateStore(5)
        store.set_hosts(("gpu-1", "offline"))
        store.apply(ProbeResult("gpu-1", "online", 12, (gpu,), system=system))
        store.apply(ProbeResult("offline", "unreachable", 5000))

        snapshot = store.snapshot()

        self.assertEqual(snapshot["appVersion"], "0.8.0")
        self.assertEqual(snapshot["stats"]["servers"], 2)
        self.assertEqual(snapshot["stats"]["onlineServers"], 1)
        self.assertEqual(snapshot["stats"]["gpus"], 1)
        self.assertEqual(snapshot["stats"]["busyGpus"], 1)
        self.assertEqual(snapshot["stats"]["memoryUsedMiB"], 250)
        self.assertEqual(snapshot["stats"]["cpuAveragePct"], 25)
        self.assertEqual(snapshot["stats"]["systemMemoryUsedMiB"], 8000)
        self.assertEqual(snapshot["stats"]["swapUsedMiB"], 500)
        self.assertEqual(snapshot["stats"]["diskUsedMiB"], 40000)
        self.assertEqual(snapshot["stats"]["networkTxBps"], 2000)
        self.assertEqual(snapshot["stats"]["diskWriteBps"], 4000)

    def test_snapshot_is_deeply_isolated_from_store_state(self) -> None:
        workload = WorkloadMetadata(kind="slurm", workload_id="42", name="train")
        process = GpuProcess(1234, "python", 512, workload)
        gpu = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=50,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
            processes=(process,),
        )
        system = SystemMetrics(
            hostname="node-a",
            uptime_seconds=1000,
            load_1m=1,
            load_5m=0.5,
            load_15m=0.25,
            cpu_cores=8,
            cpu_usage_pct=25,
            memory_total_mib=16000,
            memory_used_mib=8000,
            memory_available_mib=8000,
            swap_total_mib=2000,
            swap_used_mib=500,
            disk_total_mib=100000,
            disk_used_mib=40000,
            network_rx_bps=1000,
            network_tx_bps=2000,
            disks=(DiskMetrics("/dev/a", "ext4", "/data", 100, 40, 60, 40),),
        )
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(ProbeResult("gpu-1", "online", 12, (gpu,), system=system))

        first = store.snapshot()
        first_server = first["servers"][0]
        first_server["gpus"][0]["processes"][0]["workload"]["name"] = "changed"
        first_server["system"]["disks"][0]["mountpoint"] = "/changed"
        first_server["incidents"]["active"] = 99
        first["stats"]["gpus"] = 99

        second = store.snapshot()
        second_server = second["servers"][0]
        self.assertEqual(
            second_server["gpus"][0]["processes"][0]["workload"]["name"],
            "train",
        )
        self.assertEqual(second_server["system"]["disks"][0]["mountpoint"], "/data")
        self.assertEqual(second_server["incidents"]["active"], 0)
        self.assertEqual(second["stats"]["gpus"], 1)

    def test_preserves_last_success_but_excludes_stale_data_from_totals(self) -> None:
        system = SystemMetrics(
            hostname="node-a",
            uptime_seconds=100,
            load_1m=1,
            load_5m=1,
            load_15m=1,
            cpu_cores=4,
            cpu_usage_pct=20,
            memory_total_mib=100,
            memory_used_mib=50,
            memory_available_mib=50,
            swap_total_mib=0,
            swap_used_mib=0,
            disk_total_mib=1000,
            disk_used_mib=100,
            network_rx_bps=10,
            network_tx_bps=20,
        )
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(ProbeResult("gpu-1", "online", 10, system=system))
        self.assertTrue(store.health()["ready"])
        store.apply(
            ProbeResult(
                "gpu-1", "unreachable", 5000, message="SSH connection timed out"
            )
        )

        snapshot = store.snapshot()
        server = snapshot["servers"][0]
        self.assertTrue(server["stale"])
        self.assertEqual(server["consecutiveFailures"], 1)
        self.assertEqual(server["system"]["hostname"], "node-a")
        self.assertEqual(snapshot["stats"]["onlineServers"], 0)
        self.assertEqual(snapshot["stats"]["cpuCores"], 0)
        self.assertEqual(snapshot["stats"]["staleServers"], 1)

    def test_exposes_authoritative_retry_time_and_clears_it_after_recovery(
        self,
    ) -> None:
        store = StateStore(5)
        store.set_hosts(("gpu-1",))

        with patch(
            "mocop.service.utc_after",
            return_value="2026-08-09T12:00:10Z",
        ) as utc_after:
            store.apply(
                ProbeResult("gpu-1", "unreachable", 5000),
                retry_after_seconds=10,
            )

        self.assertEqual(
            store.snapshot()["servers"][0]["nextRetryAt"],
            "2026-08-09T12:00:10Z",
        )
        utc_after.assert_called_once_with(10)

        store.apply(ProbeResult("gpu-1", "online", 12))
        self.assertIsNone(store.snapshot()["servers"][0]["nextRetryAt"])

    def test_history_is_bounded_and_only_records_successes(self) -> None:
        system = SystemMetrics(
            hostname="node-a",
            uptime_seconds=100,
            load_1m=1,
            load_5m=1,
            load_15m=1,
            cpu_cores=4,
            cpu_usage_pct=20,
            memory_total_mib=100,
            memory_used_mib=50,
            memory_available_mib=50,
            swap_total_mib=0,
            swap_used_mib=0,
            disk_total_mib=1000,
            disk_used_mib=100,
            network_rx_bps=10,
            network_tx_bps=20,
        )
        store = StateStore(5, history_points=2)
        store.set_hosts(("gpu-1",))
        for latency in (1, 2, 3):
            store.apply(ProbeResult("gpu-1", "online", latency, system=system))
        store.apply(ProbeResult("gpu-1", "unreachable", 4))

        history = store.history("gpu-1", 10)
        self.assertIsNotNone(history)
        self.assertEqual(len(history["points"]), 2)
        self.assertEqual(history["points"][-1]["memoryUsagePct"], 50)
        self.assertIsNone(store.history("unknown", 10))

    def test_wait_only_returns_for_newer_version(self) -> None:
        store = StateStore(5)
        version = store.snapshot()["version"]
        self.assertIsNone(store.wait_for_update(version, 0.001))
        store.set_hosts(("new-host",))
        self.assertIsNotNone(store.wait_for_update(version, 0.001))

    def test_sse_readers_reuse_one_snapshot_without_exposing_mutable_state(
        self,
    ) -> None:
        store = StateStore(5)
        store.set_hosts(("gpu-1",))

        first = store.wait_for_update(-1, 0.001)
        repeated = store.wait_for_update(-1, 0.001)
        self.assertIsNotNone(first)
        self.assertIs(first, repeated)

        public = store.snapshot()
        public["stats"]["servers"] = 99
        self.assertEqual(store.snapshot()["stats"]["servers"], 1)

        version = first["version"]
        store.apply(ProbeResult("gpu-1", "unreachable", 1))
        changed = store.wait_for_update(version, 0.001)
        self.assertIsNot(first, changed)

    def test_preserves_configured_host_order_without_publishing_poll_start(
        self,
    ) -> None:
        store = StateStore(5)
        store.set_hosts(("node-b", "node-a"))
        version = store.snapshot()["version"]

        store.begin_poll(("node-b", "node-a"))
        snapshot = store.snapshot()

        self.assertEqual(
            [server["host"] for server in snapshot["servers"]], ["node-b", "node-a"]
        )
        self.assertEqual(snapshot["version"], version)
        self.assertEqual(snapshot["stats"]["pollingServers"], 2)

    def test_exposes_configured_collection_freshness_window(self) -> None:
        store = StateStore(5, collection_stale_cycles=4)

        self.assertEqual(store.snapshot()["collectionStaleAfterSeconds"], 20)

    def test_runtime_poll_interval_updates_snapshot_and_wakes_scheduler(self) -> None:
        store = StateStore(5, collection_stale_cycles=4)

        self.assertEqual(store.set_poll_interval_seconds(10), 10)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["pollIntervalSeconds"], 10)
        self.assertEqual(snapshot["collectionStaleAfterSeconds"], 40)
        self.assertTrue(store.wait_for_poll_interval_change(0))
        self.assertFalse(store.wait_for_poll_interval_change(0))

        for invalid in (0, 3601, True, "5"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                store.set_poll_interval_seconds(invalid)

    def test_exposes_incident_counts_and_last_completed_poll_timing(self) -> None:
        store = StateStore(5)
        store.set_hosts(("offline",))
        store.apply(
            ProbeResult(
                "offline",
                "unreachable",
                5000,
                message="SSH connection timed out",
            )
        )
        version_before_completion = store.snapshot()["version"]
        store.record_poll_cycle(1.234)

        snapshot = store.wait_for_update(version_before_completion, 0.001)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["stats"]["activeIncidents"], 1)
        self.assertEqual(snapshot["stats"]["criticalIncidents"], 1)
        self.assertEqual(snapshot["incidentVersion"], 1)
        self.assertEqual(snapshot["lastPollDurationMs"], 1234)
        self.assertIsNotNone(snapshot["lastPollCompletedAt"])
        self.assertEqual(store.incidents(10)["events"][0]["state"], "opened")

    def test_snapshot_carries_display_name_and_history_transport_retry(self) -> None:
        system = SystemMetrics(
            hostname="gpu-1",
            uptime_seconds=100,
            load_1m=1,
            load_5m=1,
            load_15m=1,
            cpu_cores=8,
            cpu_usage_pct=20,
            memory_total_mib=16000,
            memory_used_mib=4000,
            memory_available_mib=12000,
            swap_total_mib=1000,
            swap_used_mib=0,
            disk_total_mib=100000,
            disk_used_mib=20000,
            network_rx_bps=100,
            network_tx_bps=200,
        )
        store = StateStore(5, host_display_names=(("gpu-1", "训练 A100 节点"),))
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                42,
                system=system,
                transport_retries=1,
            )
        )

        server = store.snapshot()["servers"][0]
        self.assertEqual(server["displayName"], "训练 A100 节点")
        self.assertTrue(server["transportRetried"])
        history = store.history("gpu-1", 10)
        self.assertTrue(history["points"][-1]["transportRetried"])
        self.assertEqual(store.health()["transportRetries"], 1)

        store.set_host_display_names(())
        self.assertIsNone(store.snapshot()["servers"][0]["displayName"])

    def test_recurring_maintenance_window_activates_in_snapshot(self) -> None:
        # 2030-06-19 is a Wednesday (weekday 2); the window runs 18:00-20:00.
        window = MaintenanceWindowConfig(
            reason="Weekly patching",
            weekday=2,
            start_minutes=18 * 60,
            duration_minutes=120,
        )
        current = [datetime(2030, 6, 19, 19, 0, tzinfo=timezone.utc)]
        store = StateStore(
            5,
            maintenance_windows=(("gpu-1", window),),
            utc_clock=lambda: current[0],
        )
        store.set_hosts(("gpu-1",))

        active = store.snapshot()["servers"][0]["maintenance"]
        self.assertIsNotNone(active)
        self.assertEqual(active["until"], "2030-06-19T20:00:00Z")
        self.assertTrue(active["recurring"])

        current[0] = datetime(2030, 6, 19, 20, 1, tzinfo=timezone.utc)
        self.assertIsNone(store.snapshot()["servers"][0]["maintenance"])

        current[0] = datetime(2030, 6, 26, 18, 30, tzinfo=timezone.utc)
        self.assertIsNotNone(store.snapshot()["servers"][0]["maintenance"])

    def test_maintenance_silences_actionable_incidents_without_hiding_truth(
        self,
    ) -> None:
        window = MaintenanceWindowConfig(
            until=datetime.now(timezone.utc) + timedelta(hours=4),
            reason="Driver upgrade",
        )
        notifications = _RecordingNotifications()
        store = StateStore(
            5,
            maintenance_windows=(("offline", window),),
            notifications=notifications,
        )
        store.set_hosts(("offline",))
        store.apply(
            ProbeResult(
                "offline",
                "unreachable",
                5000,
                message="SSH connection timed out",
            )
        )

        snapshot = store.snapshot()
        incidents = store.incidents(10)

        self.assertEqual(snapshot["stats"]["activeIncidents"], 1)
        self.assertEqual(snapshot["stats"]["actionableIncidents"], 0)
        self.assertEqual(snapshot["stats"]["issueServers"], 1)
        self.assertEqual(snapshot["stats"]["actionableIssueServers"], 0)
        self.assertEqual(snapshot["stats"]["maintenanceServers"], 1)
        self.assertEqual(
            snapshot["servers"][0]["maintenance"]["reason"], "Driver upgrade"
        )
        self.assertEqual(
            snapshot["servers"][0]["incidents"],
            {
                "active": 1,
                "critical": 1,
                "actionable": 0,
                "actionableCritical": 0,
            },
        )
        self.assertTrue(incidents["active"][0]["silenced"])
        self.assertEqual(incidents["active"][0]["maintenanceReason"], "Driver upgrade")
        self.assertNotIn("silenced", incidents["events"][0])
        self.assertEqual(notifications.published, [])

        previous_revision = snapshot["incidentVersion"]
        store.set_maintenance_windows(())
        unsilenced = store.snapshot()

        self.assertEqual(unsilenced["stats"]["actionableIncidents"], 1)
        self.assertEqual(unsilenced["servers"][0]["incidents"]["actionable"], 1)
        self.assertGreater(unsilenced["incidentVersion"], previous_revision)
        self.assertFalse(store.incidents(10)["active"][0]["silenced"])

    def test_maintenance_expiry_restores_actionable_incidents_automatically(
        self,
    ) -> None:
        current = [datetime(2030, 6, 15, 12, 0, tzinfo=timezone.utc)]
        window = MaintenanceWindowConfig(
            until=current[0] + timedelta(hours=1),
            reason="Kernel upgrade",
        )
        store = StateStore(
            5,
            maintenance_windows=(("offline", window),),
            utc_clock=lambda: current[0],
        )
        store.set_hosts(("offline",))
        store.apply(ProbeResult("offline", "unreachable", 5000))
        silenced = store.snapshot()

        current[0] = window.until
        expired = store.snapshot()

        self.assertEqual(silenced["stats"]["actionableIncidents"], 0)
        self.assertEqual(expired["stats"]["actionableIncidents"], 1)
        self.assertEqual(expired["stats"]["maintenanceServers"], 0)
        self.assertIsNone(expired["servers"][0]["maintenance"])
        self.assertGreater(expired["incidentVersion"], silenced["incidentVersion"])
        self.assertFalse(store.incidents(10)["active"][0]["silenced"])

    def test_publishes_and_hot_replaces_shared_host_groups(self) -> None:
        store = StateStore(5, host_groups=(("gpu-1", "Training"),))
        store.set_hosts(("gpu-1", "gpu-2"))

        initial = store.snapshot()
        groups = {server["host"]: server["group"] for server in initial["servers"]}
        self.assertEqual(groups, {"gpu-1": "Training", "gpu-2": None})

        version = initial["version"]
        store.set_host_groups((("gpu-2", "Inference"),))
        updated = store.snapshot()
        groups = {server["host"]: server["group"] for server in updated["servers"]}
        self.assertGreater(updated["version"], version)
        self.assertEqual(groups, {"gpu-1": None, "gpu-2": "Inference"})

        unchanged_version = updated["version"]
        store.set_host_groups((("gpu-2", "Inference"),))
        self.assertEqual(store.snapshot()["version"], unchanged_version)

    def test_wires_expected_gpu_inventory_into_authoritative_incidents(self) -> None:
        store = StateStore(5, expected_gpu_counts=(("gpu-1", 2),))
        store.set_hosts(("gpu-1",))

        store.apply(ProbeResult("gpu-1", "online", 10))

        incidents = store.incidents(10)
        self.assertEqual(incidents["active"][0]["category"], "gpu_count")
        self.assertEqual(incidents["active"][0]["value"], 0)
        self.assertEqual(incidents["active"][0]["threshold"], 2)
        self.assertEqual(store.snapshot()["stats"]["incidentServers"], 1)
        self.assertEqual(store.snapshot()["stats"]["issueServers"], 1)

    def test_expected_gpu_inventory_can_be_replaced_after_host_removal(self) -> None:
        store = StateStore(5, expected_gpu_counts=(("gpu-1", 2),))
        store.set_hosts(("gpu-1",))
        store.apply(ProbeResult("gpu-1", "online", 10))
        self.assertEqual(store.incidents(10)["active"][0]["category"], "gpu_count")

        store.set_hosts(())
        store.update_expected_gpu_counts(())
        store.set_hosts(("gpu-1",))
        store.apply(ProbeResult("gpu-1", "online", 10))

        self.assertEqual(store.incidents(10)["active"], [])

    def test_incident_snapshot_adds_non_authoritative_topology_correlations(
        self,
    ) -> None:
        topology = ConnectionTopologyConfig(
            root="monitor",
            links=(
                TopologyLinkConfig("monitor", "gateway", "ssh"),
                TopologyLinkConfig("gateway", "gpu-1", "ssh"),
                TopologyLinkConfig("gateway", "gpu-2", "ssh"),
            ),
        )
        store = StateStore(5, topology=topology)
        store.set_hosts(("gpu-1", "gpu-2"))
        store.apply(ProbeResult("gpu-1", "unreachable", 1))
        store.apply(ProbeResult("gpu-2", "unreachable", 1))

        incidents = store.incidents(10)

        self.assertEqual(len(incidents["active"]), 2)
        self.assertEqual(incidents["correlations"][0]["anchor"], "gateway")
        self.assertEqual(incidents["correlations"][0]["hosts"], ["gpu-1", "gpu-2"])

    def test_notification_sink_receives_actionable_transitions_with_context(
        self,
    ) -> None:
        topology = ConnectionTopologyConfig(
            root="monitor",
            links=(
                TopologyLinkConfig("monitor", "gateway", "ssh"),
                TopologyLinkConfig("gateway", "gpu-1", "ssh"),
                TopologyLinkConfig("gateway", "gpu-2", "ssh"),
            ),
        )
        notifications = _RecordingNotifications()
        store = StateStore(5, topology=topology, notifications=notifications)
        store.set_hosts(("gpu-1", "gpu-2"))

        store.apply(ProbeResult("gpu-1", "unreachable", 1))
        store.apply(ProbeResult("gpu-2", "unreachable", 1))

        self.assertEqual(len(notifications.published), 2)
        self.assertEqual(notifications.published[-1][0][0].host, "gpu-2")
        self.assertEqual(notifications.published[-1][1][0]["anchor"], "gateway")


class _HostSource:
    def hosts(self, _config):
        return ("offline",)


class _FailingProbe:
    def __init__(self) -> None:
        self.calls = 0

    def probe(self, host, _config):
        self.calls += 1
        return ProbeResult(host, "unreachable", 5000)


class _OnlineProbe:
    def __init__(self) -> None:
        self.calls = 0

    def probe(self, host, _config):
        self.calls += 1
        return ProbeResult(host, "online", 17_000)


class _ConfigHostSource:
    def hosts(self, config):
        return config.hosts


class _FailingHostSource:
    def __init__(self) -> None:
        self.calls = 0

    def hosts(self, _config):
        self.calls += 1
        raise OSError("test discovery failure")


class _ControlledWakeEvent:
    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event
        self.waits = 0
        self.timeouts = []

    def clear(self):
        return None

    def set(self):
        return None

    def wait(self, timeout):
        self.waits += 1
        self.timeouts.append(timeout)
        if self.waits == 2:
            self._stop_event.set()
        return False


class _RecordingProbe:
    def __init__(self) -> None:
        self.calls = []

    def probe(self, host, config):
        self.calls.append(
            (
                host,
                config.hosts,
                config.probe_timeout_seconds,
                config.max_workers,
            )
        )
        return ProbeResult(host, "online", 1)


class _InventoryRecordingProbe(_RecordingProbe):
    def __init__(self) -> None:
        super().__init__()
        self.retained_hosts = []

    def retain_hosts(self, hosts):
        self.retained_hosts.append(frozenset(hosts))


class _RecordingNotifications:
    def __init__(self) -> None:
        self.published = []

    def publish(self, events, correlations):
        self.published.append((events, correlations))

    def status(self):
        return {
            "enabled": True,
            "healthy": True,
            "queuedDeliveries": 0,
            "droppedDeliveries": 0,
            "endpoints": [],
        }

    def close(self, timeout_seconds=5):
        del timeout_seconds

    def test(self):
        return True


class _ManualProbe:
    def __init__(self) -> None:
        self.first_completed = threading.Event()
        self.second_started = threading.Event()
        self.release_second = threading.Event()
        self.calls = 0

    def probe(self, host, _config):
        self.calls += 1
        if self.calls == 1:
            self.first_completed.set()
        if self.calls == 2:
            self.second_started.set()
            self.release_second.wait(2)
        return ProbeResult(host, "online", 1)


class _ReentrantManualProbe:
    def __init__(self) -> None:
        self.service = None
        self.checked = threading.Event()
        self.request_result = None
        self.calls = 0

    def probe(self, host, _config):
        self.calls += 1
        self.request_result = self.service.request_probe(host)
        self.checked.set()
        return ProbeResult(host, "online", 1)


class _BlockingProbe:
    def __init__(self) -> None:
        self.slow_started = threading.Event()
        self.release_slow = threading.Event()
        self.fast_completed_three = threading.Event()
        self._lock = threading.Lock()
        self.calls: dict[str, int] = {"slow": 0, "fast": 0}

    def probe(self, host, _config):
        with self._lock:
            self.calls[host] += 1
            call_count = self.calls[host]
        if host == "slow":
            self.slow_started.set()
            if not self.release_slow.wait(2):
                raise TimeoutError("test did not release the slow probe")
        elif call_count >= 3:
            self.fast_completed_three.set()
        return ProbeResult(host, "online", 1)


class MonitorServiceTests(unittest.TestCase):
    def test_in_flight_state_is_visible_before_probe_worker_starts(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=60,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        probe = _ReentrantManualProbe()
        service = MonitorService(config, _ConfigHostSource(), probe, StateStore(60))
        probe.service = service
        stop_event = threading.Event()
        scheduler = threading.Thread(target=service.run, args=(stop_event,))
        scheduler.start()
        try:
            self.assertTrue(probe.checked.wait(1))
        finally:
            stop_event.set()
            service.stop()
            scheduler.join(2)

        self.assertEqual(probe.calls, 1)
        self.assertEqual(probe.request_result["status"], "in_progress")
        self.assertFalse(probe.request_result["accepted"])

    def test_poll_once_prunes_inventory_aware_probe_state(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=60,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        probe = _InventoryRecordingProbe()
        service = MonitorService(config, _ConfigHostSource(), probe, StateStore(60))

        service.poll_once()

        self.assertEqual(probe.retained_hosts, [frozenset({"gpu-01"})])

    @patch("mocop.service.time.monotonic", side_effect=(10.0, 11.0))
    def test_removed_host_does_not_keep_manual_probe_cooldown(self, _monotonic) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=60,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
            manual_probe_cooldown_seconds=60,
        )
        service = MonitorService(
            config, _ConfigHostSource(), _OnlineProbe(), StateStore(60)
        )
        service._runtime_hosts.add("gpu-01")
        self.assertTrue(service.request_probe("gpu-01")["accepted"])

        service._manual_probe_requests.clear()
        service._runtime_hosts.clear()
        service._prune_schedules(set())
        service._runtime_hosts.add("gpu-01")

        self.assertTrue(service.request_probe("gpu-01")["accepted"])

    def test_manual_probe_is_coalesced_non_overlapping_and_rate_limited(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=60,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
            manual_probe_cooldown_seconds=1,
        )
        probe = _ManualProbe()
        service = MonitorService(config, _ConfigHostSource(), probe, StateStore(60))
        stop_event = threading.Event()
        scheduler = threading.Thread(target=service.run, args=(stop_event,))
        scheduler.start()
        try:
            self.assertTrue(probe.first_completed.wait(1))
            deadline = time.monotonic() + 1
            while True:
                result = service.request_probe("gpu-01")
                if result["accepted"]:
                    break
                self.assertEqual(result["status"], "in_progress")
                if time.monotonic() >= deadline:
                    self.fail("completed probe remained in progress")
                threading.Event().wait(0.01)
            self.assertIn(
                service.request_probe("gpu-01")["status"], {"queued", "in_progress"}
            )
            if not probe.second_started.wait(1):
                self.fail(
                    "queued manual probe did not start; "
                    f"calls={probe.calls}, alive={scheduler.is_alive()}, "
                    f"in_flight={service._runtime_in_flight}, "
                    f"queued={service._manual_probe_requests}, "
                    f"next={service._next_probe_at}"
                )
            self.assertEqual(service.request_probe("gpu-01")["status"], "in_progress")
        finally:
            probe.release_second.set()
            stop_event.set()
            service.stop()
            scheduler.join(2)
        self.assertEqual(probe.calls, 2)

    def test_scheduler_paces_repeated_inventory_discovery_failures(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        source = _FailingHostSource()
        service = MonitorService(config, source, _OnlineProbe(), StateStore(5))
        stop_event = threading.Event()
        wake_event = _ControlledWakeEvent(stop_event)
        service._scheduler_wakeup = wake_event

        with patch("mocop.service.print"):
            service.run(stop_event)

        self.assertEqual(source.calls, 1)
        self.assertEqual(wake_event.waits, 2)
        self.assertGreater(wake_event.timeouts[0], 4.5)

    def test_run_schedules_each_host_without_waiting_for_a_slow_peer(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("slow", "fast"),
            exclude_hosts=frozenset(),
            poll_interval_seconds=0.02,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=2,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        probe = _BlockingProbe()
        service = MonitorService(
            config,
            _ConfigHostSource(),
            probe,
            StateStore(config.poll_interval_seconds),
        )
        stop_event = threading.Event()
        scheduler = threading.Thread(target=service.run, args=(stop_event,))
        scheduler.start()
        try:
            self.assertTrue(probe.slow_started.wait(1), "slow probe never started")
            self.assertTrue(
                probe.fast_completed_three.wait(1),
                "fast host waited for the blocked host's collection cycle",
            )
            self.assertEqual(probe.calls["slow"], 1)
        finally:
            stop_event.set()
            probe.release_slow.set()
            scheduler.join(2)
        self.assertFalse(scheduler.is_alive(), "scheduler did not stop cleanly")

    def test_failure_backoff_jitter_is_stable_bounded_and_host_specific(self) -> None:
        first = MonitorService._backoff_delay(5, 3, "gpu-01", 15)
        repeated = MonitorService._backoff_delay(5, 3, "gpu-01", 15)
        second = MonitorService._backoff_delay(5, 3, "gpu-02", 15)

        self.assertEqual(first, repeated)
        self.assertGreaterEqual(first, 17)
        self.assertLessEqual(first, 20)
        self.assertNotEqual(first, second)

    def test_topology_only_nodes_never_enter_the_probe_inventory(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset({"gpu-gateway"}),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=2,
            listen_host="127.0.0.1",
            listen_port=8787,
            topology=ConnectionTopologyConfig(
                root="monitor-host",
                links=(
                    TopologyLinkConfig(
                        source="monitor-host",
                        target="gpu-gateway",
                        transport="frp-stcp",
                    ),
                    TopologyLinkConfig(
                        source="gpu-gateway",
                        target="gpu-01",
                        transport="ssh",
                    ),
                ),
            ),
        )
        probe = _RecordingProbe()
        service = MonitorService(config, _ConfigHostSource(), probe, StateStore(5))

        service.poll_once()

        self.assertEqual([call[0] for call in probe.calls], ["gpu-01"])

    def test_replaces_persisted_collector_policy_without_restart(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=2,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        state = StateStore(5)
        probe = _RecordingProbe()
        service = MonitorService(config, _ConfigHostSource(), probe, state)

        maintenance = MaintenanceWindowConfig(
            until=datetime.now(timezone.utc) + timedelta(hours=1),
            reason="Driver upgrade",
        )
        service.update_config(
            replace(
                config,
                poll_interval_seconds=2,
                probe_timeout_seconds=24,
                max_workers=7,
                maintenance_windows=(("gpu-01", maintenance),),
            )
        )
        service.poll_once()

        self.assertEqual(state.snapshot()["pollIntervalSeconds"], 2)
        self.assertEqual(state.snapshot()["collectionStaleAfterSeconds"], 6)
        self.assertEqual(
            state.snapshot()["servers"][0]["maintenance"]["reason"],
            "Driver upgrade",
        )
        self.assertEqual(probe.calls, [("gpu-01", ("gpu-01",), 24, 7)])

    def test_shutdown_wait_tracks_runtime_probe_timeout_updates(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=2,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        service = MonitorService(
            config,
            _ConfigHostSource(),
            _RecordingProbe(),
            StateStore(5),
        )
        service.update_config(
            replace(
                config,
                probe_timeout_seconds=24,
                host_overrides=(
                    (
                        "gpu-01",
                        HostOverrideConfig(probe_timeout_seconds=30),
                    ),
                ),
            )
        )

        self.assertEqual(service.shutdown_timeout_seconds(), 31)

    def test_replaces_inventory_config_without_restarting_the_service(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=2,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        state = StateStore(5)
        probe = _RecordingProbe()
        service = MonitorService(config, _ConfigHostSource(), probe, state)

        service.poll_once()
        service.update_config(replace(config, hosts=("gpu-02",)))
        self.assertTrue(state.wait_for_schedule_change(0))
        service.poll_once()

        self.assertEqual([item[0] for item in probe.calls], ["gpu-01", "gpu-02"])
        self.assertEqual(
            [server["host"] for server in state.snapshot()["servers"]],
            ["gpu-02"],
        )

    @patch("mocop.service.time.monotonic")
    def test_paces_a_slow_host_without_changing_the_global_cadence(
        self, monotonic
    ) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("offline",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
            host_overrides=(
                (
                    "offline",
                    HostOverrideConfig(
                        poll_interval_seconds=30,
                        probe_timeout_seconds=20,
                    ),
                ),
            ),
        )
        probe = _OnlineProbe()
        service = MonitorService(config, _HostSource(), probe, StateStore(5))
        monotonic.side_effect = [0, 5, 30]

        service.poll_once()
        service.poll_once()
        service.poll_once()

        self.assertEqual(probe.calls, 2)

    @patch("mocop.service.time.monotonic")
    def test_backs_off_repeated_failures_without_delaying_healthy_cycles(
        self, monotonic
    ) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("offline",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        probe = _FailingProbe()
        service = MonitorService(config, _HostSource(), probe, StateStore(5))
        monotonic.side_effect = [0, 0, 1, 6, 6]

        service.poll_once()
        service.poll_once()
        service.poll_once()

        self.assertEqual(probe.calls, 2)

    @patch("mocop.service.utc_after", return_value="2026-08-09T12:00:10Z")
    @patch("mocop.service.time.monotonic")
    def test_failure_backoff_uses_runtime_poll_interval(
        self, monotonic, utc_after
    ) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("offline",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
            retry_jitter_pct=0,
        )
        store = StateStore(5)
        store.set_poll_interval_seconds(10)
        service = MonitorService(config, _HostSource(), _FailingProbe(), store)
        monotonic.side_effect = [0, 0]

        service.poll_once()

        utc_after.assert_called_once_with(10)

    @patch("mocop.service.time.monotonic")
    def test_frequency_change_rebases_existing_failure_deadline(
        self, monotonic
    ) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("offline",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=10,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        probe = _FailingProbe()
        store = StateStore(10)
        service = MonitorService(config, _HostSource(), probe, store)
        monotonic.side_effect = [0, 0, 1, 2, 2]

        service.poll_once()
        store.set_poll_interval_seconds(2)
        service.poll_once()
        self.assertEqual(probe.calls, 1)
        service.poll_once()

        self.assertEqual(probe.calls, 2)


if __name__ == "__main__":
    unittest.main()
