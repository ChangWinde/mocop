from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from mocop.config import MonitorConfig
from mocop.models import DiskMetrics, GpuMetrics, ProbeResult, SystemMetrics
from mocop.service import MonitorService, StateStore


class StateStoreTests(unittest.TestCase):
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

        for invalid in (1, 61, True, "5"):
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

    def test_wires_expected_gpu_inventory_into_authoritative_incidents(self) -> None:
        store = StateStore(5, expected_gpu_counts=(("gpu-1", 2),))
        store.set_hosts(("gpu-1",))

        store.apply(ProbeResult("gpu-1", "online", 10))

        incidents = store.incidents(10)
        self.assertEqual(incidents["active"][0]["category"], "gpu_count")
        self.assertEqual(incidents["active"][0]["value"], 0)
        self.assertEqual(incidents["active"][0]["threshold"], 2)


class _HostSource:
    def hosts(self, _config):
        return ("offline",)


class _FailingProbe:
    def __init__(self) -> None:
        self.calls = 0

    def probe(self, host, _config):
        self.calls += 1
        return ProbeResult(host, "unreachable", 5000)


class MonitorServiceTests(unittest.TestCase):
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
