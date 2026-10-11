from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from mocop.config import ConfigError
from mocop.config_loader import load_config
from mocop.training_config import TrainingJobsConfig, parse_training_config
from mocop.training_jobs import TrainingMonitor
from mocop.training_projection import (
    ReadError,
    number,
    project_job,
    project_metrics,
    project_replicas,
    redact_text,
)
from tests.test_config import valid_config


def job(index=1, status="RUNNING", **values):
    return {
        "id": f"{index:025d}",
        "vendor": "baidu",
        "name": f"Example training {index}",
        "job_id": f"provider-{index}",
        "status": status,
        "created_at": 1_900_000_000_000 - index,
        "capabilities": {"replicas": True, "logs": True},
        **values,
    }


def metrics():
    return {
        "source": "provider",
        "fetched_at": time.time() * 1000,
        "series": [
            {
                "metric": "GpuUsage",
                "replica_id": "worker-0",
                "points": [{"ts": 1_900_000_000_000, "value": 0}],
            }
        ],
    }


class TrainingConfigTests(unittest.TestCase):
    def test_default_is_disabled_without_io(self):
        config = parse_training_config({}, Path("/example"))
        monitor = TrainingMonitor(config)
        with patch.object(monitor.client, "read") as read:
            monitor.start()
            monitor.close()
        read.assert_not_called()
        self.assertIsNone(monitor.thread)

    def test_loader_resolves_private_path_without_reading_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            value = valid_config()
            value["training_jobs"] = {
                "enabled": True,
                "origin": "https://training.example.invalid/",
                "token_file": "private/training-token",
                "poll_interval_seconds": 60,
            }
            path.write_text(json.dumps(value))
            config = load_config(path)
            self.assertEqual(
                config.training_jobs.origin, "https://training.example.invalid"
            )
            self.assertEqual(
                config.training_jobs.token_file,
                Path(folder).resolve() / "private/training-token",
            )
            self.assertEqual(config.training_jobs.poll_interval_seconds, 60)

    def test_rejects_invalid_schema_and_unsafe_origins(self):
        base = {
            "enabled": True,
            "origin": "https://training.example.invalid",
            "token_file": "token",
        }
        invalid = [
            None,
            [],
            {"unknown": 1},
            {"enabled": 1},
            {"allow_http": "yes"},
            base | {"token_file": ""},
        ]
        invalid += [
            base | {"poll_interval_seconds": value}
            for value in [True, 0, 14, 301, 30.5, "30"]
        ]
        invalid += [
            base | {"origin": value}
            for value in [
                7,
                "",
                "http://training.example.invalid",
                "https://user:secret@example.invalid",
                "https://example.invalid/path",
                "https://example.invalid?query",
                "https://example.invalid#fragment",
                "https://example.invalid:0",
                "https://example.invalid:65536",
                "https://[invalid",
                "https:// example.invalid",
            ]
        ]
        invalid += [{"enabled": True}, base | {"token_file": "bad\x00path"}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_training_config(value, Path("/example"))
        with self.assertRaises(ConfigError):
            parse_training_config(base, Path("/example"), "none")

    def test_plain_http_requires_explicit_opt_in(self):
        config = parse_training_config(
            {"origin": "http://training.example.invalid", "allow_http": True},
            Path("/example"),
        )
        self.assertTrue(config.allow_http)
        self.assertFalse(config.enabled)


class TrainingProjectionTests(unittest.TestCase):
    def test_unknown_numbers_do_not_become_zero(self):
        for value in [None, True, "0", float("nan"), float("inf"), 10**1000]:
            self.assertIsNone(number(value))
        self.assertEqual(number(0), 0)
        self.assertEqual(number(0.5), 0.5)

    def test_projection_drops_untrusted_fields_and_redacts_recognized_secrets(self):
        token = "znt_" + "x" * 48
        value = project_job(
            job(name=f"example {token}", command="private command", created_at=True)
        )
        self.assertNotIn("command", value)
        self.assertNotIn(token, json.dumps(value))
        self.assertIsNone(value["created_at"])
        self.assertIsNone(redact_text(None))
        self.assertEqual(redact_text("password=secret"), "password=[REDACTED]")
        self.assertLessEqual(len(redact_text("sk-a " * 1000, 4000)), 4000)
        self.assertEqual(redact_text("bad\ud800text"), "bad?text")
        self.assertNotIn(
            "private",
            redact_text("-----BEGIN PRIVATE KEY-----private-----END PRIVATE KEY-----"),
        )
        for value in [
            None,
            job(id="../outside"),
            job(vendor="unknown"),
            job(vendor=[]),
            job(capabilities=[1]),
            job(capabilities=0),
        ]:
            with self.assertRaises(ReadError):
                project_job(value)

    def test_replica_count_is_not_a_gpu_count_or_completion_signal(self):
        value = project_replicas({"replicas": [], "total": None})
        self.assertIsNone(value["total"])
        self.assertFalse(value["complete"])
        value = project_replicas(
            {"replicas": [{"name": "worker", "exit_code": 0}], "total": 2}
        )
        self.assertEqual(value["replicas"][0]["exit_code"], 0)
        self.assertFalse(value["complete"])
        self.assertNotIn("gpu_count", value)
        for value in [
            {},
            {"replicas": [False]},
            {"replicas": [], "total": -1},
            {"replicas": [], "total": 1.5},
        ]:
            with self.assertRaises(ReadError):
                project_replicas(value)
        self.assertEqual(
            len(project_replicas({"replicas": [{}] * 110, "total": 110})["replicas"]),
            100,
        )

    def test_provider_metrics_keep_native_units_and_missing_samples(self):
        value = metrics()
        value["series"][0]["points"] += [{"ts": 1_900_000_001_000, "value": None}]
        result = project_metrics(value)
        self.assertEqual(result["series"][0]["unit"], "provider_native")
        self.assertEqual(result["series"][0]["points"][0]["value"], 0)
        self.assertIsNone(result["series"][0]["points"][1]["value"])
        for series in [None, [False], [{"metric": "GpuUsage", "points": None}]]:
            with self.assertRaises(ReadError):
                project_metrics({"source": "provider", "series": series})
        self.assertEqual(
            project_metrics({"source": "provider", "series": [{"metric": "future"}]})[
                "series"
            ],
            [],
        )

    def test_stored_metrics_bound_samples_and_parse_gpu_json(self):
        self.assertEqual(
            project_metrics({"source": "provider", "series": [{"metric": []}]})[
                "series"
            ],
            [],
        )
        result = project_metrics(
            {
                "samples": [
                    {"ts": 12, "cpu_pct": 0, "gpus_json": '[{"index":0,"util_pct":0}]'}
                ]
                * 130,
                "replicas": ["worker-0"],
                "collection_enabled": True,
            }
        )
        self.assertEqual(len(result["samples"]), 120)
        self.assertEqual(result["samples"][0]["gpus"][0]["util_pct"], 0)
        for raw in ["invalid", "{}", '[null, {"index":1}]']:
            self.assertIsInstance(
                project_metrics({"samples": [False, {"gpus_json": raw}]})["samples"][0][
                    "gpus"
                ],
                list,
            )
        for value in [{}, {"samples": [], "replicas": None}]:
            with self.assertRaises(ReadError):
                project_metrics(value)


class TrainingMonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = TrainingMonitor(replace(TrainingJobsConfig(), enabled=True))
        self.addCleanup(self.monitor.close)

    def read(self, path):
        if "?page=" in path:
            return {"jobs": [job()], "total": 1}
        if path.endswith("/replicas?page=1&page_size=100"):
            return {"replicas": [], "total": 0}
        if "/metrics?" in path:
            return metrics()
        if "/logs?" in path:
            return {
                "lines": [{"body": "training step", "source": "worker-0"}],
                "returned": 1,
            }
        return {"job": job()}

    def test_pagination_includes_old_active_jobs_and_deduplicates(self):
        def read(path):
            if "?page=1&" in path:
                return {
                    "jobs": [job(i, "SUCCEEDED") for i in range(1, 101)],
                    "total": 101,
                }
            if "?page=2&" in path:
                return {"jobs": [job(100, "SUCCEEDED"), job(101)], "total": 101}
            return self.read(path)

        with patch.object(self.monitor.client, "read", side_effect=read):
            self.monitor.collect()
        snapshot = self.monitor.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(len(snapshot["jobs"]), 101)
        self.assertEqual(snapshot["jobs"][-1]["metrics_status"], 200)
        snapshot["jobs"].clear()
        self.assertEqual(len(self.monitor.snapshot()["jobs"]), 101)

    def test_collection_limits_and_incomplete_list_are_explicit(self):
        with patch.object(
            self.monitor.client,
            "read",
            return_value={"jobs": [job(1, "FAILED")], "total": 50},
        ):
            self.monitor.collect()
        self.assertFalse(self.monitor.snapshot()["complete"])
        self.assertEqual(self.monitor.total, 50)
        for total in [-1, True, 1.5, None]:
            with (
                patch.object(
                    self.monitor.client,
                    "read",
                    return_value={"jobs": [], "total": total},
                ),
                self.assertRaises(ReadError),
            ):
                self.monitor.collect()
        with patch.object(
            self.monitor.client,
            "read",
            return_value={"jobs": [job(1, "FAILED")] * 100, "total": 2500},
        ) as read:
            self.monitor.collect()
        self.assertEqual(read.call_count, 20)
        self.assertFalse(self.monitor.complete)

    def test_diagnostics_are_bounded_and_partial_failure_preserves_last_sample(self):
        with patch.object(self.monitor.client, "read", side_effect=self.read):
            self.monitor.collect()
        with patch.object(
            self.monitor.client,
            "read",
            side_effect=lambda path: self.read(path)
            if "?page=" in path
            else (_ for _ in ()).throw(ReadError(429)),
        ):
            self.monitor.collect()
        self.assertEqual(self.monitor.jobs[0]["metrics_status"], 429)
        self.assertIn("metrics", self.monitor.jobs[0])
        rows = [project_job(job(i)) for i in range(1, 26)]
        with (
            patch.object(
                self.monitor.client, "read", return_value={"jobs": rows, "total": 25}
            ),
            patch.object(
                self.monitor, "observations", side_effect=lambda row: {"id": row["id"]}
            ) as observed,
        ):
            self.monitor.collect()
        self.assertEqual(observed.call_count, 20)

    def test_detail_is_owner_scoped_revalidates_and_discards_revoked_cache(self):
        with patch.object(self.monitor.client, "read", side_effect=self.read) as read:
            self.monitor.collect()
            detail = self.monitor.detail(job()["id"])
            self.assertEqual(detail["logs"]["lines"][0]["body"], "training step")
            before = read.call_count
            self.monitor.detail(job()["id"])
            self.assertEqual(read.call_count - before, 1)
            with self.assertRaises(ReadError) as error:
                self.monitor.detail(job(2)["id"])
            self.assertEqual(error.exception.status, 404)
            self.assertEqual(read.call_count - before, 1)
        with (
            patch.object(self.monitor.client, "read", side_effect=ReadError(403)),
            self.assertRaises(ReadError),
        ):
            self.monitor.detail(job()["id"])
        self.assertFalse(self.monitor.details)
        with self.assertRaises(ReadError) as error:
            self.monitor.detail("../outside")
        self.assertEqual(error.exception.status, 400)

    def test_capability_loss_removes_cached_logs(self):
        with patch.object(self.monitor.client, "read", side_effect=self.read):
            self.monitor.collect()
            self.monitor.detail(job()["id"])
        with patch.object(
            self.monitor.client, "read", return_value={"job": job(capabilities={})}
        ):
            detail = self.monitor.detail(job()["id"])
        self.assertNotIn("logs", detail)
        self.assertEqual(detail["logs_status"], 501)

    def test_detail_rejects_mismatched_identity_and_concurrent_requests(self):
        self.monitor.jobs = [project_job(job())]
        with (
            patch.object(self.monitor.client, "read", return_value={"job": job(2)}),
            self.assertRaises(ReadError) as error,
        ):
            self.monitor.detail(job()["id"])
        self.assertEqual(error.exception.status, 502)
        self.monitor.detail_lock.acquire()
        try:
            with self.assertRaises(ReadError) as error:
                self.monitor.detail(job()["id"])
            self.assertEqual(error.exception.status, 429)
        finally:
            self.monitor.detail_lock.release()

    def test_detail_bounds_logs_and_clears_failed_diagnostics(self):
        token = "znt_" + "x" * 48

        def read(path):
            if "/logs?" in path:
                return {
                    "lines": [{"body": token + "a" * 5000}] * 110,
                    "truncated": True,
                    "pending": True,
                }
            return self.read(path)

        self.monitor.jobs = [project_job(job())]
        with patch.object(self.monitor.client, "read", side_effect=read):
            detail = self.monitor.detail(job()["id"])
        self.assertEqual(len(detail["logs"]["lines"]), 100)
        self.assertLessEqual(len(detail["logs"]["lines"][0]["body"]), 4000)
        self.assertNotIn(token, json.dumps(detail))
        self.monitor.details.clear()
        with patch.object(
            self.monitor.client,
            "read",
            side_effect=lambda path: {"job": job()}
            if "/by-id/" in path and "?" not in path
            else {},
        ):
            detail = self.monitor.detail(job()["id"])
        self.assertEqual(detail["logs_status"], 502)

    def test_worker_retry_retains_network_cache_but_clears_auth_cache(self):
        for failure in [
            ReadError(503),
            ReadError(401),
            ReadError(403, 90),
            RuntimeError("untrusted error"),
        ]:
            self.monitor.jobs = [project_job(job())]
            self.monitor.details["example"] = (0, {})
            self.monitor.stop_event = Mock()
            self.monitor.stop_event.is_set.side_effect = [False, True]
            with patch.object(self.monitor, "collect", side_effect=failure):
                self.monitor.run()
            snapshot = self.monitor.snapshot()
            self.assertTrue(snapshot["stale"])
            if isinstance(failure, ReadError) and failure.status in (401, 403):
                self.assertEqual(snapshot["jobs"], [])
                self.assertFalse(self.monitor.details)
            else:
                self.assertEqual(len(snapshot["jobs"]), 1)
            self.monitor.stop_event.wait.assert_called_once()
        self.monitor.stop_event = Mock()

    def test_start_is_idempotent_and_close_stops_worker(self):
        with patch.object(
            self.monitor, "collect", side_effect=self.monitor.stop_event.set
        ) as collect:
            self.monitor.start()
            self.monitor.start()
            self.monitor.close()
        collect.assert_called_once()
        self.assertFalse(self.monitor.thread.is_alive())

    def test_unsupported_replicas_and_cancelled_collection(self):
        with patch.object(self.monitor.client, "read", return_value=metrics()):
            result = self.monitor.observations(project_job(job(capabilities={})))
        self.assertEqual(result["replicas_status"], 501)
        self.monitor.stop_event.set()
        with patch.object(self.monitor.client, "read") as read:
            self.monitor.collect()
            self.assertEqual(
                self.monitor.observations(project_job(job())), {"id": job()["id"]}
            )
        read.assert_not_called()
