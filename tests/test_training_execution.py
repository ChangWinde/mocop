import unittest

from mocop.training_execution import execution
from mocop.training_insights import overview
from mocop.zenith_monitor import TrainingMonitor

NOW = 1_900_000_000_000


def observed(*states, status="RUNNING", age=0, **kwargs):
    return {
        "status": status,
        "gpu_count": 4,
        "updated_at": NOW,
        "replicas_status": 200,
        "replicas": {
            "fetched_at": NOW - age,
            "complete": True,
            "total": len(states),
            "replicas": [{"status": s} if isinstance(s, str) else s for s in states],
        },
        **kwargs,
    }


class ExecutionTests(unittest.TestCase):
    def test_provider_starting_is_not_counted_as_running(self):
        rows = [
            observed("Running"),
            observed("ContainerCreating"),
            observed({"status": "Pending", "secondary_status": "ImagePullBackOff"}),
        ]
        result = overview(rows, NOW)
        self.assertEqual(result["known_running_cards"], 4)
        self.assertEqual(result["known_starting_cards"], 4)
        self.assertEqual(result["states"]["startup_failed"]["cards"], 4)
        self.assertEqual(result["known_active_cards"], 8)

    def test_stale_partial_and_failed_replica_reads_do_not_override_ledger(self):
        for job in (
            observed("Pending", age=120001),
            observed("Pending", replicas_status=503),
        ):
            self.assertEqual(execution(job, NOW)["status"], "RUNNING")
        job = observed("Pending")
        job["replicas"]["complete"] = False
        self.assertEqual(execution(job, NOW)["status"], "RUNNING")
        job["replicas"]["complete"] = True
        job["replicas"]["total"] = 2
        self.assertEqual(execution(job, NOW)["status"], "RUNNING")

    def test_terminal_job_wins_over_old_running_replicas(self):
        job = observed(
            "Running", status="FAILED", started_at=NOW - 1000, runtime_seconds=1
        )
        self.assertEqual(execution(job, NOW)["status"], "FAILED")
        self.assertEqual(overview([job], NOW)["known_running_cards"], 0)

    def test_missing_start_time_does_not_imply_startup_failure(self):
        for runtime in (None, False, 1):
            self.assertEqual(
                execution({"status": "FAILED", "runtime_seconds": runtime}, NOW)[
                    "status"
                ],
                "FAILED",
            )
        self.assertEqual(
            execution({"status": "FAILED", "runtime_seconds": 0}, NOW)["status"],
            "STARTUP_FAILED",
        )
        self.assertEqual(
            execution(observed("Failed", status="PROVISIONING"), NOW)["status"],
            "STARTUP_FAILED",
        )
        self.assertEqual(execution(observed("Failed"), NOW)["status"], "FAILED")

    def test_mixed_replicas_are_not_all_counted_as_running(self):
        job = observed("Running", "Pending")
        self.assertEqual(execution(job, NOW)["status"], "MIXED")
        result = overview([job], NOW)
        self.assertEqual(result["known_running_cards"], 0)
        self.assertEqual(result["states"]["mixed"]["cards"], 4)
        self.assertEqual(result["known_active_cards"], 4)

    def test_startup_failures_are_recent_and_keep_unknown_card_counts(self):
        current = {
            "status": "FAILED",
            "runtime_seconds": 0,
            "updated_at": NOW,
            "gpu_count": 8,
        }
        rows = [
            current,
            {**current, "gpu_count": None},
            {**current, "updated_at": NOW - 86400001},
            {**current, "updated_at": None},
        ]
        result = overview(rows, NOW)["states"]["startup_failed"]
        self.assertEqual(
            (result["jobs"], result["cards"], result["unknown_card_jobs"]), (2, 8, 1)
        )

    def test_fresh_browser_deployment_phase_expires(self):
        job = {
            "status": "RUNNING",
            "browser_execution_status": "PROVISIONING",
            "browser_status_sampled_at": NOW,
        }
        self.assertEqual(execution(job, NOW)["status"], "STARTING")
        self.assertEqual(execution(job, NOW + 120001)["status"], "RUNNING")
        job["status"] = "STOPPED"
        self.assertEqual(execution(job, NOW)["status"], "STOPPED")

    def test_snapshot_uses_same_classification_for_rows_and_counts(self):
        import time

        monitor = TrainingMonitor(start=False)
        job = observed("ContainerCreating")
        job["replicas"]["fetched_at"] = time.time() * 1000
        monitor.jobs = [job]
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["jobs"][0]["execution"]["status"], "STARTING")
        self.assertEqual(snapshot["overview"]["known_running_cards"], 0)
        self.assertEqual(snapshot["overview"]["known_starting_cards"], 4)
        self.assertNotIn("execution", monitor.jobs[0])
