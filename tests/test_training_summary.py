import unittest

from mocop.training_projection import project_job
from tests.test_training_jobs import job


class TrainingSummaryTests(unittest.TestCase):
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
