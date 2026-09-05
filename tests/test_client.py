from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mocop import client
from mocop.__main__ import main
from mocop.api_manifest import API_ROUTES
from mocop.config_loader import load_config
from mocop.service import StateStore
from mocop.web import MonitorHttpServer

TOKEN = "C" * 43


class ApiClientTests(unittest.TestCase):
    """`mocop api` against a real loopback server: address and capability come
    from the configuration directory, bodies pass through untouched."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = StateStore(5)
        self.server = MonitorHttpServer(
            ("127.0.0.1", 0), self.state, access_token=TOKEN
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "ssh_config": str(self.root / "ssh-config"),
                    "auto_discover": False,
                    "hosts": ["gpu-1"],
                    "exclude_hosts": [],
                    "poll_interval_seconds": 5,
                    "probe_timeout_seconds": 12,
                    "connect_timeout_seconds": 5,
                    "max_workers": 2,
                    "listen_host": "127.0.0.1",
                    "listen_port": self.server.server_port,
                }
            ),
            encoding="utf-8",
        )
        token_path = self.root / "access-token"
        token_path.write_text(TOKEN + "\n", encoding="ascii")
        os.chmod(token_path, 0o600)

    def _stop_server(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)

    def run_api(self, *argv: str) -> tuple[int, bytes]:
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with redirect_stdout(stdout):
            code = main(["api", *argv, "--config", str(self.config_path)])
        stdout.flush()
        return code, stdout.buffer.getvalue()

    def test_authenticated_and_public_routes_pass_their_bodies_through(self) -> None:
        code, body = self.run_api("/api/snapshot")
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(body)["appVersion"], self.state.snapshot()["appVersion"]
        )
        self.assertTrue(body.endswith(b"\n"))

        code, body = self.run_api("/healthz")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(body)["status"], "ok")

        code, body = self.run_api("/metrics")
        self.assertEqual(code, 0)
        self.assertIn(b"# TYPE mocop_build_info gauge", body)

        code, body = self.run_api("/api/capacity?gpus=2&min_vram_gib=40")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(body)["request"]["gpuCount"], 2)

    def test_server_errors_keep_the_envelope_and_exit_one(self) -> None:
        code, body = self.run_api("/api/history?host=gpu-1&limit=1")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(body)["code"], "INVALID_LIMIT")

        code, body = self.run_api("/api/nope")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(body)["code"], "NOT_FOUND")

    def test_dashboard_tiers_and_malformed_targets_are_refused_locally(self) -> None:
        dashboard_routes = [
            path
            for method, path, access in API_ROUTES
            if method == "GET" and access == "reader"
        ]
        self.assertIn("/api/inventory", dashboard_routes)
        for path in dashboard_routes + ["/api/probe"]:
            with self.subTest(path=path):
                code, body = self.run_api(path)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(body)["code"], "DASHBOARD_ONLY")
        for target in ("api/meta", "http://127.0.0.1/api/meta", "/api/meta#x"):
            with self.subTest(target=target):
                code, body = self.run_api(target)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(body)["code"], "INVALID_TARGET")

    def test_missing_capability_and_unreachable_service_are_reported(self) -> None:
        (self.root / "access-token").unlink()
        code, body = self.run_api("/api/snapshot")
        self.assertEqual(code, 2)
        envelope = json.loads(body)
        self.assertEqual(envelope["code"], "TOKEN_UNAVAILABLE")
        self.assertIn("--token-file", envelope["error"])
        # Public routes never need the capability.
        self.assertEqual(self.run_api("/api/meta")[0], 0)

        elsewhere = self.root / "token.txt"
        elsewhere.write_text(TOKEN, encoding="ascii")
        os.chmod(elsewhere, 0o600)
        self.assertEqual(
            self.run_api("/api/snapshot", "--token-file", str(elsewhere))[0], 0
        )

        self._stop_server()
        code, body = self.run_api("/api/meta")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(body)["code"], "CONNECTION_FAILED")

    def test_event_stream_is_forwarded_line_by_line(self) -> None:
        config = load_config(self.config_path)
        response = client.request(
            "/api/events", config_path=self.config_path, timeout=5
        )
        self.assertEqual(response.body, b"")
        assert response.lines is not None
        first = next(response.lines)
        self.assertEqual(first, b"event: snapshot\n")
        payload = json.loads(next(response.lines).removeprefix(b"data: "))
        self.assertEqual(payload["pollIntervalSeconds"], config.poll_interval_seconds)

    def test_silent_event_stream_becomes_a_connection_failure(self) -> None:
        # A followed stream that stops delivering heartbeats must end with the
        # envelope and exit code of an unreachable service, not a traceback.
        with patch.object(client, "_STREAM_SILENCE_SECONDS", 0.5):
            response = client.request(
                "/api/events", config_path=self.config_path, timeout=0.5
            )
            assert response.lines is not None
            self.assertEqual(next(response.lines), b"event: snapshot\n")
            # The fixture store publishes nothing else and the heartbeat is
            # 15 s away, so the next read outlives the shortened silence bound.
            with self.assertRaises(client.ApiClientError) as raised:
                for _line in response.lines:
                    pass
        self.assertEqual(raised.exception.code, "CONNECTION_FAILED")
        self.assertEqual(raised.exception.exit_code, 1)

    def test_wildcard_binds_map_to_loopback(self) -> None:
        config = load_config(self.config_path)
        for listen_host, expected in (
            ("0.0.0.0", "http://127.0.0.1:1"),
            ("::", "http://[::1]:1"),
            ("127.0.0.1", "http://127.0.0.1:1"),
            ("fe80::1%eth0", "http://[fe80::1%25eth0]:1"),
        ):
            rebound = replace(config, listen_host=listen_host, listen_port=1)
            self.assertEqual(client.service_url(rebound), expected)


if __name__ == "__main__":
    unittest.main()
