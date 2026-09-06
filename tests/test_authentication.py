from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from mocop.__main__ import main
from mocop.api_manifest import API_ROUTES, AUTHENTICATION_MODES
from mocop.client import request
from mocop.config import ConfigError
from mocop.config_loader import load_config
from mocop.lifecycle import UserServiceManager
from mocop.service import StateStore
from mocop.web import MonitorHttpServer
from tests.test_cli import write_config


class AuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "config.json"

    def serve(self, **kwargs) -> MonitorHttpServer:
        server = MonitorHttpServer(("127.0.0.1", 0), StateStore(5), **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown_event.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.addCleanup(stop)
        return server

    def call(self, server, path, *, method="GET", headers=None, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=3
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_mode_is_explicit_and_strict(self) -> None:
        self.assertEqual(
            load_config(write_config(self.config_path)).authentication, "bearer"
        )
        for mode in AUTHENTICATION_MODES:
            config = load_config(write_config(self.config_path, authentication=mode))
            self.assertEqual(config.authentication, mode)
        for mode in (None, "", False, True, 0, [], {}, "NONE", " none", "disabled"):
            with self.subTest(mode=mode), self.assertRaises(ConfigError):
                load_config(write_config(self.config_path, authentication=mode))
        for mode, token in (("bearer", ""), ("", ""), ("invalid", "secret")):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                MonitorHttpServer(
                    ("127.0.0.1", 0),
                    StateStore(5),
                    access_token=token,
                    authentication=mode,
                )

    def test_anonymous_snapshot_metrics_head_and_stream(self) -> None:
        server = self.serve(authentication="none", access_token="")
        for headers in ({}, {"Authorization": "Bearer stale"}):
            code, body = self.call(server, "/api/snapshot", headers=headers)
            self.assertEqual(code, 200)
            self.assertIn("servers", json.loads(body))
        self.assertFalse(server.state.dashboard_attended())
        self.assertEqual(self.call(server, "/metrics")[0], 200)
        self.assertEqual(self.call(server, "/api/snapshot", method="HEAD"), (200, b""))
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=3
        )
        try:
            connection.request("GET", "/api/events")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.readline().strip(), b"event: snapshot")
        finally:
            connection.close()

    def test_default_mode_still_authenticates_every_private_route(self) -> None:
        server = self.serve(access_token="T" * 43)
        for method, path, tier in API_ROUTES:
            if tier == "public":
                continue
            with self.subTest(path=path):
                code, body = self.call(server, path, method=method)
                self.assertEqual(
                    (code, json.loads(body)["code"]), (403, "AUTHENTICATION_REQUIRED")
                )
        code, body = self.call(server, "/api/meta")
        self.assertEqual(json.loads(body)["authentication"], {"mode": "bearer"})
        self.assertEqual(json.loads(body)["write"]["authorization"], "Bearer")

    def test_anonymous_mode_keeps_host_origin_marker_and_body_guards(self) -> None:
        calls = []
        restarted = threading.Event()

        def restart() -> None:
            calls.append(1)
            restarted.set()

        server = self.serve(authentication="none", access_token="", restart=restart)
        origin = f"http://127.0.0.1:{server.server_port}"
        for headers in ({"Host": "attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
            for method, path, tier in API_ROUTES:
                if tier == "public":
                    continue
                with self.subTest(headers=headers, path=path):
                    code, body = self.call(server, path, method=method, headers=headers)
                    self.assertEqual(
                        (code, json.loads(body)["code"]), (403, "UNTRUSTED_ORIGIN")
                    )
        for path in ("/api/unknown", "/metrics/unknown"):
            self.assertEqual(self.call(server, path)[0], 404)
        self.assertEqual(self.call(server, "/api/snapshot", method="DELETE")[0], 405)
        self.assertEqual(self.call(server, "/api/diagnostics")[0], 403)
        self.assertEqual(
            self.call(
                server, "/api/diagnostics", headers={"X-Monitor-Request": "dashboard"}
            )[0],
            200,
        )
        allowed = {
            "X-Monitor-Request": "dashboard",
            "Origin": origin,
            "Content-Type": "application/json",
        }
        for headers in (
            {},
            {**allowed, "Origin": "https://attacker.example"},
            {**allowed, "X-Monitor-Request": ""},
            {**allowed, "Origin": "null"},
        ):
            self.assertEqual(
                self.call(
                    server,
                    "/api/service/restart",
                    method="POST",
                    headers=headers,
                    body=b"{}",
                )[0],
                403,
            )
        self.assertEqual(calls, [])
        self.assertEqual(
            self.call(
                server,
                "/api/service/restart",
                method="POST",
                headers=allowed,
                body=b'{"extra":true}',
            )[0],
            400,
        )
        self.assertEqual(calls, [])
        self.assertEqual(
            self.call(
                server,
                "/api/service/restart",
                method="POST",
                headers=allowed,
                body=b"{}",
            )[0],
            202,
        )
        # The callback is scheduled after the response has been flushed.
        self.assertTrue(restarted.wait(2))
        self.assertEqual(calls, [1])

    def test_anonymous_cli_needs_no_token_and_exposes_effective_policy(self) -> None:
        server = self.serve(authentication="none", access_token="")
        write_config(
            self.config_path, authentication="none", listen_port=server.server_port
        )
        with patch(
            "mocop.client.read_access_token",
            side_effect=AssertionError("must not read a token"),
        ):
            response = request("/api/snapshot", config_path=self.config_path)
            self.assertEqual(response.status, 200)
            response = request("/api/meta", config_path=self.config_path)
        meta = json.loads(response.body)
        self.assertEqual(meta["authentication"], {"mode": "none"})
        self.assertEqual(meta["write"]["authorization"], "none")

    def test_foreground_and_managed_none_ignore_absent_token_files(self) -> None:
        write_config(self.config_path, authentication="none")
        self.config_path.chmod(0o600)
        for extra in (
            [],
            ["--managed-service"],
            ["--managed-service", "--access-token-file", str(self.root / "absent")],
        ):
            with (
                patch(
                    "mocop.__main__.read_access_token",
                    side_effect=AssertionError("token read"),
                ),
                patch(
                    "mocop.__main__.MonitorHttpServer",
                    side_effect=OSError("test bind stop"),
                ) as server,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(["--config", str(self.config_path), *extra]), 1)
            self.assertEqual(server.call_args.kwargs["access_token"], "")
            self.assertEqual(server.call_args.kwargs["authentication"], "none")

    def test_installer_does_not_create_or_read_a_token_in_none_mode(self) -> None:
        write_config(self.config_path, authentication="none")
        self.config_path.chmod(0o600)
        manager = UserServiceManager(
            config_path=self.config_path,
            unit_path=self.root / "mocop.service",
            python_executable=Path("/usr/bin/python3"),
            run=lambda _: 0,
        )
        with patch(
            "mocop.lifecycle.ensure_access_token",
            side_effect=AssertionError("token creation"),
        ):
            manager.install()
            manager.commit_install()
        self.assertFalse((self.root / "access-token").exists())
        with (
            patch("mocop.__main__.UserServiceManager") as cls,
            patch(
                "mocop.__main__.read_access_token",
                side_effect=AssertionError("token read"),
            ),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            mocked = cls.return_value
            mocked.install.return_value = load_config(self.config_path)
            mocked.wait_until_active.return_value = True
            mocked.wait_until_healthy.return_value = True
            mocked.unit_path = self.root / "mocop.service"
            self.assertEqual(
                main(
                    ["service", "install", "--config", str(self.config_path), "--json"]
                ),
                0,
            )
        self.assertEqual(
            json.loads(stdout.getvalue())["dashboardUrl"], "http://127.0.0.1:8787/"
        )
        mocked.wait_until_healthy.assert_called_once_with(
            "127.0.0.1", 8787, authentication="none"
        )

    def test_health_gate_verifies_mode_not_only_http_success(self) -> None:
        manager = UserServiceManager(
            config_path=self.config_path,
            unit_path=self.root / "mocop.service",
            python_executable=Path("/usr/bin/python3"),
            run=lambda _: 0,
        )
        for policy, code, expected in (
            ({"mode": "none"}, 200, True),
            ({"mode": "bearer"}, 403, False),
            ({"mode": "none"}, 403, False),
            ({"mode": "bearer"}, 200, False),
            (None, 200, False),
        ):
            with self.subTest(policy=policy, code=code):
                connection = Mock()
                meta, snapshot = Mock(status=200), Mock(status=code)
                meta.read.return_value = json.dumps(
                    {"apiVersion": "2", "authentication": policy}
                ).encode()
                snapshot.read.return_value = b"{}"
                connection.getresponse.side_effect = (meta, snapshot)
                with patch(
                    "mocop.lifecycle.http.client.HTTPConnection",
                    return_value=connection,
                ):
                    self.assertEqual(
                        manager.wait_until_healthy(
                            "127.0.0.1", 8787, authentication="none", timeout_seconds=0
                        ),
                        expected,
                    )
