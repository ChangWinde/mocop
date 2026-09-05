from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from mocop import __version__
from mocop.config import (
    ConnectionTopologyConfig,
    HostOverrideConfig,
    IncidentActionConfig,
    MaintenanceWindowConfig,
    MonitorConfig,
    TopologyLinkConfig,
)
from mocop.discovery import HostDiscoverySnapshot
from mocop.models import (
    DiskMetrics,
    GpuMetrics,
    GpuProcess,
    ProbeResult,
    SystemMetrics,
    WorkloadMetadata,
)
from mocop.persistence import DisabledPersistence, LoadedTelemetry
from mocop.service import _MAX_GPU_IDENTITIES_PER_HOST, MonitorService, StateStore


class StateStoreTests(unittest.TestCase):
    def test_rejects_a_result_from_a_removed_host_incarnation(self) -> None:
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        old_incarnation = store.host_incarnation("gpu-1")
        store.set_hosts(())
        store.set_hosts(("gpu-1",))

        accepted = store.apply(
            ProbeResult("gpu-1", "online", 777, message="old incarnation"),
            expected_incarnation=old_incarnation,
        )

        self.assertFalse(accepted)
        self.assertNotEqual(store.host_incarnation("gpu-1"), old_incarnation)
        self.assertEqual(store.snapshot()["servers"][0]["status"], "pending")

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
            incident_started_at="2026-08-10T00:00:00Z",
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

    def test_legacy_actions_remain_effective_until_their_existing_expiry(self) -> None:
        def clock() -> datetime:
            return datetime(2026, 8, 10, tzinfo=timezone.utc)

        for action_name in ("acknowledged", "silenced"):
            notifications = _RecordingNotifications()
            action = IncidentActionConfig(
                host="offline",
                condition_key="connectivity",
                action=action_name,
                until=datetime(2026, 8, 11, tzinfo=timezone.utc),
                reason="legacy operator action",
            )
            store = StateStore(
                5,
                incident_actions=(action,),
                notifications=notifications,
                utc_clock=clock,
            )
            store.set_hosts(("offline",))
            store.apply(
                ProbeResult(
                    "offline",
                    "unreachable",
                    1,
                    observed_at="2026-08-10T00:00:00Z",
                )
            )

            with self.subTest(action=action_name):
                active = store.incidents(10)["active"][0]
                self.assertEqual(active[action_name], True)
                self.assertFalse(active["actionable"])
                self.assertEqual(
                    bool(notifications.published), action_name != "silenced"
                )

    def test_bound_action_survives_restart_but_not_a_resolved_recurrence(self) -> None:
        opened_at = "2026-08-10T00:00:00Z"
        action = IncidentActionConfig(
            host="offline",
            condition_key="connectivity",
            action="silenced",
            until=datetime(2026, 8, 11, tzinfo=timezone.utc),
            reason="planned investigation",
            incident_started_at=opened_at,
        )
        notifications = _RecordingNotifications()
        store = StateStore(
            5,
            incident_actions=(action,),
            notifications=notifications,
            utc_clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        store.set_hosts(("offline",))

        self.assertEqual(store.incidents(10)["active"], [])
        store.apply(
            ProbeResult(
                "offline",
                "unreachable",
                1,
                observed_at="2026-08-10T00:05:00Z",
            )
        )
        active = store.incidents(10)["active"][0]
        self.assertEqual(active["firstObservedAt"], "2026-08-10T00:05:00Z")
        self.assertTrue(active["silenced"])
        self.assertEqual(notifications.published, [])

        store.apply(
            ProbeResult("offline", "online", 1, observed_at="2026-08-10T00:06:00Z")
        )
        store.apply(
            ProbeResult("offline", "online", 1, observed_at="2026-08-10T00:07:00Z")
        )
        store.apply(
            ProbeResult(
                "offline",
                "unreachable",
                1,
                observed_at="2026-08-10T00:08:00Z",
            )
        )

        recurrent = store.incidents(10)["active"][0]
        self.assertEqual(recurrent["firstObservedAt"], "2026-08-10T00:08:00Z")
        self.assertFalse(recurrent["silenced"])
        self.assertTrue(recurrent["actionable"])

    def test_healthy_post_restart_sample_consumes_a_stale_bound_action(self) -> None:
        action = IncidentActionConfig(
            host="offline",
            condition_key="connectivity",
            action="silenced",
            until=datetime(2026, 8, 11, tzinfo=timezone.utc),
            reason="old incident",
            incident_started_at="2026-08-10T00:00:00Z",
        )
        store = StateStore(
            5,
            incident_actions=(action,),
            utc_clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        store.set_hosts(("offline",))
        store.apply(
            ProbeResult("offline", "online", 1, observed_at="2026-08-10T00:05:00Z")
        )
        store.apply(
            ProbeResult(
                "offline",
                "unreachable",
                1,
                observed_at="2026-08-10T00:06:00Z",
            )
        )

        recurrent = store.incidents(10)["active"][0]
        self.assertFalse(recurrent["silenced"])
        self.assertTrue(recurrent["actionable"])

    def test_unknown_gpu_telemetry_does_not_consume_startup_action_binding(
        self,
    ) -> None:
        def gpu(temperature: float | None) -> GpuMetrics:
            return GpuMetrics(
                index=0,
                uuid="GPU-1",
                name="Test GPU",
                driver_version="550",
                pstate="P0",
                temperature_c=temperature,
                utilization_gpu_pct=10,
                utilization_memory_pct=10,
                memory_total_mib=1000,
                memory_used_mib=100,
                memory_free_mib=900,
                power_draw_w=50,
                power_limit_w=200,
            )

        action = IncidentActionConfig(
            host="gpu-1",
            condition_key="gpu_temperature:GPU-1",
            action="silenced",
            until=datetime(2026, 8, 11, tzinfo=timezone.utc),
            reason="ongoing thermal investigation",
            incident_started_at="2026-08-10T00:00:00Z",
        )

        def store() -> StateStore:
            value = StateStore(
                5,
                incident_actions=(action,),
                utc_clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
            )
            value.set_hosts(("gpu-1",))
            return value

        continuous = store()
        continuous.apply(
            ProbeResult(
                "gpu-1",
                "unreachable",
                1,
                observed_at="2026-08-10T00:01:00Z",
            )
        )
        continuous.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (gpu(None),),
                observed_at="2026-08-10T00:02:00Z",
            )
        )
        for minute in (3, 4):
            continuous.apply(
                ProbeResult(
                    "gpu-1",
                    "online",
                    1,
                    (gpu(90),),
                    observed_at=f"2026-08-10T00:0{minute}:00Z",
                )
            )
        self.assertTrue(continuous.incidents(10)["active"][0]["silenced"])

        recovered = store()
        recovered.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (gpu(60),),
                observed_at="2026-08-10T00:01:00Z",
            )
        )
        for minute in (2, 3):
            recovered.apply(
                ProbeResult(
                    "gpu-1",
                    "online",
                    1,
                    (gpu(90),),
                    observed_at=f"2026-08-10T00:0{minute}:00Z",
                )
            )
        recurrent = recovered.incidents(10)["active"][0]
        self.assertFalse(recurrent["silenced"])
        self.assertTrue(recurrent["actionable"])

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

    def test_processes_carry_a_monitor_relative_first_seen_timestamp(self) -> None:
        base = GpuMetrics(
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
        store = StateStore(5)
        store.set_hosts(("gpu-1",))

        def snapshot_processes():
            return {
                process["pid"]: process
                for process in store.snapshot()["servers"][0]["gpus"][0]["processes"]
            }

        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (base,), observed_at="2026-08-10T00:00:00Z"
            )
        )
        both = replace(
            base,
            processes=(
                GpuProcess(10, "train.py", 260),
                GpuProcess(11, "eval.py", 200),
            ),
        )
        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (both,), observed_at="2026-08-10T00:00:05Z"
            )
        )

        by_pid = snapshot_processes()
        # The retained process keeps its original stamp with live telemetry;
        # the newcomer is stamped with the sample that first observed it.
        self.assertEqual(by_pid[10]["first_seen_at"], "2026-08-10T00:00:00Z")
        self.assertEqual(by_pid[10]["used_memory_mib"], 260)
        self.assertEqual(by_pid[11]["first_seen_at"], "2026-08-10T00:00:05Z")

        gone = replace(base, processes=(GpuProcess(11, "eval.py", 210),))
        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (gone,), observed_at="2026-08-10T00:00:10Z"
            )
        )
        returned = replace(
            base,
            processes=(
                GpuProcess(11, "eval.py", 210),
                GpuProcess(10, "train.py", 250),
            ),
        )
        store.apply(
            ProbeResult(
                "gpu-1", "online", 1, (returned,), observed_at="2026-08-10T00:00:15Z"
            )
        )

        by_pid = snapshot_processes()
        # A stopped-and-restarted pid/name pair starts a fresh observation.
        self.assertEqual(by_pid[10]["first_seen_at"], "2026-08-10T00:00:15Z")
        self.assertEqual(by_pid[11]["first_seen_at"], "2026-08-10T00:00:05Z")

    @staticmethod
    def _workload_gpu(started_at: str | None) -> GpuMetrics:
        workload = WorkloadMetadata(kind="process", started_at=started_at)
        return GpuMetrics(
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
            processes=(GpuProcess(10, "train.py", 250, workload),),
        )

    def test_pid_reuse_with_new_workload_start_is_a_fresh_instance(self) -> None:
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (self._workload_gpu("2026-08-09T23:00:00Z"),),
                observed_at="2026-08-10T00:00:00Z",
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (self._workload_gpu("2026-08-10T00:00:03Z"),),
                observed_at="2026-08-10T00:00:05Z",
            )
        )

        events = store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"]
        self.assertEqual(
            [(event["event"], event["pid"]) for event in events],
            [("stopped", 10), ("started", 10)],
        )
        self.assertEqual(
            [event["workload"]["started_at"] for event in events],
            ["2026-08-09T23:00:00Z", "2026-08-10T00:00:03Z"],
        )
        process = store.snapshot()["servers"][0]["gpus"][0]["processes"][0]
        self.assertEqual(process["first_seen_at"], "2026-08-10T00:00:05Z")

        # The same instance observed again keeps its stamp and stays silent.
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (self._workload_gpu("2026-08-10T00:00:03Z"),),
                observed_at="2026-08-10T00:00:10Z",
            )
        )
        self.assertEqual(
            store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"], events
        )
        process = store.snapshot()["servers"][0]["gpus"][0]["processes"][0]
        self.assertEqual(process["first_seen_at"], "2026-08-10T00:00:05Z")

    def test_missing_workload_start_keeps_lower_bound_identity(self) -> None:
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (self._workload_gpu(None),),
                observed_at="2026-08-10T00:00:00Z",
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (self._workload_gpu(None),),
                observed_at="2026-08-10T00:00:05Z",
            )
        )
        # One side missing a start time cannot prove PID reuse either.
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (self._workload_gpu("2026-08-10T00:00:03Z"),),
                observed_at="2026-08-10T00:00:10Z",
            )
        )

        self.assertEqual(store.gpu_history("gpu-1", "GPU-1", 10)["processEvents"], [])
        process = store.snapshot()["servers"][0]["gpus"][0]["processes"][0]
        self.assertEqual(process["first_seen_at"], "2026-08-10T00:00:00Z")

    def test_dashboard_attendance_follows_recent_reader_activity(self) -> None:
        store = StateStore(5)
        self.assertFalse(store.dashboard_attended())

        with patch(
            "mocop.service.time.monotonic",
            side_effect=(100.0, 101.0, 131.0),
        ):
            store.record_dashboard_activity()
            self.assertTrue(store.dashboard_attended())
            self.assertFalse(store.dashboard_attended())

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

    def test_unknown_gpu_memory_is_recorded_as_missing_not_zero(self) -> None:
        unknown = GpuMetrics(
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
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (unknown, replace(unknown, index=1, uuid="GPU-2")),
                system=system,
                observed_at="2026-08-10T00:00:00Z",
            )
        )

        point = store.history("gpu-1", 10)["points"][-1]
        self.assertIsNone(point["gpuMemoryUsagePct"])
        self.assertEqual(point["memoryUsagePct"], 25)

        measured = replace(
            unknown, index=1, uuid="GPU-2", memory_total_mib=1000, memory_used_mib=250
        )
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (unknown, measured),
                system=system,
                observed_at="2026-08-10T00:00:05Z",
            )
        )

        point = store.history("gpu-1", 10)["points"][-1]
        self.assertEqual(point["gpuMemoryUsagePct"], 25.0)

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

    def test_gpu_identity_churn_is_bounded_and_reclaims_stale_telemetry(self) -> None:
        base = GpuMetrics(
            index=0,
            uuid="GPU-0",
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
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (replace(base, processes=(GpuProcess(10, "train.py", 250),)),),
                observed_at="2026-08-10T00:00:00Z",
            )
        )
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (replace(base, processes=(GpuProcess(11, "eval.py", 200),)),),
                observed_at="2026-08-10T00:00:05Z",
            )
        )
        self.assertTrue(store.gpu_history("gpu-1", "GPU-0", 10)["processEvents"])

        for churn in range(_MAX_GPU_IDENTITIES_PER_HOST):
            store.apply(
                ProbeResult(
                    "gpu-1",
                    "online",
                    1,
                    (replace(base, uuid=f"GPU-churn-{churn}"),),
                    observed_at="2026-08-10T00:01:00Z",
                )
            )

        host_keys = [key for key in store._gpu_history if key[0] == "gpu-1"]
        self.assertEqual(len(host_keys), _MAX_GPU_IDENTITIES_PER_HOST)
        self.assertIsNone(store.gpu_history("gpu-1", "GPU-0", 10))
        self.assertNotIn(("gpu-1", "GPU-0"), store._process_events)
        newest = f"GPU-churn-{_MAX_GPU_IDENTITIES_PER_HOST - 1}"
        self.assertIsNotNone(store.gpu_history("gpu-1", newest, 10))
        self.assertIsNotNone(store.gpu_history("gpu-1", "GPU-churn-0", 10))

    def test_restored_gpu_identity_churn_is_bounded_before_live_samples(self) -> None:
        base = datetime(2026, 8, 10, tzinfo=timezone.utc)
        restored = LoadedTelemetry(
            history={},
            incident_events=(),
            gpu_history={
                ("gpu-1", f"GPU-{index:03d}"): (
                    {
                        "observedAt": (base + timedelta(seconds=index))
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z"),
                        "index": index,
                    },
                )
                for index in range(_MAX_GPU_IDENTITIES_PER_HOST + 1)
            },
        )

        store = StateStore(5, restored=restored)
        store.set_hosts(("gpu-1",))

        retained = [key for key in store._gpu_history if key[0] == "gpu-1"]
        self.assertEqual(len(retained), _MAX_GPU_IDENTITIES_PER_HOST)
        self.assertNotIn(("gpu-1", "GPU-000"), store._gpu_history)
        self.assertIn(
            ("gpu-1", f"GPU-{_MAX_GPU_IDENTITIES_PER_HOST:03d}"),
            store._gpu_history,
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

        self.assertEqual(snapshot["appVersion"], __version__)
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

    def test_snapshot_view_shares_a_read_only_projection(self) -> None:
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        store.apply(ProbeResult("gpu-1", "online", 12))

        view = store.snapshot_view()
        self.assertEqual(view, store.snapshot())
        self.assertIs(view, store.snapshot_view())

        # Deep copies handed to general callers stay isolated from the view.
        isolated = store.snapshot()
        isolated["stats"]["servers"] = 99
        isolated["servers"][0]["status"] = "changed"
        self.assertEqual(store.snapshot_view()["stats"]["servers"], 1)
        self.assertEqual(store.snapshot_view()["servers"][0]["status"], "online")

        store.apply(ProbeResult("gpu-1", "unreachable", 1))
        self.assertIsNot(view, store.snapshot_view())
        self.assertEqual(store.snapshot_view()["servers"][0]["status"], "unreachable")

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

    def test_adapter_status_change_gets_a_revision_and_wakes_waiters(self) -> None:
        class MutablePersistence(DisabledPersistence):
            def __init__(self) -> None:
                self.healthy = True

            def status(self) -> dict[str, object]:
                return {**super().status(), "healthy": self.healthy}

        persistence = MutablePersistence()
        store = StateStore(5, persistence=persistence)
        before = store.snapshot()

        persistence.healthy = False
        changed = store.wait_for_update(before["version"], 0.3)

        self.assertIsNotNone(changed)
        self.assertGreater(changed["version"], before["version"])
        self.assertFalse(changed["persistence"]["healthy"])

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

    def test_preserves_host_order_and_publishes_observable_poll_start(self) -> None:
        store = StateStore(5)
        store.set_hosts(("node-b", "node-a"))
        version = store.snapshot()["version"]

        store.begin_poll(("node-b", "node-a"))
        snapshot = store.snapshot()

        self.assertEqual(
            [server["host"] for server in snapshot["servers"]], ["node-b", "node-a"]
        )
        self.assertEqual(snapshot["version"], version + 1)
        self.assertEqual(snapshot["stats"]["pollingServers"], 2)

    def test_exposes_configured_collection_freshness_window(self) -> None:
        store = StateStore(5, collection_stale_cycles=4)

        self.assertEqual(store.snapshot()["collectionStaleAfterSeconds"], 20)

    def test_runtime_poll_interval_updates_snapshot_and_stale_window(self) -> None:
        store = StateStore(5, collection_stale_cycles=4)
        before = store.snapshot()["version"]

        self.assertEqual(store.set_poll_interval_seconds(10), 10)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["pollIntervalSeconds"], 10)
        self.assertEqual(snapshot["collectionStaleAfterSeconds"], 40)
        self.assertGreater(snapshot["version"], before)
        # An unchanged value publishes nothing.
        self.assertEqual(store.set_poll_interval_seconds(10), 10)
        self.assertEqual(store.snapshot()["version"], snapshot["version"])

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

    def test_apply_folds_batch_completion_into_one_published_revision(self) -> None:
        store = StateStore(5)
        store.set_hosts(("gpu-1",))
        version = store.snapshot()["version"]

        store.apply(
            ProbeResult("gpu-1", "online", 1),
            poll_cycle_duration_seconds=1.234,
        )

        snapshot = store.snapshot()
        # One revision covers both the probe result and the batch timing;
        # the old separate record_poll_cycle publish added a second one.
        self.assertEqual(snapshot["version"], version + 1)
        self.assertEqual(snapshot["lastPollDurationMs"], 1234)
        self.assertIsNotNone(snapshot["lastPollCompletedAt"])

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

    def test_resolved_transitions_reach_notifications_inside_maintenance(self) -> None:
        # The opened event was delivered before the window began; suppressing
        # the resolved pairing would leave the receiver's alert hanging
        # forever, so resolved transitions always reach the delivery queue.
        notifications = _RecordingNotifications()
        current = [datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)]
        store = StateStore(
            5,
            notifications=notifications,
            utc_clock=lambda: current[0],
        )
        store.set_hosts(("gpu-1",))
        store.apply(
            ProbeResult(
                "gpu-1",
                "unreachable",
                1,
                observed_at="2026-08-10T00:00:00Z",
            )
        )
        published_states = [
            event.state for events, _ in notifications.published for event in events
        ]
        self.assertEqual(published_states, ["opened"])

        window = MaintenanceWindowConfig(
            reason="Planned recovery",
            until=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
        store.set_maintenance_windows((("gpu-1", window),))
        for minute in (1, 2):
            current[0] = datetime(2026, 8, 10, 0, minute, tzinfo=timezone.utc)
            store.apply(
                ProbeResult(
                    "gpu-1",
                    "online",
                    1,
                    observed_at=f"2026-08-10T00:0{minute}:00Z",
                )
            )

        published_states = [
            event.state for events, _ in notifications.published for event in events
        ]
        self.assertEqual(published_states, ["opened", "resolved"])

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

    def test_maintenance_state_is_consistent_across_instance_boundary(self) -> None:
        # 2030-06-19 is a Wednesday (weekday 2); the window runs 18:00-20:00.
        window = MaintenanceWindowConfig(
            reason="Weekly patching",
            weekday=2,
            start_minutes=18 * 60,
            duration_minutes=120,
        )
        inside = datetime(2030, 6, 19, 19, 59, 59, tzinfo=timezone.utc)
        after_boundary = datetime(2030, 6, 19, 20, 0, 1, tzinfo=timezone.utc)
        clock_times = [inside]

        def clock() -> datetime:
            if len(clock_times) > 1:
                return clock_times.pop(0)
            return clock_times[0]

        store = StateStore(
            5,
            maintenance_windows=(("offline", window),),
            utc_clock=clock,
        )
        store.set_hosts(("offline",))
        store.apply(ProbeResult("offline", "unreachable", 5000))

        # Budget one sample for the expiry refresh and one for the assembly;
        # any extra sampling while assembling would cross into next week's
        # instance and contradict the active-window decision.
        clock_times[:] = [inside, inside, after_boundary]
        server = store.snapshot()["servers"][0]
        self.assertIsNotNone(server["maintenance"])
        self.assertEqual(server["maintenance"]["until"], "2030-06-19T20:00:00Z")

        clock_times[:] = [inside, inside, after_boundary]
        condition = store.incidents(10)["active"][0]
        self.assertTrue(condition["silenced"])
        self.assertEqual(condition["maintenanceUntil"], "2030-06-19T20:00:00Z")

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
        silenced_version = silenced["version"]

        current[0] = window.until
        expired = store.wait_for_update(silenced_version, 0)

        self.assertIsNotNone(expired)
        assert expired is not None
        self.assertEqual(silenced["stats"]["actionableIncidents"], 0)
        self.assertEqual(expired["stats"]["actionableIncidents"], 1)
        self.assertEqual(expired["stats"]["maintenanceServers"], 0)
        self.assertIsNone(expired["servers"][0]["maintenance"])
        self.assertGreater(expired["incidentVersion"], silenced["incidentVersion"])
        self.assertGreater(expired["version"], silenced_version)
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

    def test_queued_notification_is_fenced_by_host_incarnation(self) -> None:
        notifications = _RecordingNotifications()
        store = StateStore(5, notifications=notifications)
        store.set_hosts(("gpu-1",))
        store.apply(ProbeResult("gpu-1", "unreachable", 1))
        old_event = notifications.published[-1][0][0]
        self.assertTrue(notifications.actionable_check(old_event))

        store.set_hosts(())
        store.set_hosts(("gpu-1",))
        store.apply(ProbeResult("gpu-1", "unreachable", 1))
        new_event = notifications.published[-1][0][0]

        self.assertFalse(notifications.actionable_check(old_event))
        self.assertTrue(notifications.actionable_check(new_event))

    def test_acknowledged_conditions_do_not_feed_correlation(self) -> None:
        topology = ConnectionTopologyConfig(
            root="monitor",
            links=(
                TopologyLinkConfig("monitor", "gateway", "ssh"),
                TopologyLinkConfig("gateway", "gpu-1", "ssh"),
                TopologyLinkConfig("gateway", "gpu-2", "ssh"),
            ),
        )
        action = IncidentActionConfig(
            host="gpu-2",
            condition_key="connectivity",
            action="acknowledged",
            until=datetime(2026, 8, 11, tzinfo=timezone.utc),
            reason="owner notified",
            incident_started_at="2026-08-10T00:00:00Z",
        )
        notifications = _RecordingNotifications()
        store = StateStore(
            5,
            topology=topology,
            notifications=notifications,
            incident_actions=(action,),
            utc_clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        store.set_hosts(("gpu-1", "gpu-2"))

        store.apply(
            ProbeResult("gpu-1", "unreachable", 1, observed_at="2026-08-10T00:00:00Z")
        )
        store.apply(
            ProbeResult("gpu-2", "unreachable", 1, observed_at="2026-08-10T00:00:00Z")
        )

        # Acknowledgement records ownership without silencing delivery, but
        # the correlator consumes only actionable connectivity conditions.
        self.assertEqual(len(notifications.published), 2)
        self.assertEqual(notifications.published[-1][0][0].host, "gpu-2")
        self.assertEqual(notifications.published[-1][1], ())
        self.assertEqual(store.incidents(10)["correlations"], [])


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


class _ExplodingProbe:
    def probe(self, _host, _config):
        raise RuntimeError("sensitive internal detail")


class _ConfigHostSource:
    def hosts(self, config):
        return config.hosts


class _ResolvedHostSource:
    def __init__(self, discovery: HostDiscoverySnapshot) -> None:
        self._discovery = discovery

    def hosts(self, _config):
        return self._discovery.hosts

    def discovery(self, _config):
        return self._discovery


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


class _StopDuringClearWakeEvent:
    """Simulates stop() landing between the loop predicate and clear()."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event
        self.waits = 0

    def clear(self):
        self._stop_event.set()

    def set(self):
        return None

    def wait(self, timeout):
        del timeout
        self.waits += 1
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


class _SignallingRecordingProbe(_RecordingProbe):
    def __init__(self) -> None:
        super().__init__()
        self.probed = threading.Event()

    def probe(self, host, config):
        result = super().probe(host, config)
        self.probed.set()
        return result


class _AttendedRecordingProbe(_SignallingRecordingProbe):
    def __init__(self) -> None:
        super().__init__()
        self.attended_calls = []

    def set_attended(self, attended):
        self.attended_calls.append(attended)


class _RecordingNotifications:
    def __init__(self) -> None:
        self.published = []

    def publish(self, events, correlations):
        self.published.append((events, correlations))

    def set_actionable_check(self, check):
        self.actionable_check = check

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


class _PollCycleRecordingStore(StateStore):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.apply_durations = []
        self.record_poll_cycle_calls = 0

    def apply(
        self,
        result,
        retry_after_seconds=None,
        poll_cycle_duration_seconds=None,
        expected_incarnation=None,
    ):
        self.apply_durations.append(poll_cycle_duration_seconds)
        return super().apply(
            result,
            retry_after_seconds=retry_after_seconds,
            poll_cycle_duration_seconds=poll_cycle_duration_seconds,
            expected_incarnation=expected_incarnation,
        )

    def record_poll_cycle(self, duration_seconds):
        self.record_poll_cycle_calls += 1
        super().record_poll_cycle(duration_seconds)


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
    def test_unexpected_probe_failure_is_actionably_and_safely_reported(self) -> None:
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
        store = StateStore(60)
        service = MonitorService(config, _ConfigHostSource(), _ExplodingProbe(), store)

        with self.assertLogs("mocop.service", "ERROR") as captured:
            service.poll_once()

        snapshot = store.snapshot()
        self.assertEqual(
            snapshot["servers"][0]["message"], "Unexpected collector error"
        )
        self.assertIn("internal probe failure", snapshot["collectorError"])
        log = "\n".join(captured.output)
        self.assertIn("RuntimeError", log)
        self.assertNotIn("sensitive internal detail", log)
        self.assertEqual(store.health()["reason"], "collector failed unexpectedly")

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

    def test_scheduler_publishes_batch_timing_with_the_final_apply(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=("gpu-01", "gpu-02"),
            exclude_hosts=frozenset(),
            poll_interval_seconds=60,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=2,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        store = _PollCycleRecordingStore(60)
        service = MonitorService(config, _ConfigHostSource(), _OnlineProbe(), store)
        stop_event = threading.Event()
        scheduler = threading.Thread(target=service.run, args=(stop_event,))
        scheduler.start()
        try:
            deadline = time.monotonic() + 2
            while store.snapshot()["lastPollDurationMs"] is None:
                if time.monotonic() >= deadline:
                    self.fail("scheduler never recorded the completed batch")
                threading.Event().wait(0.005)
        finally:
            stop_event.set()
            service.stop()
            scheduler.join(2)

        # The batch duration rides along with the final host's apply instead
        # of a separate record_poll_cycle publish.
        self.assertEqual(store.record_poll_cycle_calls, 0)
        self.assertEqual(len(store.apply_durations), 2)
        batch_durations = [
            duration for duration in store.apply_durations if duration is not None
        ]
        self.assertEqual(len(batch_durations), 1)
        self.assertEqual(
            store.snapshot()["lastPollDurationMs"],
            max(0, round(batch_durations[0] * 1000)),
        )

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

    def test_stop_raced_with_wakeup_clear_skips_the_long_wait(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=False,
            hosts=(),
            exclude_hosts=frozenset(),
            poll_interval_seconds=60,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        service = MonitorService(
            config, _ConfigHostSource(), _OnlineProbe(), StateStore(60)
        )
        stop_event = threading.Event()
        wake_event = _StopDuringClearWakeEvent(stop_event)
        service._scheduler_wakeup = wake_event

        # The stop signal set right after clear() was consumed by it; the loop
        # must still exit before committing to the next long scheduler wait.
        service.run(stop_event)

        self.assertEqual(wake_event.waits, 0)

    def test_stale_config_snapshot_is_not_used_to_submit_probes(self) -> None:
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
        probe = _SignallingRecordingProbe()
        service = MonitorService(config, _ConfigHostSource(), probe, StateStore(60))
        replaced = threading.Event()
        original_rebase = service._rebase_failure_backoff

        def rebase_then_replace_config(now, active_config):
            # Runs after the loop snapshotted the old config and discovered the
            # old inventory, but before this round's probe submissions.
            original_rebase(now, active_config)
            if not replaced.is_set():
                replaced.set()
                service.update_config(replace(config, hosts=("gpu-02",)))

        service._rebase_failure_backoff = rebase_then_replace_config
        stop_event = threading.Event()
        scheduler = threading.Thread(target=service.run, args=(stop_event,))
        scheduler.start()
        try:
            self.assertTrue(probe.probed.wait(2))
        finally:
            stop_event.set()
            service.stop()
            scheduler.join(2)

        self.assertEqual(
            [(host, hosts) for host, hosts, _, _ in probe.calls],
            [("gpu-02", ("gpu-02",))],
        )

    def test_run_relays_dashboard_attendance_to_the_probe(self) -> None:
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

        for attended in (False, True):
            probe = _AttendedRecordingProbe()
            state = StateStore(60)
            if attended:
                state.record_dashboard_activity()
            service = MonitorService(config, _ConfigHostSource(), probe, state)
            stop_event = threading.Event()
            scheduler = threading.Thread(target=service.run, args=(stop_event,))
            scheduler.start()
            try:
                self.assertTrue(probe.probed.wait(2))
            finally:
                stop_event.set()
                service.stop()
                scheduler.join(2)
            with self.subTest(attended=attended):
                self.assertTrue(probe.attended_calls)
                self.assertEqual(probe.attended_calls[0], attended)

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

    def test_resolved_discovery_applies_groups_and_never_probes_infrastructure(
        self,
    ) -> None:
        config = MonitorConfig(
            ssh_config=Path("/tmp/config"),
            auto_discover=True,
            hosts=(),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=2,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        topology = ConnectionTopologyConfig(
            root="monitor",
            links=(
                TopologyLinkConfig("monitor", "bastion", "ssh"),
                TopologyLinkConfig("bastion", "gpu-01", "ssh"),
            ),
        )
        discovery = HostDiscoverySnapshot(
            aliases=("bastion", "gpu-01"),
            eligible_aliases=("gpu-01",),
            hosts=("gpu-01",),
            infrastructure_hosts=("bastion",),
            host_groups=(("gpu-01", "bastion"),),
            topology=topology,
            warnings=(),
            mode="topology",
        )
        state = StateStore(5)
        probe = _RecordingProbe()
        service = MonitorService(config, _ResolvedHostSource(discovery), probe, state)

        service.poll_once()

        self.assertEqual([call[0] for call in probe.calls], ["gpu-01"])
        server = state.snapshot()["servers"][0]
        self.assertEqual(server["group"], "bastion")
        self.assertEqual(state.incidents(10)["correlations"], [])

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

    def test_shutdown_wait_uses_global_timeout_without_host_overrides(self) -> None:
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

        self.assertEqual(service.shutdown_timeout_seconds(), 13)

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
        # The scheduler's own wakeup event is what the run loop waits on.
        self.assertTrue(service._scheduler_wakeup.is_set())
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


class UsageRollupTests(unittest.TestCase):
    """StateStore.usage(): occupancy pairing, idle classification, anchors."""

    _NOW = datetime(2026, 8, 14, 2, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def _gpu(
        processes: tuple[GpuProcess, ...],
        utilization: float | None = 50,
    ) -> GpuMetrics:
        return GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=utilization,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
            processes=processes,
        )

    def _store(self, **kwargs: object) -> StateStore:
        store = StateStore(5, utc_clock=lambda: self._NOW, **kwargs)
        store.set_hosts(("gpu-1",))
        return store

    def _apply(
        self,
        store: StateStore,
        observed_at: str,
        processes: tuple[GpuProcess, ...],
        utilization: float | None = 50,
    ) -> None:
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                1,
                (self._gpu(processes, utilization),),
                observed_at=observed_at,
            )
        )

    def test_usage_pairs_transitions_and_classifies_idle_occupancy(self) -> None:
        store = self._store()
        process = GpuProcess(
            10, "train.py", 250, WorkloadMetadata(kind="process", owner="alice")
        )
        self._apply(store, "2026-08-14T01:00:00Z", (), utilization=50)
        self._apply(store, "2026-08-14T01:00:30Z", (process,), utilization=50)
        self._apply(store, "2026-08-14T01:01:00Z", (process,), utilization=0)
        self._apply(store, "2026-08-14T01:01:30Z", (), utilization=0)

        usage = store.usage(1, 50)

        self.assertEqual(usage["windowHours"], 1)
        self.assertEqual(usage["sinceAt"], "2026-08-14T01:00:00Z")
        self.assertEqual(usage["earliestDataAt"], "2026-08-14T01:00:00Z")
        self.assertEqual(usage["totalOwners"], 1)
        self.assertEqual(usage["droppedRecords"], 0)
        self.assertEqual(usage["totalGpuSeconds"], 60.0)
        (owner,) = usage["owners"]
        self.assertEqual(owner["owner"], "alice")
        self.assertEqual(owner["gpuSeconds"], 60.0)
        # One half of the interval was sampled busy, the other half idle.
        self.assertEqual(owner["sampledSeconds"], 60.0)
        self.assertEqual(owner["idleSeconds"], 30.0)
        self.assertEqual(owner["idleShare"], 0.5)
        self.assertEqual(owner["hosts"], ["gpu-1"])
        self.assertEqual(owner["gpus"], 1)
        self.assertEqual(owner["processes"], 1)
        self.assertEqual(owner["kinds"], {"process": 1})

    def test_usage_includes_open_intervals_up_to_now(self) -> None:
        store = self._store()
        process = GpuProcess(
            11, "serve.py", 200, WorkloadMetadata(kind="process", owner="bob")
        )
        self._apply(store, "2026-08-14T01:59:00Z", ())
        self._apply(store, "2026-08-14T01:59:45Z", (process,))

        (owner,) = store.usage(1, 50)["owners"]
        self.assertEqual(owner["owner"], "bob")
        self.assertEqual(owner["gpuSeconds"], 15.0)

    def test_usage_covers_processes_seeded_before_any_transition(self) -> None:
        # The very first sample of a GPU seeds the live process table without
        # emitting a started transition; the rollup must still count it.
        store = self._store()
        process = GpuProcess(
            12, "notebook.py", 100, WorkloadMetadata(kind="process", owner="dora")
        )
        self._apply(store, "2026-08-14T01:59:00Z", (process,))

        (owner,) = store.usage(1, 50)["owners"]
        self.assertEqual(owner["owner"], "dora")
        self.assertEqual(owner["gpuSeconds"], 60.0)

    def test_usage_drops_unmatched_stops_instead_of_using_process_start(self) -> None:
        record = {
            "observedAt": "2026-08-14T01:30:00Z",
            "gpuId": "GPU-1",
            "index": 0,
            "event": "stopped",
            "pid": 7,
            "name": "old.py",
            "usedMemoryMiB": 10.0,
            "workload": {
                "kind": "docker",
                "workload_id": "abc123def456",
                "owner": "carol",
                "started_at": "2026-08-14T00:00:00Z",
            },
        }
        anonymous = {
            **record,
            "observedAt": "2026-08-14T01:40:00Z",
            "pid": 8,
            "name": "anon.py",
            "workload": None,
        }
        restored = LoadedTelemetry(
            history={},
            incident_events=(),
            process_events={("gpu-1", "GPU-1"): (record, anonymous)},
        )
        store = self._store(restored=restored)

        usage = store.usage(1, 50)

        # A process start timestamp predates GPU observation and is not a safe
        # accounting anchor.  Both orphan stops are therefore reported.
        self.assertEqual(usage["owners"], [])
        self.assertEqual(usage["droppedRecords"], 2)

    def test_usage_unions_concurrent_processes_for_one_owner_and_gpu(self) -> None:
        store = self._store()
        first = GpuProcess(
            10, "train-a.py", 250, WorkloadMetadata(kind="process", owner="alice")
        )
        second = GpuProcess(
            11, "train-b.py", 250, WorkloadMetadata(kind="process", owner="alice")
        )
        self._apply(store, "2026-08-14T01:00:00Z", (first, second))
        self._apply(store, "2026-08-14T02:00:00Z", ())

        (owner,) = store.usage(1, 50)["owners"]
        self.assertEqual(owner["gpuSeconds"], 3600.0)
        self.assertEqual(owner["processes"], 2)

    def test_usage_starts_at_first_gpu_observation_not_workload_start(self) -> None:
        store = self._store()
        process = GpuProcess(
            12,
            "late-context.py",
            100,
            WorkloadMetadata(
                kind="process",
                owner="alice",
                started_at="2026-08-13T20:00:00Z",
            ),
        )
        self._apply(store, "2026-08-14T01:59:00Z", (process,))

        (owner,) = store.usage(1, 50)["owners"]
        self.assertEqual(owner["gpuSeconds"], 60.0)
        self.assertEqual(store.usage(1, 50)["earliestDataAt"], "2026-08-14T01:59:00Z")

    def test_usage_closes_at_last_successful_sample_on_disconnect(self) -> None:
        store = self._store()
        process = GpuProcess(
            13, "train.py", 100, WorkloadMetadata(kind="process", owner="alice")
        )
        self._apply(store, "2026-08-14T01:00:00Z", (process,))
        self._apply(store, "2026-08-14T01:30:00Z", (process,))
        store.apply(
            ProbeResult(
                "gpu-1",
                "unreachable",
                1,
                (),
                message="SSH failed",
                observed_at="2026-08-14T01:31:00Z",
            )
        )

        usage = store.usage(1, 50)
        (owner,) = usage["owners"]
        self.assertEqual(owner["gpuSeconds"], 1800.0)
        self.assertEqual(usage["droppedRecords"], 0)

    def test_usage_history_does_not_change_with_current_poll_interval(self) -> None:
        store = self._store()
        process = GpuProcess(
            14, "train.py", 100, WorkloadMetadata(kind="process", owner="alice")
        )
        self._apply(store, "2026-08-14T01:00:00Z", (process,), utilization=0)
        self._apply(store, "2026-08-14T01:02:00Z", (), utilization=0)
        before = store.usage(1, 50)

        store.set_poll_interval_seconds(60)
        after = store.usage(1, 50)

        self.assertEqual(before["owners"], after["owners"])
        self.assertEqual(before["owners"][0]["sampledSeconds"], 0.0)

    def test_usage_reconciles_restored_pid_reuse_before_attribution(self) -> None:
        restored = LoadedTelemetry(
            history={},
            incident_events=(),
            gpu_history={
                ("gpu-1", "GPU-1"): (
                    {"observedAt": "2026-08-14T01:30:00Z", "index": 0},
                )
            },
            process_events={
                ("gpu-1", "GPU-1"): (
                    {
                        "observedAt": "2026-08-14T01:00:00Z",
                        "gpuId": "GPU-1",
                        "index": 0,
                        "event": "started",
                        "pid": 42,
                        "name": "python",
                        "usedMemoryMiB": 100,
                        "workload": {
                            "kind": "process",
                            "owner": "alice",
                            "started_at": "2026-08-14T00:30:00Z",
                        },
                    },
                )
            },
        )
        store = self._store(restored=restored)
        replacement = GpuProcess(
            42,
            "python",
            100,
            WorkloadMetadata(
                kind="process",
                owner="bob",
                started_at="2026-08-14T01:58:00Z",
            ),
        )
        self._apply(store, "2026-08-14T01:58:00Z", (replacement,))

        owners = {item["owner"]: item for item in store.usage(1, 50)["owners"]}
        self.assertEqual(owners["alice"]["gpuSeconds"], 1800.0)
        self.assertEqual(owners["bob"]["gpuSeconds"], 120.0)

    def test_usage_does_not_extend_orphaned_starts_across_collection_gaps(self) -> None:
        started = {
            "observedAt": "2026-08-14T01:30:00Z",
            "gpuId": "GPU-1",
            "index": 0,
            "event": "started",
            "pid": 9,
            "name": "finished-during-gap.py",
            "usedMemoryMiB": 10.0,
            "workload": {
                "kind": "process",
                "owner": "eve",
                "started_at": "2026-08-14T01:30:00Z",
            },
        }
        restored = LoadedTelemetry(
            history={},
            incident_events=(),
            process_events={("gpu-1", "GPU-1"): (started,)},
        )
        store = self._store(restored=restored)

        usage = store.usage(1, 50)

        self.assertEqual(usage["owners"], [])
        self.assertEqual(usage["totalGpuSeconds"], 0)
        self.assertEqual(usage["droppedRecords"], 1)

    def test_usage_uses_the_store_clock_for_generated_timestamp(self) -> None:
        usage = self._store().usage(1, 50)

        self.assertEqual(usage["generatedAt"], "2026-08-14T02:00:00Z")


if __name__ == "__main__":
    unittest.main()
