import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mocop.directory_disk import DailyDirectoryDisk, parse_disk
from mocop.remote_script import (
    _DIRECTORY_DISK_PROGRAM,
    DIRECTORY_DISK_INVENTORY_SCRIPT,
    DIRECTORY_DISK_SCRIPT,
)


def sample(status="ok", size=1024):
    return json.dumps(
        {
            "protocol": "DIRECTORY_DISK_V1",
            "directories": [{"path": "/data/alice", "sizeKiB": size, "status": status}],
            "notes": [],
        }
    )


def filesystem(path, kind="ext4"):
    return {
        "mountPoint": path,
        "aliases": [path],
        "fsType": kind,
        "kind": "network" if kind == "cifs" else "local",
        "scanRoot": path != "/",
        "totalKiB": 1024,
        "usedKiB": 512,
        "availableKiB": 512,
        "percentUsed": 50,
    }


class DirectoryDiskTests(unittest.TestCase):
    def test_unknown_is_distinct_from_empty_and_malformed_samples_rejected(self):
        self.assertEqual(parse_disk(sample(size=0))["directories"][0]["sizeKiB"], 0)
        self.assertIsNone(
            parse_disk(sample("timeout", None))["directories"][0]["sizeKiB"]
        )
        for value in (
            sample("timeout", 12),
            sample(size=-1),
            sample(size=True),
            sample().replace("/data/alice", "/data/../alice"),
            "{}",
            sample()[:-1],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_disk(value)

    def test_independent_daily_schedule_and_failed_attempt_preserves_disk_data(self):
        now = [1000.0]
        result = SimpleNamespace(returncode=0, stdout=sample(), stderr="")
        calls = []

        def runner(command, **kwargs):
            calls.append(kwargs)
            return result

        config = SimpleNamespace(
            ssh_config=Path("/tmp/config"),
            local_host=None,
            connect_timeout_seconds=5,
            probe_timeout_seconds=10,
        )
        collector = DailyDirectoryDisk(
            config,
            lambda: ["gpu-01"],
            runner=runner,
            clock=lambda: now[0],
            monotonic=lambda: now[0],
        )
        self.addCleanup(collector.stop)
        self.assertEqual(collector.snapshot()["metric"], "disk")
        self.assertEqual(collector.snapshot()["servers"][0]["directories"], [])
        collector.collect_due()
        collector.collect_due()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["timeout_seconds"], 40)
        self.assertEqual(calls[0]["input_text"], DIRECTORY_DISK_INVENTORY_SCRIPT)
        self.assertEqual(calls[1]["timeout_seconds"], 1830)
        self.assertEqual(calls[1]["input_text"], DIRECTORY_DISK_SCRIPT)
        result.returncode = 255
        result.stderr = "Permission denied secret-location"
        now[0] += 86400
        collector.collect_due()
        row = collector.snapshot()["servers"][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["directories"][0]["sizeKiB"], 1024)
        self.assertNotIn("secret-location", row["message"])
        self.assertNotIn("users", row)

    def test_remote_protocol_keeps_paths_as_arguments_and_partial_failures_unknown(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ["alice", "bob", "slow", "$(touch INJECTED)", ".hidden"]:
                (root / name).mkdir()
            (root / "link").symlink_to(root / "alice", target_is_directory=True)
            namespace = {}
            exec(compile(_DIRECTORY_DISK_PROGRAM, "<remote-disk>", "exec"), namespace)
            namespace["FALLBACK_ROOTS"] = ()
            namespace["discover_mounts"] = lambda: ([filesystem(folder)], [])

            def du(command, **kwargs):
                self.assertEqual(
                    command[:7], ["du", "-P", "-x", "-k", "-s", "--null", "--"]
                )
                self.assertNotIn("shell", kwargs)
                name = Path(command[-1]).name
                if name == "slow":
                    raise subprocess.TimeoutExpired(command, 90)
                return SimpleNamespace(
                    returncode=1 if name == "bob" else 0, stdout=b"42\tname\0"
                )

            output = io.StringIO()
            real_isdir = os.path.isdir
            with (
                patch("subprocess.run", side_effect=du),
                patch("shutil.which", return_value=None),
                patch(
                    "os.path.isdir",
                    side_effect=lambda path: str(path) != "/root" and real_isdir(path),
                ),
                contextlib.redirect_stdout(output),
            ):
                namespace["main"](True)
            rows = {
                Path(row["path"]).name: row
                for row in parse_disk(output.getvalue())["directories"]
            }
            self.assertNotIn("link", rows)
            self.assertNotIn(
                "root", rows, "the fixture must not inspect the host's root home"
            )
            self.assertEqual(rows["alice"]["sizeKiB"], 42)
            self.assertEqual(rows["bob"]["status"], "unreadable")
            self.assertIsNone(rows["slow"]["sizeKiB"])
            self.assertIn(".hidden", rows)
            self.assertIn("$(touch INJECTED)", rows)

    def test_http_disk_route_is_authenticated_cache_only(self):
        import threading
        from http.client import HTTPConnection

        from mocop.service import StateStore
        from mocop.web import MonitorHttpServer

        source = SimpleNamespace(
            snapshot=lambda: {"metric": "disk", "servers": [], "intervalSeconds": 86400}
        )
        server = MonitorHttpServer(
            ("127.0.0.1", 0),
            StateStore(5),
            access_token="T" * 43,
            directory_disk=source,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for method, path, token, expected in [
                ("GET", "/api/directory-disk", False, 403),
                ("GET", "/api/directory-disk", True, 200),
                ("HEAD", "/api/directory-disk", True, 200),
                ("GET", "/api/directory-disk?path=/etc", True, 400),
                ("POST", "/api/directory-disk", True, 405),
            ]:
                with self.subTest(method=method, path=path):
                    client = HTTPConnection(*server.server_address, timeout=3)
                    client.request(
                        method,
                        path,
                        headers={"Authorization": "Bearer " + "T" * 43}
                        if token
                        else {},
                    )
                    response = client.getresponse()
                    body = response.read()
                    self.assertEqual(response.status, expected)
                    if method == "GET" and expected == 200:
                        self.assertEqual(json.loads(body)["metric"], "disk")
                    client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_every_host_inventory_is_published_before_slow_directory_scans(self):
        hosts = ["gpu-01", "gpu-02"]
        stages = []
        payload = json.loads(sample())
        payload["filesystems"] = [filesystem("/data")]
        payload["directories"][0]["mountPoint"] = "/data"
        config = SimpleNamespace(
            ssh_config=Path("/tmp/config"),
            local_host=None,
            connect_timeout_seconds=5,
            probe_timeout_seconds=10,
        )
        collector = None

        def runner(command, **kwargs):
            inventory = kwargs["input_text"] == DIRECTORY_DISK_INVENTORY_SCRIPT
            stages.append("inventory" if inventory else "scan")
            if not inventory:
                records = collector.snapshot()["servers"]
                self.assertTrue(all(r.get("filesystems") for r in records))
                self.assertTrue(all(r.get("inventoryAt") for r in records))
                self.assertTrue(all("_inventoryDeadline" not in r for r in records))
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        collector = DailyDirectoryDisk(config, lambda: hosts, runner=runner)
        self.addCleanup(collector.stop)
        collector.collect_due()
        self.assertEqual(stages, ["inventory", "inventory", "scan", "scan"])
        collector.collect_due()
        self.assertEqual(len(stages), 4, "both phases run only once per day")
