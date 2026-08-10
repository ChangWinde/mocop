from __future__ import annotations

import unittest

from mocop.config import IncidentConfig, ThresholdConfig
from mocop.incidents import IncidentTracker, ThresholdIncidentPolicy
from mocop.models import (
    DiskMetrics,
    GpuHealthMetrics,
    GpuMetrics,
    GpuProcess,
    ProbeResult,
    SystemMetrics,
)


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


def gpu(
    temperature: float,
    *,
    utilization: float = 10,
    memory_used: float = 10,
    health: GpuHealthMetrics | None = None,
    processes_available: bool = True,
) -> GpuMetrics:
    return GpuMetrics(
        index=0,
        uuid="GPU-1",
        name="Test GPU",
        driver_version="550",
        pstate="P0",
        temperature_c=temperature,
        utilization_gpu_pct=utilization,
        utilization_memory_pct=10,
        memory_total_mib=100,
        memory_used_mib=memory_used,
        memory_free_mib=100 - memory_used,
        power_draw_w=50,
        power_limit_w=100,
        processes=(GpuProcess(42, "python", memory_used),) if memory_used else (),
        processes_available=processes_available,
        health=health,
    )


class IncidentTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = IncidentTracker(
            ThresholdIncidentPolicy(
                ThresholdConfig(),
                incidents=IncidentConfig(gpu_idle_memory_cycles=3),
            ),
            history_points=20,
        )

    def test_initial_online_sample_honors_open_window_without_events(self) -> None:
        warning = ProbeResult(
            "node-a",
            "online",
            1,
            (gpu(82),),
            observed_at="2026-08-09T00:00:00Z",
            system=system(90, 90),
        )
        self.tracker.update(warning)

        snapshot = self.tracker.snapshot(20)
        self.assertEqual(snapshot["version"], 0)
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(snapshot["active"], [])

        self.tracker.update(warning)
        snapshot = self.tracker.snapshot(20)
        self.assertEqual(
            {incident["category"] for incident in snapshot["active"]},
            {"cpu", "disk", "gpu_temperature"},
        )

    def test_only_condition_and_severity_transitions_create_events(self) -> None:
        warning = ProbeResult("node-a", "online", 1, (gpu(82),), system=system(90, 90))
        self.tracker.update(warning)
        self.tracker.update(warning)
        self.tracker.update(
            ProbeResult("node-a", "online", 1, (gpu(86),), system=system(96, 91))
        )
        after_escalation = self.tracker.snapshot(20)
        self.assertEqual(after_escalation["version"], 5)
        self.assertEqual(
            {
                (event["category"], event["state"])
                for event in after_escalation["events"][:2]
            },
            {("cpu", "escalated"), ("gpu_temperature", "escalated")},
        )

        recovered_sample = ProbeResult(
            "node-a", "online", 1, (gpu(70),), system=system(20, 20)
        )
        self.tracker.update(recovered_sample)
        self.assertEqual(len(self.tracker.snapshot(20)["active"]), 3)
        self.tracker.update(recovered_sample)
        final = self.tracker.snapshot(20)
        self.assertEqual(final["active"], [])
        self.assertEqual(
            [event["state"] for event in final["events"][:3]],
            ["resolved", "resolved", "resolved"],
        )

    def test_failed_probe_preserves_resource_incidents_until_fresh_recovery(
        self,
    ) -> None:
        warning = ProbeResult("node-a", "online", 1, system=system(20, 90))
        self.tracker.update(warning)
        self.tracker.update(warning)
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
            [("connectivity", "opened"), ("disk", "opened")],
        )

        healthy = ProbeResult(
            "node-a",
            "online",
            1,
            observed_at="2026-08-09T00:00:10Z",
            system=system(20, 20),
        )
        self.tracker.update(healthy)
        recovering = self.tracker.snapshot(20)
        self.assertEqual(
            {incident["category"] for incident in recovering["active"]},
            {"connectivity", "disk"},
        )
        self.tracker.update(healthy)
        recovered = self.tracker.snapshot(20)
        self.assertEqual(recovered["active"], [])
        self.assertEqual(
            {(event["category"], event["state"]) for event in recovered["events"][:2]},
            {("connectivity", "resolved"), ("disk", "resolved")},
        )

    def test_event_log_is_bounded_and_newest_first(self) -> None:
        tracker = IncidentTracker(
            ThresholdIncidentPolicy(
                ThresholdConfig(), incidents=IncidentConfig(recovery_cycles=1)
            ),
            history_points=2,
        )
        tracker.update(ProbeResult("node-a", "unreachable", 1))
        tracker.update(ProbeResult("node-a", "online", 1, system=system(1, 1)))
        tracker.update(ProbeResult("node-a", "unreachable", 1))

        events = tracker.snapshot(20)["events"]
        self.assertEqual([event["eventId"] for event in events], [3, 2])

    def test_removing_a_host_invalidates_the_active_incident_revision(self) -> None:
        self.tracker.update(
            ProbeResult("node-a", "online", 1, (gpu(82),), system=system(90, 90))
        )
        self.tracker.update(
            ProbeResult("node-a", "online", 1, (gpu(82),), system=system(90, 90))
        )
        previous_version = self.tracker.version
        previous_events = self.tracker.snapshot(20)["events"]

        self.tracker.remove_hosts(set())

        snapshot = self.tracker.snapshot(20)
        self.assertEqual(snapshot["version"], previous_version + 1)
        self.assertEqual(snapshot["active"], [])
        self.assertEqual(snapshot["events"], previous_events)

    def test_requires_stable_open_and_recovery_samples(self) -> None:
        self.tracker.update(
            ProbeResult("node-a", "online", 1, (gpu(70),), system=system(20, 20))
        )
        warning = ProbeResult("node-a", "online", 1, (gpu(70),), system=system(90, 20))
        self.tracker.update(warning)
        self.assertEqual(self.tracker.snapshot(20)["active"], [])
        self.tracker.update(warning)
        self.assertEqual(
            [item["category"] for item in self.tracker.snapshot(20)["active"]],
            ["cpu"],
        )

        healthy = ProbeResult("node-a", "online", 1, (gpu(70),), system=system(20, 20))
        self.tracker.update(healthy)
        self.assertEqual(len(self.tracker.snapshot(20)["active"]), 1)
        self.tracker.update(healthy)
        self.assertEqual(self.tracker.snapshot(20)["active"], [])

    def test_connectivity_flapping_opens_once_until_stable_recovery(self) -> None:
        healthy = ProbeResult("node-a", "online", 1, system=system(20, 20))
        failed = ProbeResult("node-a", "unreachable", 5000)
        self.tracker.update(healthy)
        for result in (failed, healthy, failed, healthy):
            self.tracker.update(result)

        snapshot = self.tracker.snapshot(20)
        self.assertEqual(
            [(event["category"], event["state"]) for event in snapshot["events"]],
            [("connectivity", "opened")],
        )
        self.assertEqual(snapshot["active"][0]["category"], "connectivity")

    def test_detects_gpu_availability_health_pressure_and_sustained_idle_memory(
        self,
    ) -> None:
        policy = ThresholdIncidentPolicy(
            ThresholdConfig(),
            expected_gpu_counts=(("node-a", 2),),
            incidents=IncidentConfig(gpu_idle_memory_cycles=3),
        )
        unhealthy = gpu(
            70,
            utilization=0,
            memory_used=96,
            health=GpuHealthMetrics(
                ecc_uncorrected_volatile=1,
                retired_pages_pending=False,
                remapped_rows_pending=True,
                thermal_slowdown=True,
                power_brake_slowdown=False,
                mig_mode="Disabled",
            ),
            processes_available=False,
        )
        result = ProbeResult(
            "node-a",
            "online",
            1,
            (unhealthy,),
            message="nvidia-smi query failed",
            system=system(20, 20),
        )

        conditions = policy.conditions(result)

        self.assertEqual(
            {condition.category for condition in conditions.values()},
            {
                "gpu_availability",
                "gpu_processes",
                "gpu_memory",
                "gpu_idle_memory",
                "gpu_ecc",
                "gpu_memory_repair",
                "gpu_slowdown",
            },
        )
        idle = next(
            condition
            for condition in conditions.values()
            if condition.category == "gpu_idle_memory"
        )
        self.assertEqual(idle.open_after_cycles, 3)

        count_conditions = policy.conditions(
            ProbeResult(
                "node-a",
                "online",
                1,
                (unhealthy,),
                system=system(20, 20),
            )
        )
        self.assertIn("gpu_count", count_conditions)
        self.assertEqual(
            sum(
                condition.category == "gpu_processes"
                for condition in count_conditions.values()
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
