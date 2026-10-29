import unittest

from mocop.zenith_monitor import TrainingMonitor

project_job = TrainingMonitor(start=False).job


def job(**kwargs):
    return {
        "id": "a" * 25,
        "vendor": "baidu",
        "job_id": "fictional-job",
        "status": "RUNNING",
        **kwargs,
    }


class TrainingSummaryTests(unittest.TestCase):
    def test_gpu_requests_total_all_roles_and_keep_models(self):
        row = project_job(
            job(
                resource_requests=[
                    {"gpu_type": "Example A", "gpu_count": 8, "replicas": 2},
                    {"gpu_type": "Example B", "gpu_count": 4, "replicas": 3},
                ]
            )
        )
        self.assertEqual(row["gpu_count"], 28)
        self.assertEqual(row["gpu_model"], "Example A / Example B")
        self.assertEqual(row["gpu_count_source"], "resource_requests")

    def test_incomplete_or_oversized_resource_requests_do_not_undercount(self):
        good = {"gpu_type": "Example A", "gpu_count": 8, "replicas": 2}
        for requests in [
            [good, {"gpu_count": 8}],
            [good, None],
            [good, {"gpu_count": True, "replicas": 1}],
            [good, {"gpu_count": 8, "replicas": -1}],
            [good] * 65,
            [{"gpu_count": 256, "replicas": 256}],
        ]:
            with self.subTest(requests=requests):
                self.assertIsNone(
                    project_job(job(resource_requests=requests))["gpu_count"]
                )
        row = project_job(job(resource_requests=[good], gpu_count=4))
        self.assertEqual(row["gpu_count"], 4)
        self.assertEqual(row["gpu_count_source"], "platform")
        row = project_job(
            job(resource_requests=[good, {"gpu_count": 8, "replicas": 1}])
        )
        self.assertEqual(row["gpu_count"], 24)
        self.assertIsNone(row["gpu_model"])

    def test_resource_models_are_redacted_and_reported_runtime_preserves_zero(self):
        row = project_job(
            job(
                resource_requests=[
                    {"gpu_type": "password=secret", "gpu_count": 0, "replicas": 2}
                ],
                runtime_seconds=0,
            )
        )
        self.assertEqual(row["gpu_count"], 0)
        self.assertNotIn("secret", row["gpu_model"])
        self.assertEqual(row["runtime_seconds"], 0)
        for value in [True, -1, float("inf"), "10", 10**100]:
            with self.subTest(value=value):
                self.assertIsNone(
                    project_job(job(runtime_seconds=value))["runtime_seconds"]
                )

    def test_explicit_resources_and_start_time_are_bounded(self):
        row = project_job(job(gpu_count=8, gpu_model="Example GPU", started_at=1234))
        self.assertEqual(row["gpu_count"], 8)
        self.assertEqual(row["gpu_model"], "Example GPU")
        self.assertEqual(row["started_at"], 1234)
        for invalid in [True, -1, 0.5, "8", 100000000]:
            with self.subTest(value=invalid):
                self.assertIsNone(project_job(job(gpu_count=invalid))["gpu_count"])
        self.assertEqual(project_job(job(gpu_count=0))["gpu_count"], 0)

    def test_absent_resources_stay_unknown_and_secrets_are_redacted(self):
        row = project_job(job(name="Example 8GPU", replicas=8))
        self.assertIsNone(row["gpu_count"])
        self.assertIsNone(row["started_at"])
        row = project_job(job(gpu_model="password=secret"))
        self.assertNotIn("secret", row["gpu_model"])
