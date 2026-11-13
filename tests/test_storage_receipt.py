import contextlib
import errno
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mocop import storage_probe


class StorageReceiptTests(unittest.TestCase):
    def test_minute_polls_preserve_measurement_age_and_do_not_fill_history(self):
        from mocop.storage_analysis import PersonalStorageMonitor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"
            settings = {
                "roots": ["/data/alice"],
                "measurementFile": "/private/count.json",
            }
            policy.write_text(json.dumps({"hosts": {"gpu-01": settings}}))
            policy.chmod(0o600)
            finished = time.time() - 40000
            sample = {
                "protocol": "PERSONAL_STORAGE_V1",
                "sourceEpoch": finished,
                "directories": [
                    {
                        "path": "/data/alice",
                        "bytes": 123,
                        "status": "ok",
                        "children": [],
                        "sampleEpoch": finished,
                    }
                ],
                "filesystems": [],
            }
            calls = []

            def runner(*args, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(returncode=0, stdout=json.dumps(sample))

            monitor = PersonalStorageMonitor(
                SimpleNamespace(ssh_config=root / "ssh"),
                lambda: ["gpu-01"],
                policy_path=policy,
                history_path=root / "history",
                runner=runner,
            )
            item = next(iter(monitor.policy().items()))
            for _ in range(3):
                monitor.collect(item)
            self.assertEqual(len(monitor.histories["gpu-01"]), 1)
            row = monitor.snapshot()["servers"][0]
            self.assertTrue(row["stale"])
            self.assertTrue(row["directories"][0]["stale"])
            self.assertEqual(row["sourceEpoch"], finished)
            self.assertEqual(calls[-1]["timeout_seconds"], 30)
            self.assertLessEqual(monitor.deadlines["gpu-01"] - time.monotonic(), 60)
            sample["measurementError"] = "failed to read"
            monitor.collect(item)
            row = monitor.snapshot()["servers"][0]
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["directories"][0]["bytes"], 123)

    def receipt(self, root, **changes):
        now = time.time()
        return {
            "complete": True,
            "started_at": now - 4200,
            "finished_at": now - 100,
            "root_allocated_bytes": {root: 123456},
            "top_directories": [{"path": root + "/project", "allocated_bytes": 10000}],
            "archive_changed_during_measurement": True,
            **changes,
        }

    def test_complete_receipt_replaces_timed_out_scan_without_launching_du(self):
        with tempfile.TemporaryDirectory() as directory:
            root = str(Path(directory) / "pool")
            Path(root).mkdir()
            path = Path(directory) / "receipt.json"
            data = self.receipt(root)
            path.write_text(json.dumps(data))
            path.chmod(0o600)
            output = io.StringIO()
            with (
                patch.object(
                    storage_probe,
                    "mount_inventory",
                    return_value=[
                        {"target": directory, "fstype": "ext4", "source": "disk"}
                    ],
                ),
                patch.object(storage_probe, "scan") as scan,
                patch(
                    "sys.stdin",
                    io.StringIO(
                        json.dumps({"roots": [root], "measurementFile": str(path)})
                    ),
                ),
                contextlib.redirect_stdout(output),
            ):
                storage_probe.main()
            result = json.loads(output.getvalue())
            scan.assert_not_called()
            row = result["directories"][0]
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["bytes"], 123456)
            self.assertEqual(row["sampleEpoch"], data["finished_at"])
            self.assertEqual(row["scanDurationSeconds"], 4100)
            self.assertTrue(row["liveChanges"])

    def test_broken_mount_is_not_a_missing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "bucket")
            real_stat = os.stat

            def remote_stat(path, *args, **kwargs):
                if path == target:
                    raise OSError(errno.ENOTCONN, "disconnected FUSE")
                return real_stat(path, *args, **kwargs)

            output = io.StringIO()
            with (
                patch.object(storage_probe.os, "stat", side_effect=remote_stat),
                patch.object(storage_probe, "mount_inventory", return_value=[]),
                patch("sys.stdin", io.StringIO(json.dumps({"roots": [target]}))),
                contextlib.redirect_stdout(output),
            ):
                storage_probe.main()
            row = json.loads(output.getvalue())["directories"][0]
            self.assertEqual(row["status"], "mount_unavailable")
            self.assertEqual(row["errorCode"], "ENOTCONN")
            self.assertIsNone(row["bytes"])

    def test_private_receipt_rejects_incomplete_malformed_and_future_totals(self):
        from mocop.storage_receipt import read_measurement

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            root = "/data/alice"
            for changes in (
                {"complete": False},
                {"complete": 1},
                {"root_allocated_bytes": {root: True}},
                {"root_allocated_bytes": {root: -1}},
                {"finished_at": time.time() + 3600},
                {"started_at": time.time() + 3600},
            ):
                data = self.receipt(root, **changes)
                path.write_text(json.dumps(data))
                path.chmod(0o600)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    read_measurement(str(path))
            path.write_text(json.dumps(self.receipt(root)))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_measurement(str(path))
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises((OSError, ValueError)):
                read_measurement(str(link))

    def test_receipt_scope_is_exact_not_a_parent_total_or_top_children_sum(self):
        from mocop.storage_receipt import measurement_row

        data = self.receipt("/shared/alice")
        row = measurement_row(data, "/shared", "/shared/alice")
        self.assertEqual(row["bytes"], 123456)
        self.assertEqual(row["path"], "/shared")
        self.assertIsNone(measurement_row(data, "/shared", "/shared/bob")["bytes"])
        data["top_directories"].append(
            {"path": "/shared/alice/../bob", "allocated_bytes": 99}
        )
        self.assertNotIn(
            "/shared/alice/../bob",
            [
                c["path"]
                for c in measurement_row(data, "/shared", "/shared/alice")["children"]
            ],
        )

    def test_measurement_source_change_invalidates_cached_scope(self):
        from mocop.storage_analysis import PersonalStorageMonitor

        record = {
            "directories": [{"path": "/data/alice"}],
            "measurementFile": "/old/result",
        }
        policy = {"roots": ["/data/alice"], "measurementFile": "/new/result"}
        self.assertFalse(PersonalStorageMonitor.matches_policy(record, policy))
