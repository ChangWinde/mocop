import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mocop.storage_analysis import PersonalStorageMonitor, analysis, validate_sample
from mocop.training_browser import merge_browser_observations
from mocop.training_insights import (
    capacity_projection,
    card_hours_projection,
    overview,
    utilization,
)
from mocop.zenith_monitor import TrainingMonitor


class TrainingInsightsTests(unittest.TestCase):
    def test_browser_capture_skips_unreadable_tabs_and_keeps_credentials_in_browser(
        self,
    ):
        from mocop import training_browser_capture as bridge

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            helper = state / "helper"
            helper.touch()
            os.utime(helper, (time.time() + 30, time.time() + 30))
            output = state / "metadata.json"
            payload = {
                "state": "done",
                "status": "ok",
                "jobs": [],
                "complete": True,
                "sampled_at": 1000,
            }
            with (
                patch.object(bridge.sys, "platform", "darwin"),
                patch.object(bridge, "STATE", state),
                patch.object(bridge, "HELPER", helper),
                patch.object(bridge, "OBSERVATIONS", output),
                patch.object(
                    bridge,
                    "tabs",
                    return_value=iter([{"tab": "unreadable"}, {"tab": "readable"}]),
                ),
                patch.object(
                    bridge,
                    "execute",
                    side_effect=[ValueError(), "started", json.dumps(payload)],
                ) as execute,
                patch.object(bridge.time, "sleep"),
            ):
                result = bridge.capture()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(execute.call_args_list[1].args[0]["tab"], "readable")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertIn("skipAuthRedirect:true", bridge.JAVASCRIPT)
            self.assertIn("scope:'mine'", bridge.JAVASCRIPT)

    def test_sync_request_time_does_not_extend_the_one_minute_interval(self):
        monitor = TrainingMonitor(start=False)
        clock = [100.0]

        def read(_):
            clock[0] += 10
            return {"jobs": [], "total": 0}

        with (
            patch("mocop.zenith_monitor.time.monotonic", side_effect=lambda: clock[0]),
            patch.object(monitor, "read", side_effect=read),
        ):
            monitor.collect()
        self.assertEqual(monitor.platform_collected_at, 100)
        self.assertEqual(monitor.list_collected_at, 100)

    def test_list_and_capacity_sync_every_minute(self):
        monitor = TrainingMonitor(start=False)
        monitor.jobs = [{"id": "a" * 25, "status": "FAILED", "created_at": 1}]
        monitor.total, monitor.complete = 1, True
        monitor.list_collected_at = time.monotonic() - 61
        monitor.platform_collected_at = time.monotonic() - 61
        calls = []

        def read(path):
            calls.append(path)
            return {"jobs": [], "total": 0}

        monitor.read = read
        monitor.collect()
        self.assertIn("/pt/api/ai/jobs?page=1&page_size=100", calls)
        self.assertIn("/pt/api/compute-offerings", calls)
        self.assertEqual(monitor.snapshot()["list_poll_interval_seconds"], 60)

    def test_capacity_states_remain_unknown_without_explicit_counters(self):
        cap = {"total_gpus": 64, "free_gpus": 16}
        offering = {
            "gpu_model": "GPU A",
            "availability_by_resource_zone": {
                "z": {"vendor": "cloud", "job_capacity": cap}
            },
        }
        row = capacity_projection({"offerings": [offering]})["rows"][0]
        self.assertIsNone(row["starting"])
        self.assertIsNone(row["running"])
        self.assertEqual(row["allocated"], 48)
        cap.update(starting_gpus=8, running_gpus=40)
        row = capacity_projection({"offerings": [offering]})["rows"][0]
        self.assertEqual((row["starting"], row["running"]), (8, 40))

    def test_active_card_totals_keep_starting_and_queue_separate(self):
        result = overview(
            [
                {"status": "RUNNING", "gpu_count": 8},
                {"status": "STARTING", "gpu_count": 4},
                {"status": "PROVISIONING", "gpu_count": 8},
                {"status": "QUEUED", "gpu_count": 2},
                {"status": "FAILED", "gpu_count": 64},
            ]
        )
        self.assertEqual(result["known_active_cards"], 22)
        self.assertEqual(result["known_starting_cards"], 12)
        self.assertEqual(result["known_queued_cards"], 2)

    def test_utilization_keeps_zero_and_expires_old_samples(self):
        now = 1000000
        job = {
            "metrics": {
                "source": "provider",
                "series": [
                    {
                        "metric": "GpuUsage",
                        "replica_id": "a",
                        "points": [{"ts": now, "value": 0}],
                    },
                    {
                        "metric": "GpuUsage",
                        "replica_id": "b",
                        "points": [{"ts": now, "value": 100}],
                    },
                    {
                        "metric": "GpuUsage",
                        "replica_id": "c",
                        "points": [{"ts": now - 180001, "value": 30}],
                    },
                ],
            }
        }
        result = utilization(job, now)
        self.assertEqual(result["pct"], 50)
        self.assertTrue(result["partial"])
        self.assertIsNone(utilization(job, now + 180001)["pct"])
        job["metrics"]["series"][0]["points"].append({"ts": now + 1, "value": None})
        self.assertEqual(utilization(job, now)["observed"], 1)

    def test_only_running_jobs_contribute_requested_cards(self):
        result = overview(
            [
                {"status": "RUNNING", "gpu_count": 8, "gpu_model": "GPU A"},
                {"status": "RUNNING", "gpu_count": None},
                {"status": "QUEUED", "gpu_count": 64},
                {"status": "FAILED", "gpu_count": 64},
            ]
        )
        self.assertEqual(result["known_running_cards"], 8)
        self.assertEqual(result["unknown_card_jobs"], 1)
        self.assertEqual(result["models"], {"GPU A": 8})

    def test_capacity_repeated_offering_sizes_are_not_added(self):
        cap = {
            "total_gpus": 64,
            "free_gpus": 16,
            "selection": "best_candidate",
            "observed_at": 1,
        }
        offering = {
            "gpu_model": "GPU A",
            "availability_by_resource_zone": {
                "z": {"vendor": "cloud", "job_capacity": cap}
            },
        }
        result = capacity_projection({"offerings": [offering, copy.deepcopy(offering)]})
        known = [r for r in result["rows"] if r["total"] is not None]
        self.assertEqual(len(known), 1)
        self.assertFalse(result["additive"])
        self.assertEqual(known[0]["free"], 16)

    def test_bill_does_not_expose_full_account_payload(self):
        result = card_hours_projection(
            {
                "personal": {
                    "used_card_hours": 10,
                    "raw_used_card_hours": 25,
                    "password": "private",
                    "remaining_card_hours": 0,
                }
            }
        )
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(result["personal"]["remaining_card_hours"], 0)

    def test_browser_projection_requires_identity_private_file_and_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observation.json"
            jobs = [{"vendor": "baidu", "job_id": "my-job", "gpu_count": None}]
            payload = {
                "status": "ok",
                "sampled_at": 1000,
                "jobs": [
                    {
                        "vendor": "baidu",
                        "job_id": "my-job",
                        "resource_requests": [
                            {"gpu_type": "GPU A", "gpu_count": 4, "replicas": 2}
                        ],
                    }
                ],
            }
            path.write_text(json.dumps(payload))
            path.chmod(0o600)
            result = merge_browser_observations(jobs, path=path, now=2000)
            self.assertEqual(result[0]["gpu_count"], 8)
            self.assertIsNone(jobs[0]["gpu_count"])
            expired = merge_browser_observations(result, path=path, now=700000)
            self.assertIsNone(expired[0]["gpu_count"])
            path.chmod(0o644)
            self.assertIsNone(
                merge_browser_observations(jobs, path=path, now=2000)[0]["gpu_count"]
            )

    def test_rotation_includes_active_jobs_beyond_previous_top_twenty(self):
        monitor = TrainingMonitor(start=False)
        now = time.time() * 1000
        monitor.jobs = [
            {
                "id": str(i).zfill(25),
                "status": "RUNNING",
                "created_at": i,
                "capabilities": {"replicas": False},
                "vendor": "baidu",
                "job_id": str(i),
            }
            for i in range(24)
        ]
        monitor.total = 24
        monitor.complete = True
        monitor.list_collected_at = time.monotonic()
        monitor.platform_collected_at = time.monotonic()
        observed = []

        def observations(job):
            observed.append(job["id"])
            return {
                "id": job["id"],
                "metrics": {
                    "fetched_at": now + len(observed),
                    "source": "provider",
                    "series": [],
                },
                "metrics_status": 200,
            }

        with patch.object(monitor, "observations", side_effect=observations):
            for _ in range(3):
                monitor.collect()
        self.assertEqual(len(set(observed)), 24)


class StorageTests(unittest.TestCase):
    def test_private_exclusions_are_literal_names_and_reach_du_as_arguments(self):
        from mocop.storage_probe import scan, valid_exclusions

        self.assertTrue(valid_exclusions(["excluded-user", "cache_01"]))
        for value in ("user", ["../user"], ["*"], [".."], ["user\nsecret"], ["a"] * 33):
            self.assertFalse(valid_exclusions(value))
        result = SimpleNamespace(returncode=0, stdout=b"12\t/data/alice\0")
        with patch("mocop.storage_probe.subprocess.run", return_value=result) as run:
            self.assertEqual(
                scan("/data/alice", 1, excluded_names=["excluded-user"])["bytes"], 12
            )
        self.assertIn("--exclude=excluded-user", run.call_args.args[0])

    def test_changed_exclusions_invalidate_directory_totals_and_growth(self):
        sample = self.sample()
        sample.update(epoch=2000, excludeNames=["excluded-user"])
        old = copy.deepcopy(sample)
        old.update(epoch=1000, excludeNames=[])
        self.assertFalse(
            PersonalStorageMonitor.matches_policy(
                old, {"roots": ["/data/alice"], "excludeNames": ["excluded-user"]}
            )
        )
        self.assertIsNone(analysis(sample, [old])["directories"][0]["changeBytes"])

    def test_nas_parent_includes_nested_mounts_without_excluding_each_mount_itself(
        self,
    ):
        from mocop import storage_probe

        with tempfile.TemporaryDirectory() as directory:
            root = str(Path(directory) / "nas")
            shares = [str(Path(root) / n) for n in ("data", "archive")]
            for path in shares:
                Path(path).mkdir(parents=True)
            mounts = [
                {"target": path, "fstype": "cifs" if i else "ext4", "source": str(i)}
                for i, path in enumerate([directory] + shares)
            ]

            def scan(path, timeout, **kwargs):
                self.assertNotIn(path, kwargs["excluded"])
                return {"path": path, "bytes": 10, "status": "ok", "children": []}

            output = io.StringIO()
            with (
                patch.object(storage_probe, "mount_inventory", return_value=mounts),
                patch.object(storage_probe, "scan", side_effect=scan),
                patch("sys.stdin", io.StringIO(json.dumps({"roots": [root]}))),
                contextlib.redirect_stdout(output),
            ):
                storage_probe.main()
            result = validate_sample(json.loads(output.getvalue()), [root])
            self.assertEqual(result["directories"][0]["bytes"], 30)
            self.assertEqual(
                {c["path"] for c in result["directories"][0]["children"]}, set(shares)
            )
            self.assertEqual(len(result["filesystems"]), 3)

    def test_timeout_keeps_completed_children_and_never_invents_root_total(self):
        from mocop import storage_probe

        error = subprocess.TimeoutExpired("du", 1, output=b"12\t/data/alice/complete\0")
        with patch.object(storage_probe.subprocess, "run", side_effect=error):
            result = storage_probe.scan("/data/alice", 1)
        self.assertIsNone(result["bytes"])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(
            result["children"], [{"path": "/data/alice/complete", "bytes": 12}]
        )

    def test_virtual_object_storage_has_no_disk_pressure_or_free_space_forecast(self):
        sample = self.sample()
        sample["filesystems"][0]["capacityVirtual"] = True
        sample["epoch"] = 1000
        result = analysis(sample, [])
        self.assertIsNone(result["filesystems"][0]["percentUsed"])
        self.assertIsNone(result["filesystems"][0]["daysUntilFull"])

    def test_new_policy_displays_new_roots_instead_of_old_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "policy.json"
            policy.write_text(
                json.dumps({"hosts": {"gpu-01": {"roots": ["/new/alice"]}}})
            )
            policy.chmod(0o600)
            monitor = PersonalStorageMonitor(
                SimpleNamespace(),
                lambda: ["gpu-01"],
                policy_path=policy,
                history_path=Path(directory) / "h",
            )
            sample = self.sample()
            sample.update(host="gpu-01", knownBytes=4096, status="ok")
            monitor.records["gpu-01"] = sample
            row = monitor.snapshot()["servers"][0]
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["knownBytes"])
            self.assertEqual([r["path"] for r in row["directories"]], ["/new/alice"])

    def sample(self):
        return {
            "protocol": "PERSONAL_STORAGE_V1",
            "directories": [
                {
                    "path": "/data/alice",
                    "bytes": 4096,
                    "status": "ok",
                    "children": [{"path": "/data/alice/data", "bytes": 4096}],
                }
            ],
            "filesystems": [
                {
                    "mountPoint": "/data",
                    "key": "device",
                    "kind": "local",
                    "fsType": "ext4",
                    "totalBytes": 100 * 1024**3,
                    "usedBytes": 80 * 1024**3,
                    "availableBytes": 20 * 1024**3,
                }
            ],
        }

    def test_live_capacity_overlay_keeps_personal_sample_and_ignores_stale_hosts(self):
        from mocop.storage_analysis import merge_live_capacity

        sample = self.sample()
        sample.update(host="gpu-01", sampledAt="old", knownBytes=4096)
        storage = {"servers": [sample]}
        live = {
            "host": "gpu-01",
            "status": "online",
            "stale": False,
            "lastSuccessAt": "new",
            "system": {
                "disks": [
                    {
                        "mountpoint": "/data",
                        "total_mib": 100,
                        "used_mib": 98,
                        "available_mib": 2,
                        "used_pct": 98,
                    }
                ]
            },
        }
        result = merge_live_capacity(storage, {"servers": [live]})
        self.assertEqual(result["servers"][0]["knownBytes"], 4096)
        self.assertEqual(result["servers"][0]["sampledAt"], "old")
        self.assertEqual(
            result["servers"][0]["filesystems"][0]["availableBytes"], 2 * 1024**2
        )
        self.assertEqual(
            result["servers"][0]["filesystems"][0]["capacitySampledAt"], "new"
        )
        live["stale"] = True
        self.assertEqual(merge_live_capacity(storage, {"servers": [live]}), storage)

    def test_invalid_paths_and_unknown_totals_are_rejected(self):
        sample = self.sample()
        validate_sample(sample, ["/data/alice"])
        for path in ("/data/bob", "/data/alice/../bob"):
            invalid = copy.deepcopy(sample)
            invalid["directories"][0]["children"][0]["path"] = path
            with self.assertRaises(ValueError):
                validate_sample(invalid, ["/data/alice"])
        sample["directories"][0]["status"] = "timeout"
        with self.assertRaises(ValueError):
            validate_sample(sample, ["/data/alice"])

    def test_partial_scans_do_not_become_zero_and_trends_require_history(self):
        sample = self.sample()
        sample["epoch"] = 100000
        initial = analysis(sample, [])
        self.assertIsNone(initial["directories"][0]["changeBytes"])
        self.assertIsNone(initial["filesystems"][0]["daysUntilFull"])
        history = []
        for epoch in (1000, 25000):
            old = copy.deepcopy(sample)
            old["epoch"] = epoch
            old["filesystems"][0]["availableBytes"] = 30 * 1024**3
            history.append(old)
        result = analysis(sample, history)
        self.assertGreater(result["filesystems"][0]["daysUntilFull"], 0)
        sample["directories"][0].update(status="timeout", bytes=None)
        result = analysis(sample, history)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["directories"][0]["changeBytes"])

    def test_policy_is_data_not_remote_shell_and_history_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "policy.json"
            history = Path(directory) / "history.json"
            root = "/data/alice/$(touch innocent);name"
            policy.write_text(json.dumps({"hosts": {"gpu-01": {"roots": [root]}}}))
            policy.chmod(0o600)
            sample = self.sample()
            sample["directories"][0].update(path=root, children=[])
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(returncode=0, stdout=json.dumps(sample))

            config = SimpleNamespace(ssh_config=Path("/tmp/fake-ssh"))
            monitor = PersonalStorageMonitor(
                config,
                lambda: ["gpu-01"],
                policy_path=policy,
                history_path=history,
                runner=runner,
            )
            monitor.collect(next(iter(monitor.policy().items())))
            command, options = calls[0]
            self.assertNotIn(root, " ".join(command))
            self.assertEqual(json.loads(options["input_text"])["roots"], [root])
            self.assertEqual(os.stat(history).st_mode & 0o777, 0o600)
            restored = PersonalStorageMonitor(
                config,
                lambda: ["gpu-01"],
                policy_path=policy,
                history_path=history,
                runner=runner,
            )
            self.assertEqual(restored.snapshot()["servers"][0]["knownBytes"], 4096)
            restored.hosts = lambda: []
            self.assertEqual(restored.snapshot()["servers"], [])

    def test_other_user_and_overlapping_roots_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "policy.json"
            policy.chmod if policy.exists() else None
            monitor = PersonalStorageMonitor(
                SimpleNamespace(),
                lambda: ["gpu-01"],
                policy_path=policy,
                history_path=Path(directory) / "h",
            )
            for roots in (
                ["/home/excluded-user"],
                ["/data/alice", "/data/alice/sub"],
                ["/"],
            ):
                policy.write_text(
                    json.dumps(
                        {
                            "hosts": {
                                "gpu-01": {
                                    "roots": roots,
                                    "excludeNames": ["excluded-user"],
                                }
                            }
                        }
                    )
                )
                policy.chmod(0o600)
                with self.assertRaises(ValueError):
                    monitor.policy()
