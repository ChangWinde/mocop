from __future__ import annotations

import unittest

from mocop.config import ThresholdConfig
from mocop.incidents import IncidentTracker, ThresholdIncidentPolicy
from mocop.models import DiskMetrics, GpuMetrics, ProbeResult, SystemMetrics


def system(cpu: float, disk: float) -> SystemMetrics:
    return SystemMetrics(
        hostname="node-a",
        uptime_seconds=100,
        load_1m=1,
        load_5m=1,
        load_15m=1,
        cpu_cores=8,
        cpu_usage_pct=cpu,
        memory_total_mib=100,
        memory_used_mib=20,
        memory_available_mib=80,
        swap_total_mib=0,
        swap_used_mib=0,
        disk_total_mib=100,
        disk_used_mib=disk,
        network_rx_bps=0,
        network_tx_bps=0,
        disks=(DiskMetrics("/dev/a", "ext4", "/", 100, disk, 100 - disk, disk),),
    )


def gpu(temperature: float) -> GpuMetrics:
    return GpuMetrics(
        index=0,
        uuid="GPU-1",
        name="Test GPU",
        driver_version="550",
        pstate="P0",
        temperature_c=temperature,
        utilization_gpu_pct=10,
        utilization_memory_pct=10,
        memory_total_mib=100,
        memory_used_mib=10,
        memory_free_mib=90,
        power_draw_w=50,
        power_limit_w=100,
    )


class IncidentTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = IncidentTracker(
            ThresholdIncidentPolicy(ThresholdConfig()), history_points=20
        )

    def test_initial_online_sample_seeds_without_fabricating_events(self) -> None:
        self.tracker.update(
            ProbeResult(
                "node-a",
                "online",
                1,
                (gpu(82),),
                observed_at="2026-08-09T00:00:00Z",
                system=system(90, 90),
            )
        )

        snapshot = self.tracker.snapshot(20)
        self.assertEqual(snapshot["version"], 0)
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(
            {incident["category"] for incident in snapshot["active"]},
            {"cpu", "disk", "gpu_temperature"},
        )

    def test_only_condition_and_severity_transitions_create_events(self) -> None:
        self.tracker.update(
            ProbeResult("node-a", "online", 1, (gpu(82),), system=system(90, 90))
        )
        self.tracker.update(
            ProbeResult("node-a", "online", 1, (gpu(86),), system=system(96, 91))
        )
        after_escalation = self.tracker.snapshot(20)
        self.assertEqual(after_escalation["version"], 2)
        self.assertEqual(
            {
                (event["category"], event["state"])
                for event in after_escalation["events"]
            },
            {("cpu", "escalated"), ("gpu_temperature", "escalated")},
        )

        self.tracker.update(
            ProbeResult("node-a", "online", 1, (gpu(70),), system=system(20, 20))
        )
        final = self.tracker.snapshot(20)
        self.assertEqual(final["active"], [])
        self.assertEqual(
            [event["state"] for event in final["events"][:3]],
            ["resolved", "resolved", "resolved"],
        )

    def test_failed_probe_preserves_resource_incidents_until_fresh_recovery(
        self,
    ) -> None:
        self.tracker.update(ProbeResult("node-a", "online", 1, system=system(20, 90)))
        self.tracker.update(
            ProbeResult(
                "node-a",
                "unreachable",
                5000,
                message="SSH connection timed out",
                observed_at="2026-08-09T00:00:05Z",
            )
        )
        failed = self.tracker.snapshot(20)
        self.assertEqual(
            {incident["category"] for incident in failed["active"]},
            {"connectivity", "disk"},
        )
        self.assertEqual(
            [(event["category"], event["state"]) for event in failed["events"]],
            [("connectivity", "opened")],
        )

        self.tracker.update(
            ProbeResult(
                "node-a",
                "online",
                1,
                observed_at="2026-08-09T00:00:10Z",
                system=system(20, 20),
            )
        )
        recovered = self.tracker.snapshot(20)
        self.assertEqual(recovered["active"], [])
        self.assertEqual(
            {(event["category"], event["state"]) for event in recovered["events"][:2]},
            {("connectivity", "resolved"), ("disk", "resolved")},
        )

    def test_event_log_is_bounded_and_newest_first(self) -> None:
        tracker = IncidentTracker(
            ThresholdIncidentPolicy(ThresholdConfig()), history_points=2
        )
        tracker.update(ProbeResult("node-a", "unreachable", 1))
        tracker.update(ProbeResult("node-a", "online", 1, system=system(1, 1)))
        tracker.update(ProbeResult("node-a", "unreachable", 1))

        events = tracker.snapshot(20)["events"]
        self.assertEqual([event["eventId"] for event in events], [3, 2])


if __name__ == "__main__":
    unittest.main()
