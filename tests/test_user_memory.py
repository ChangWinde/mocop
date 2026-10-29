import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

from mocop.service import StateStore
from mocop.user_memory import DailyUserMemory, parse_memory
from mocop.web import MonitorHttpServer

SAMPLE = "USER_MEMORY_V1\nTOTAL 1048576\n 1000 alice 1024\n1000 alice 2048\n1001 alice 512\n0 root 0\nEND\n"


class UserMemoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.hosts = ["gpu-01"]
        self.calls = []
        self.result = SimpleNamespace(returncode=0, stdout=SAMPLE, stderr="")
        config = SimpleNamespace(
            ssh_config=Path("/tmp/test-ssh-config"),
            local_host=None,
            connect_timeout_seconds=5,
            probe_timeout_seconds=10,
        )
        self.collector = DailyUserMemory(
            config,
            lambda: self.hosts,
            runner=self.runner,
            clock=lambda: self.now,
            monotonic=lambda: self.now,
        )
        self.addCleanup(self.collector.stop)

    def runner(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.result

    def test_accounts_are_grouped_by_uid_and_zero_rss_is_preserved(self):
        users = parse_memory(SAMPLE)["users"]
        self.assertEqual(
            [(u["uid"], u["rssKiB"], u["processes"]) for u in users],
            [(1000, 3072, 2), (1001, 512, 1), (0, 0, 1)],
        )

    def test_truncated_and_invalid_samples_fail_closed(self):
        for sample in (
            SAMPLE.replace("END\n", ""),
            SAMPLE.replace("TOTAL 1048576", "TOTAL 0"),
            SAMPLE.replace("alice 1024", "alice -1"),
            SAMPLE.replace("1000 alice", "bad alice"),
        ):
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                parse_memory(sample)

    def test_http_reads_do_not_collect_and_success_is_sampled_once_per_day(self):
        self.assertEqual(self.collector.snapshot()["servers"][0]["status"], "pending")
        self.assertEqual(self.calls, [])
        self.collector.collect_due()
        self.now += 86399
        self.collector.collect_due()
        self.assertEqual(len(self.calls), 1)
        self.now += 1
        self.collector.collect_due()
        self.assertEqual(len(self.calls), 2)
        command, options = self.calls[0]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(command[-4:], ["--", "gpu-01", "sh", "-s"])
        self.assertEqual(options["max_output_bytes"], 4 * 1024 * 1024)

    def test_failed_sample_preserves_previous_success_with_explicit_error(self):
        self.collector.collect_due()
        previous = self.collector.snapshot()["servers"][0]
        self.result = SimpleNamespace(
            returncode=255,
            stdout="",
            stderr="REMOTE HOST IDENTIFICATION HAS CHANGED! secret-address",
        )
        self.now += 86400
        self.collector.collect_due()
        row = self.collector.snapshot()["servers"][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["message"], "SSH host key changed")
        self.assertEqual(row["sampledAt"], previous["sampledAt"])
        self.assertEqual(row["users"], previous["users"])
        self.assertNotIn("secret-address", json.dumps(row))

    def test_removed_and_unsafe_hosts_are_not_collected_or_exposed(self):
        self.collector.collect_due()
        self.hosts = ["--evil", "gpu;id"]
        self.collector.collect_due()
        self.assertEqual(self.collector.snapshot()["servers"], [])
        self.assertEqual(len(self.calls), 1)

    def test_snapshot_is_detached_and_stop_prevents_new_processes(self):
        self.collector.collect_due()
        data = self.collector.snapshot()
        data["servers"][0]["users"].clear()
        self.assertEqual(len(self.collector.snapshot()["servers"][0]["users"]), 3)
        self.collector.stop()
        self.now += 86400
        self.collector.collect_due()
        self.assertEqual(len(self.calls), 1)

    def test_route_requires_bearer_and_rejects_queries_and_writes(self):
        state = StateStore(5)
        server = MonitorHttpServer(
            ("127.0.0.1", 0), state, access_token="T" * 43, user_memory=self.collector
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for method, path, token, expected in [
                ("GET", "/api/user-memory", False, 403),
                ("GET", "/api/user-memory", True, 200),
                ("HEAD", "/api/user-memory", True, 200),
                ("GET", "/api/user-memory?host=evil", True, 400),
                ("POST", "/api/user-memory", True, 405),
            ]:
                with self.subTest(method=method, path=path, token=token):
                    client = HTTPConnection(*server.server_address, timeout=3)
                    client.request(
                        method,
                        path,
                        headers={"Authorization": "Bearer " + "T" * 43}
                        if token
                        else {},
                    )
                    response = client.getresponse()
                    response.read()
                    self.assertEqual(response.status, expected)
                    client.close()
            self.assertEqual(self.calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
