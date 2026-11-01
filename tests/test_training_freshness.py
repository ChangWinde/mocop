import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from mocop.platform_endpoint import read_api_prefix
from mocop.zenith_monitor import NoRedirect, ReadError, TrainingMonitor


class TrainingFreshnessTests(unittest.TestCase):
    def test_api_version_is_private_explicit_and_redirects_stay_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-prefix"
            self.assertEqual(read_api_prefix(path), "/pt/api")
            for value in ("/pt/api", "/pt/api2"):
                path.write_text(value)
                path.chmod(0o600)
                self.assertEqual(read_api_prefix(path), value)
            for value in (
                "https://evil.example.test",
                "/pt/api2/../admin",
                "/pt/api3",
                "",
            ):
                path.write_text(value)
                self.assertIsNone(read_api_prefix(path))
            path.write_text("/pt/api2")
            path.chmod(0o644)
            self.assertIsNone(read_api_prefix(path))
            link = Path(directory) / "link"
            link.symlink_to(path)
            self.assertIsNone(read_api_prefix(link))
        monitor = TrainingMonitor(start=False)
        monitor.token = lambda: "znt_" + "a" * 30
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = (
            b'{"jobs":[]}'
        )
        with (
            patch("mocop.zenith_monitor.ORIGIN", "https://training.example.test"),
            patch("mocop.zenith_monitor.API_PREFIX", "/pt/api2"),
            patch("mocop.zenith_monitor.build_opener", return_value=opener),
        ):
            monitor.read("/pt/api/ai/jobs?page=1&page_size=100")
            request = opener.open.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "https://training.example.test/pt/api2/ai/jobs?page=1&page_size=100",
            )
            opener.open.side_effect = HTTPError(
                request.full_url, 302, "Moved", {}, None
            )
            with self.assertRaises(ReadError) as error:
                monitor.read("/pt/api/ai/jobs?page=1&page_size=100")
            self.assertEqual(error.exception.status, 302)
        self.assertIsNone(
            NoRedirect().redirect_request(
                None, None, 302, "Moved", {}, "https://evil.example.test"
            )
        )
        with patch("mocop.zenith_monitor.API_PREFIX", None):
            with self.assertRaises(ReadError) as error:
                monitor.read("/pt/api/ai/jobs")
            self.assertEqual(error.exception.status, 503)

    def test_two_list_and_observation_batches_fit_the_published_read_budget(self):
        monitor = TrainingMonitor(start=False)
        monitor.token = lambda: "znt_" + "a" * 30
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b"{}"
        with (
            patch("mocop.zenith_monitor.ORIGIN", "https://training.example.test"),
            patch("mocop.zenith_monitor.API_PREFIX", "/pt/api2"),
            patch("mocop.zenith_monitor.time.monotonic", return_value=100),
            patch("mocop.zenith_monitor.build_opener", return_value=opener),
        ):
            # Two adjacent 2350-job polls plus metrics: 2*(24 list+2 accounting+16 observations).
            for _ in range(84):
                monitor.read("/pt/api/ai/jobs?page=1&page_size=100")
            for _ in range(6):
                monitor.read("/pt/api/ai/jobs?page=1&page_size=100")
            with self.assertRaises(ReadError) as error:
                monitor.read("/pt/api/ai/jobs?page=1&page_size=100")
            self.assertEqual(error.exception.status, 429)
            self.assertGreater(error.exception.retry_after, 0)

    def test_list_over_two_thousand_records_finishes_pagination(self):
        monitor = TrainingMonitor(start=False)
        monitor.collect_platform = lambda: None
        monitor.token = lambda: "znt_" + "a" * 30
        total = 2055

        def read(path):
            page = int(path.split("page=")[1].split("&")[0])
            return {
                "total": total,
                "jobs": [
                    {
                        "id": str(i).zfill(25),
                        "vendor": "baidu",
                        "job_id": str(i),
                        "status": "SUCCEEDED",
                    }
                    for i in range((page - 1) * 100, min(page * 100, total))
                ],
            }

        monitor.read = read
        monitor.collect()
        result = monitor.snapshot()
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["jobs"]), total)
        self.assertEqual(result["total"], total)

    def test_recent_refresh_revalidates_long_running_jobs_without_reloading_history(
        self,
    ):
        monitor = TrainingMonitor(start=False)
        monitor.collect_platform = lambda: None
        monitor.token = lambda: "znt_" + "a" * 30

        def row(i, status="SUCCEEDED"):
            return {
                "id": str(i).zfill(25),
                "vendor": "baidu",
                "job_id": "fictional-" + str(i),
                "status": status,
                "created_at": i,
                "gpu_count": 4,
            }

        monitor.jobs = [
            monitor.job(row(i, "RUNNING" if i == 1 else "SUCCEEDED"))
            for i in range(2055)
        ]
        monitor.total, monitor.complete = 2055, True
        monitor.full_list_collected_at = time.monotonic()
        calls = []

        def read(path):
            calls.append(path)
            if "/by-id/" in path:
                return {"job": row(1, "SUCCEEDED")}
            return {
                "total": 2056,
                "jobs": [row(2055, "RUNNING")]
                + [row(i) for i in range(2054, 1955, -1)],
            }

        monitor.read = read
        monitor.observations = lambda job: {"id": job["id"]}
        monitor.collect()
        snapshot = monitor.snapshot()
        self.assertEqual(
            calls,
            [
                "/pt/api/ai/jobs?page=1&page_size=100",
                "/pt/api/ai/jobs/by-id/" + str(1).zfill(25),
            ],
        )
        self.assertTrue(snapshot["complete"])
        self.assertEqual(len(snapshot["jobs"]), 2056)
        self.assertEqual(snapshot["overview"]["running_jobs"], 1)
        self.assertEqual(snapshot["overview"]["known_running_cards"], 4)

    def test_expired_history_is_reconciled_in_full(self):
        from mocop.training_listing import collect_listing

        monitor = TrainingMonitor(start=False)
        monitor.token = lambda: "znt_" + "a" * 30
        monitor.jobs = [{"id": "a" * 25, "status": "SUCCEEDED"}]
        monitor.total, monitor.complete = 1, True
        monitor.full_list_collected_at = 0
        monitor.read = lambda path: {"jobs": [], "total": 0}
        rows, complete, total = collect_listing(monitor, 901, {"SUCCEEDED"}, ReadError)
        self.assertEqual((rows, complete, total), ([], True, 0))
        self.assertEqual(monitor.full_list_collected_at, 901)

    def test_snapshot_expires_cached_utilization_without_another_successful_poll(self):
        monitor = TrainingMonitor(start=False)
        now = time.time() * 1000
        monitor.sampled_at = monitor.list_sampled_at = now - 600000
        monitor.jobs = [
            {
                "status": "RUNNING",
                "gpu_count": 4,
                "gpu_utilization": {"pct": 90},
                "metrics": {
                    "source": "provider",
                    "series": [
                        {
                            "metric": "GpuUsage",
                            "points": [{"ts": now - 600000, "value": 90}],
                        }
                    ],
                },
            }
        ]
        result = monitor.snapshot()
        self.assertTrue(result["stale"])
        self.assertIsNone(result["jobs"][0]["gpu_utilization"]["pct"])
        self.assertGreaterEqual(result["list_age_seconds"], 600)
        self.assertEqual(monitor.jobs[0]["gpu_utilization"]["pct"], 90)

    def test_one_job_forbidden_observation_does_not_freeze_the_task_list(self):
        monitor = TrainingMonitor(start=False)
        monitor.replicas = lambda _: (_ for _ in ()).throw(ReadError(403))
        monitor.metrics = lambda _: (_ for _ in ()).throw(ReadError(403))
        result = monitor.observations(
            {"id": "a" * 25, "capabilities": {"replicas": True}}
        )
        self.assertEqual(result["metrics_status"], 403)
        self.assertIsNone(result["metrics"])
        self.assertIsNone(result["replicas"])

    def test_tab_discovery_uses_the_non_dom_macos_script_environment(self):
        from mocop import training_browser_capture as bridge

        outputs = [
            MagicMock(
                stdout="123 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ),
            MagicMock(
                returncode=0, stdout=json.dumps([{"pid": 123, "window": 1, "tab": 2}])
            ),
        ]
        with patch.object(bridge.subprocess, "run", side_effect=outputs) as run:
            self.assertEqual(list(bridge.tabs()), [{"pid": 123, "window": 1, "tab": 2}])
        script = run.call_args.args[0][-1]
        self.assertNotIn("new URL(", script)
        self.assertIn("u.indexOf(", script)
