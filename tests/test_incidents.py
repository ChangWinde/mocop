from __future__ import annotations

import unittest
from dataclasses import replace

from mocop.config import IncidentConfig, IncidentScopeOverrideConfig, ThresholdConfig
from mocop.incidents import (
    IncidentCondition,
    IncidentEvent,
    IncidentTracker,
    ThresholdIncidentPolicy,
)
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
    processes_sampled: bool = True,
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
        processes_sampled=processes_sampled,
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

    def test_skipped_process_sample_is_not_an_availability_incident(self) -> None:
        policy = ThresholdIncidentPolicy(ThresholdConfig())
        skipped = replace(
            gpu(60),
            processes=(),
            processes_available=False,
            processes_sampled=False,
        )

        conditions = policy.conditions(
            ProbeResult(
                "node-a",
                "online",
                1,
                (skipped,),
                observed_at="2026-08-11T00:00:00Z",
                system=system(20, 20),
            )
        )

        self.assertNotIn("gpu_processes", conditions)

    def test_scoped_thresholds_and_mount_exclusions_override_global_policy(
        self,
    ) -> None:
        policy = ThresholdIncidentPolicy(
            ThresholdConfig(),
            host_overrides=(
                (
                    "node-a",
                    IncidentScopeOverrideConfig(
                        thresholds=(("cpu_warning_pct", 95.0),),
                        exclude_disk_mounts=frozenset({"/"}),
                    ),
                ),
            ),
        )

        conditions = policy.conditions(
            ProbeResult("node-a", "online", 1, system=system(90, 99))
        )

        self.assertNotIn("cpu", conditions)
        self.assertFalse(any(key.startswith("disk:") for key in conditions))

    def test_restores_transition_context_and_continues_event_ids(self) -> None:
        historical = IncidentEvent(
            event_id=7,
            host="old-node",
            condition=IncidentCondition(
                key="connectivity",
                category="connectivity",
                resource="SSH",
                severity="critical",
                value=None,
                threshold=None,
                observed_at="2026-08-09T00:00:00Z",
            ),
            state="opened",
            observed_at="2026-08-09T00:00:00Z",
        )
        tracker = IncidentTracker(
            ThresholdIncidentPolicy(ThresholdConfig()),
            history_points=20,
            historical_events=(historical,),
        )

        created = tracker.update(ProbeResult("new-node", "unreachable", 1))

        self.assertEqual([event.event_id for event in created], [8])
        self.assertEqual(
            [event["eventId"] for event in tracker.snapshot(20)["events"]],
            [8, 7],
        )

    def test_only_condition_and_severity_transitions_create_events(self) -> None:
        warning = ProbeResult("node-a", "online", 1, (gpu(82),), system=system(90, 90))
        self.tracker.update(warning)
        self.tracker.update(warning)
        critical = ProbeResult("node-a", "online", 1, (gpu(86),), system=system(96, 91))
        # Severity changes need the same sustained confirmation as opening.
        self.tracker.update(critical)
        self.tracker.update(critical)
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

    def test_gpu_query_failure_does_not_advance_gpu_recovery(self) -> None:
        hot = ProbeResult("node-a", "online", 1, (gpu(82),), system=system(20, 20))
        self.tracker.update(hot)
        self.tracker.update(hot)

        blind = ProbeResult(
            "node-a",
            "online",
            1,
            message="nvidia-smi query failed",
            system=system(20, 20),
        )
        first = self.tracker.update(blind)
        second = self.tracker.update(blind)

        self.assertEqual(
            [(event.condition.key, event.state) for event in first],
            [("gpu_availability", "opened")],
        )
        self.assertEqual(second, ())
        snapshot = self.tracker.snapshot(20)
        self.assertIn(
            "gpu_temperature:GPU-1",
            {item["conditionKey"] for item in snapshot["active"]},
        )
        self.assertNotIn(
            ("gpu_temperature:GPU-1", "resolved"),
            {(event["conditionKey"], event["state"]) for event in snapshot["events"]},
        )

        recovered = ProbeResult(
            "node-a", "online", 1, (gpu(70),), system=system(20, 20)
        )
        self.tracker.update(recovered)
        self.tracker.update(recovered)
        final = self.tracker.snapshot(20)
        self.assertEqual(final["active"], [])
        self.assertLessEqual(
            {
                ("gpu_temperature:GPU-1", "resolved"),
                ("gpu_availability", "resolved"),
            },
            {(event["conditionKey"], event["state"]) for event in final["events"]},
        )

    def test_missing_health_telemetry_freezes_health_recovery(self) -> None:
        def health(errors: int) -> GpuHealthMetrics:
            return GpuHealthMetrics(
                ecc_uncorrected_volatile=errors,
                retired_pages_pending=False,
                remapped_rows_pending=False,
                thermal_slowdown=False,
                power_brake_slowdown=False,
                mig_mode="Disabled",
            )

        sick = ProbeResult(
            "node-a", "online", 1, (gpu(60, health=health(2)),), system=system(20, 20)
        )
        self.tracker.update(sick)
        self.tracker.update(sick)

        no_health = ProbeResult(
            "node-a", "online", 1, (gpu(60),), system=system(20, 20)
        )
        self.tracker.update(no_health)
        self.tracker.update(no_health)
        self.assertIn(
            "gpu_ecc:GPU-1",
            {item["conditionKey"] for item in self.tracker.snapshot(20)["active"]},
        )

        healthy = ProbeResult(
            "node-a", "online", 1, (gpu(60, health=health(0)),), system=system(20, 20)
        )
        self.tracker.update(healthy)
        self.tracker.update(healthy)
        self.assertNotIn(
            "gpu_ecc:GPU-1",
            {item["conditionKey"] for item in self.tracker.snapshot(20)["active"]},
        )

    def test_skipped_process_sampling_freezes_process_recovery(self) -> None:
        unavailable = replace(gpu(60), processes=(), processes_available=False)
        broken = ProbeResult(
            "node-a", "online", 1, (unavailable,), system=system(20, 20)
        )
        self.tracker.update(broken)
        self.tracker.update(broken)

        skipped_gpu = replace(
            gpu(60), processes=(), processes_available=False, processes_sampled=False
        )
        skipped = ProbeResult(
            "node-a", "online", 1, (skipped_gpu,), system=system(20, 20)
        )
        self.tracker.update(skipped)
        self.tracker.update(skipped)
        self.assertIn(
            "gpu_processes",
            {item["conditionKey"] for item in self.tracker.snapshot(20)["active"]},
        )

        sampled = ProbeResult("node-a", "online", 1, (gpu(60),), system=system(20, 20))
        self.tracker.update(sampled)
        self.tracker.update(sampled)
        self.assertNotIn(
            "gpu_processes",
            {item["conditionKey"] for item in self.tracker.snapshot(20)["active"]},
        )

    def test_online_sample_without_system_metrics_freezes_system_recovery(
        self,
    ) -> None:
        warning = ProbeResult("node-a", "online", 1, system=system(90, 20))
        self.tracker.update(warning)
        self.tracker.update(warning)

        headless = ProbeResult("node-a", "online", 1)
        self.tracker.update(headless)
        self.tracker.update(headless)
        self.assertEqual(
            [item["conditionKey"] for item in self.tracker.snapshot(20)["active"]],
            ["cpu"],
        )

        recovered = ProbeResult("node-a", "online", 1, system=system(20, 20))
        self.tracker.update(recovered)
        self.tracker.update(recovered)
        self.assertEqual(self.tracker.snapshot(20)["active"], [])

    def test_first_online_sample_emits_opened_for_immediate_conditions(self) -> None:
        tracker = IncidentTracker(
            ThresholdIncidentPolicy(
                ThresholdConfig(), expected_gpu_counts=(("node-a", 2),)
            ),
            history_points=20,
        )

        created = tracker.update(
            ProbeResult(
                "node-a",
                "online",
                1,
                message="nvidia-smi query failed",
                observed_at="2026-08-11T00:00:00Z",
                system=system(20, 20),
            )
        )

        self.assertEqual(
            [(event.condition.key, event.state) for event in created],
            [("gpu_availability", "opened")],
        )
        snapshot = tracker.snapshot(20)
        self.assertGreater(snapshot["version"], 0)
        self.assertEqual(
            [(event["conditionKey"], event["state"]) for event in snapshot["events"]],
            [("gpu_availability", "opened")],
        )

    def test_severity_flapping_requires_sustained_confirmation(self) -> None:
        warning = ProbeResult("node-a", "online", 1, system=system(90, 20))
        critical = ProbeResult("node-a", "online", 1, system=system(96, 20))
        self.tracker.update(warning)
        self.tracker.update(warning)

        for sample in (critical, warning, critical, warning):
            self.tracker.update(sample)

        flapping = self.tracker.snapshot(20)
        self.assertEqual([event["state"] for event in flapping["events"]], ["opened"])
        self.assertEqual(flapping["active"][0]["severity"], "warning")

        self.tracker.update(critical)
        self.tracker.update(critical)
        escalated = self.tracker.snapshot(20)
        self.assertEqual(escalated["events"][0]["state"], "escalated")
        self.assertEqual(escalated["active"][0]["severity"], "critical")

        self.tracker.update(warning)
        self.tracker.update(warning)
        deescalated = self.tracker.snapshot(20)
        self.assertEqual(deescalated["events"][0]["state"], "deescalated")
        self.assertEqual(deescalated["active"][0]["severity"], "warning")


if __name__ == "__main__":
    unittest.main()
