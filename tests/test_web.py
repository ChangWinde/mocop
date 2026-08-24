from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import unittest
from contextlib import suppress
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mocop import __version__
from mocop.inventory import InventoryRequestError
from mocop.models import GpuMetrics, GpuProcess, ProbeResult, SystemMetrics
from mocop.service import StateStore
from mocop.web import (
    API_ROUTES,
    MonitorHttpServer,
    MonitorRequestHandler,
    serve_in_thread,
)


class _Inventory:
    def __init__(self) -> None:
        self.configured = ["gpu-01"]
        self.available = ["gpu-02"]
        self.collector_settings = {
            "pollIntervalSeconds": 5,
            "probeTimeoutSeconds": 15,
            "connectTimeoutSeconds": 10,
            "maxWorkers": 16,
        }
        self.maintenance_windows = {}
        self.host_groups = {}
        self.incident_actions = []
        self.connection_topology = {
            "root": "gpu-01",
            "links": [
                {
                    "source": "gpu-01",
                    "target": "gpu-02",
                    "transport": "frp-stcp",
                    "label": "STCP · 7009",
                }
            ],
        }

    def topology(self):
        return self.connection_topology

    def writable(self):
        return True

    def snapshot(self):
        return {
            "configuredHosts": list(self.configured),
            "activeHosts": list(self.configured),
            "availableHosts": list(self.available),
            "localHost": None,
            "autoDiscover": False,
            "ignoredCodeHostCount": 2,
            "excludedHostCount": 1,
            "collectorSettings": dict(self.collector_settings),
            "maintenanceWindows": dict(self.maintenance_windows),
            "hostGroups": dict(self.host_groups),
            "incidentActions": list(self.incident_actions),
            "writable": True,
        }

    def change(self, action, host):
        if action == "add" and host in self.available:
            self.available.remove(host)
            self.configured.append(host)
        elif action == "remove" and host in self.configured:
            self.configured.remove(host)
            self.available.append(host)
        else:
            raise InventoryRequestError("inventory changed")
        return self.snapshot()

    def update_collector_settings(self, settings):
        self.collector_settings.update(settings)
        return dict(self.collector_settings)

    def update_maintenance(self, host, duration_seconds, reason):
        if host not in self.configured:
            raise InventoryRequestError("inventory changed")
        if duration_seconds:
            self.maintenance_windows[host] = {
                "until": "2030-06-15T12:30:00Z",
                "reason": reason.strip(),
                "active": True,
            }
        else:
            self.maintenance_windows.pop(host, None)
        return self.snapshot()

    def update_host_group(self, host, group):
        if host not in self.configured:
            raise InventoryRequestError("inventory changed")
        normalized = group.strip()
        if normalized:
            self.host_groups[host] = normalized
        else:
            self.host_groups.pop(host, None)
        return self.snapshot()

    def update_incident_action(
        self,
        host,
        condition_key,
        action,
        duration_seconds,
        reason,
        incident_started_at=None,
    ):
        self.incident_actions = [
            item
            for item in self.incident_actions
            if (item["host"], item["condition_key"]) != (host, condition_key)
        ]
        if action != "clear":
            self.incident_actions.append(
                {
                    "host": host,
                    "condition_key": condition_key,
                    "action": action,
                    "until": "2030-08-10T00:00:00Z",
                    "reason": reason,
                    "incident_started_at": incident_started_at,
                }
            )
        return self.snapshot()


class _ProbeControl:
    def __init__(self) -> None:
        self.hosts = []
        self.responses = {}

    def request_probe(self, host):
        self.hosts.append(host)
        if host in self.responses:
            return dict(self.responses[host])
        return {"status": "queued", "accepted": True, "host": host}


class _StubNotificationSink:
    """Enabled notification sink whose queue acceptance is scripted."""

    def __init__(self, queued: bool) -> None:
        self.queued = queued

    def status(self):
        return {
            "enabled": True,
            "healthy": True,
            "queuedDeliveries": 0,
            "droppedDeliveries": 0,
            "endpoints": [],
        }

    def test(self):
        return self.queued

    def publish(self, events, correlations):
        return None

    def close(self, timeout_seconds=5.0):
        return None


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = StateStore(5)
        self.inventory = _Inventory()
        self.probe_control = _ProbeControl()
        self.server, self.thread = serve_in_thread(
            "127.0.0.1",
            0,
            self.state,
            self.inventory,
            probe_control=self.probe_control,
        )
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def assert_http_error(self, request: Request | str, status: int):
        """Assert one HTTP failure and close its response before returning headers."""
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        try:
            self.assertEqual(raised.exception.code, status)
            return raised.exception.headers
        finally:
            raised.exception.close()

    def assert_json_error(self, request: Request | str, status: int, code: str):
        """Assert one JSON error envelope carrying the stable machine code."""
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        error = raised.exception
        try:
            self.assertEqual(error.code, status)
            self.assertIn("application/json", error.headers.get("Content-Type", ""))
            payload = json.load(error)
            self.assertEqual(payload.get("code"), code)
            self.assertIsInstance(payload.get("error"), str)
            return error.headers, payload
        finally:
            error.close()

    def request_status(self, request: Request | str) -> int:
        try:
            with urlopen(request, timeout=2) as response:
                response.read()
                return response.status
        except HTTPError as error:
            error.close()
            return error.code

    def test_dashboard_reads_and_event_streams_mark_viewer_presence(self) -> None:
        self.assertFalse(self.state.dashboard_attended())

        # An unmarked probe (curl, scraper, health check) is not a viewer.
        with urlopen(f"{self.base}/healthz", timeout=2) as response:
            response.read()
        self.assertFalse(self.state.dashboard_attended())

        with urlopen(
            Request(
                f"{self.base}/api/snapshot",
                headers={"X-Monitor-Request": "dashboard"},
            ),
            timeout=2,
        ) as response:
            response.read()
        self.assertTrue(self.state.dashboard_attended())

        # A live event stream keeps refreshing presence on its own.
        fresh = self.standalone_server()
        self.assertFalse(fresh.state.dashboard_attended())
        stream = self.open_event_stream(fresh.server_port)
        stream.recv(256)
        deadline = time.monotonic() + 2
        while not fresh.state.dashboard_attended() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(fresh.state.dashboard_attended())

    def test_bearer_capability_protects_private_api_without_ambient_cookie(
        self,
    ) -> None:
        token = "A" * 43
        protected = self.standalone_server(access_token=token)
        base = f"http://127.0.0.1:{protected.server_port}"

        # Static bootstrap and liveness stay available, while telemetry and
        # operational surfaces are inaccessible to another local UID.
        for path in ("/", "/healthz", "/readyz"):
            with self.subTest(path=path):
                self.assertIn(self.request_status(f"{base}{path}"), {200, 503})
        for path in ("/api/snapshot", "/api/events", "/metrics"):
            with self.subTest(path=path):
                self.assert_json_error(
                    Request(f"{base}{path}"), 403, "AUTHENTICATION_REQUIRED"
                )
        protected_fallbacks = (
            ("POST", "/api/snapshot", b"{}"),
            ("POST", "/api/unknown", b"{}"),
            ("PUT", "/api/snapshot", None),
            ("PATCH", "/api/snapshot", None),
            ("DELETE", "/api/snapshot", None),
            ("TRACE", "/api/snapshot", None),
            ("CONNECT", "/api/snapshot", None),
            ("PROPFIND", "/api/snapshot", None),
            ("COPY", "/api/snapshot", None),
            ("FOO", "/api/snapshot", None),
        )
        for method, path, data in protected_fallbacks:
            with self.subTest(method=method, path=path):
                self.assert_json_error(
                    Request(f"{base}{path}", data=data, method=method),
                    403,
                    "AUTHENTICATION_REQUIRED",
                )
        marked = Request(
            f"{base}/api/snapshot",
            headers={"X-Monitor-Request": "dashboard"},
        )
        self.assert_json_error(marked, 403, "AUTHENTICATION_REQUIRED")
        self.assertFalse(protected.state.dashboard_attended())

        wrong = Request(
            f"{base}/api/snapshot",
            headers={"Authorization": "Bearer wrong-token-value-that-is-long-enough"},
        )
        self.assert_json_error(wrong, 403, "AUTHENTICATION_REQUIRED")

        authenticated = Request(
            f"{base}/api/snapshot",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Monitor-Request": "dashboard",
            },
        )
        with urlopen(authenticated, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.headers.get("Set-Cookie"))
            self.assertIn("version", json.load(response))
        self.assertTrue(protected.state.dashboard_attended())

        authenticated_fallback = Request(
            f"{base}/api/snapshot",
            method="FOO",
            headers={"Authorization": f"Bearer {token}"},
        )
        headers, _ = self.assert_json_error(
            authenticated_fallback, 405, "METHOD_NOT_ALLOWED"
        )
        self.assertEqual(headers["Allow"], "GET, HEAD")

    def standalone_server(self, **kwargs) -> MonitorHttpServer:
        server, thread = serve_in_thread("127.0.0.1", 0, StateStore(5), **kwargs)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def open_connection(self, port: int) -> http.client.HTTPConnection:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        self.addCleanup(connection.close)
        return connection

    def open_event_stream(self, port: int) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(
            b"GET /api/events HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Accept: text/event-stream\r\n\r\n"
        )
        return sock

    @staticmethod
    def read_until(sock: socket.socket, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def read_to_eof(sock: socket.socket) -> bytes:
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return data
            data += chunk

    def test_snapshot_and_security_headers(self) -> None:
        with urlopen(f"{self.base}/api/snapshot", timeout=2) as response:
            payload = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Server"], f"mocop/{__version__}")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn(
                "default-src 'self'", response.headers["Content-Security-Policy"]
            )
            self.assertIn(
                "img-src 'self' data: blob:",
                response.headers["Content-Security-Policy"],
            )
        self.assertEqual(payload["stats"]["servers"], 0)
        self.assertEqual(payload["appVersion"], __version__)

    def test_snapshot_serialization_is_reused_per_state_revision(self) -> None:
        self.assertTrue(self.server.RequestHandlerClass.disable_nagle_algorithm)
        first_snapshot = self.state.snapshot()
        first = self.server.snapshot_payload(first_snapshot)
        repeated = self.server.snapshot_payload(self.state.snapshot())

        self.assertIs(first, repeated)
        self.state.set_hosts(("gpu-01",))
        changed = self.server.snapshot_payload(self.state.snapshot())
        self.assertIsNot(first, changed)
        self.assertNotEqual(first, changed)

    def test_metrics_serialization_is_reused_per_state_revision(self) -> None:
        first = self.server.metrics_payload(self.state.snapshot())
        repeated = self.server.metrics_payload(self.state.snapshot())

        self.assertIs(first, repeated)
        self.state.set_hosts(("gpu-01",))
        changed = self.server.metrics_payload(self.state.snapshot())
        self.assertIsNot(first, changed)

    def test_sse_frames_are_cached_per_state_revision(self) -> None:
        frame = self.server.snapshot_frame(self.state.snapshot())
        payload = self.server.snapshot_payload(self.state.snapshot())

        self.assertEqual(frame, b"event: snapshot\ndata: " + payload + b"\n\n")
        # Same revision, same bytes object: no per-client reassembly.
        self.assertIs(frame, self.server.snapshot_frame(self.state.snapshot()))

        self.state.set_hosts(("gpu-01",))
        changed = self.server.snapshot_frame(self.state.snapshot())
        self.assertIsNot(frame, changed)
        self.assertNotEqual(frame, changed)

    def test_pure_read_paths_prefer_the_state_snapshot_view(self) -> None:
        # Web serialization must use the cached read-only projection.
        calls = []
        baseline = self.state.snapshot

        def snapshot_view():
            calls.append(True)
            return baseline()

        self.state.snapshot_view = snapshot_view
        self.addCleanup(delattr, self.state, "snapshot_view")

        with urlopen(f"{self.base}/api/snapshot", timeout=2) as response:
            self.assertEqual(response.status, 200)
            response.read()
        with urlopen(f"{self.base}/metrics", timeout=2) as response:
            self.assertEqual(response.status, 200)
            response.read()

        self.assertEqual(len(calls), 2)

    def test_sse_emits_named_heartbeat_events(self) -> None:
        with (
            patch("mocop.web._SSE_HEARTBEAT_SECONDS", 0.05),
            patch("mocop.web._SSE_STOP_POLL_SECONDS", 0.02),
        ):
            server = self.standalone_server()
            sock = self.open_event_stream(server.server_port)
            data = self.read_until(sock, b"event: heartbeat\ndata: {}\n\n")

        # The stream still opens with a snapshot before idling into
        # browser-visible named heartbeats.
        self.assertIn(b"event: snapshot", data)

    def test_expected_client_disconnects_do_not_hide_server_errors(self) -> None:
        with patch.object(ThreadingHTTPServer, "handle_error") as inherited:
            try:
                raise ConnectionResetError("client closed the connection")
            except ConnectionResetError:
                self.server.handle_error(object(), ("127.0.0.1", 1))
            inherited.assert_not_called()

            try:
                raise RuntimeError("unexpected handler failure")
            except RuntimeError:
                self.server.handle_error(object(), ("127.0.0.1", 1))
            inherited.assert_called_once()

    def test_exposes_current_snapshot_as_openmetrics(self) -> None:
        with urlopen(f"{self.base}/metrics", timeout=2) as response:
            body = response.read().decode()

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.headers["Content-Type"],
            "application/openmetrics-text; version=1.0.0; charset=utf-8",
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("# TYPE mocop_build_info gauge\n", body)
        self.assertIn("mocop_cluster_servers 0\n", body)
        self.assertTrue(body.endswith("# EOF\n"))

        self.assert_http_error(f"{self.base}/metrics?host=gpu-01", 400)

    def test_index_is_served(self) -> None:
        with urlopen(f"{self.base}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        with urlopen(f"{self.base}/app.js", timeout=2) as response:
            script = response.read().decode("utf-8")
        with urlopen(f"{self.base}/dashboard-auth.js", timeout=2) as response:
            authentication_script = response.read().decode("utf-8")
        with urlopen(f"{self.base}/process-search.js", timeout=2) as response:
            search_script = response.read().decode("utf-8")
        with urlopen(f"{self.base}/capacity-watch.js", timeout=2) as response:
            watch_script = response.read().decode("utf-8")
        with urlopen(f"{self.base}/format.js", timeout=2) as response:
            format_script = response.read().decode("utf-8")
        self.assertIn("Mocop", body)
        self.assertIn("AI-NATIVE GPU CLUSTER MONITOR", body)
        self.assertIn("GPU 集群实时监控", body)
        self.assertIn('id="attention-panel"', body)
        self.assertIn('data-attention-filter="storage"', body)
        self.assertIn('id="incident-panel"', body)
        self.assertIn('id="export-csv"', body)
        self.assertIn('id="server-bar"', body)
        self.assertIn('id="cpu-bar"', body)
        self.assertIn('id="gpu-bar"', body)
        self.assertIn('id="gpu-memory-bar"', body)
        self.assertIn('class="search-shortcut"', body)
        self.assertIn('id="gpu-sort"', body)
        self.assertIn('id="refresh-interval"', body)
        self.assertIn('<option value="5" selected>5 秒</option>', body)
        self.assertIn('id="toggle-groups"', body)
        self.assertIn('id="gpu-groups"', body)
        self.assertIn('id="settings-toggle"', body)
        self.assertIn('id="settings-dialog"', body)
        self.assertIn('id="collector-settings-form"', body)
        self.assertIn('id="restart-service"', body)
        self.assertIn('id="restart-confirm-dialog"', body)
        self.assertIn('fetch("/api/service/restart"', script)
        self.assertIn('id="interface-density"', body)
        self.assertIn('id="default-server-filter"', body)
        self.assertIn('id="server-sort"', body)
        for style in (
            "precision",
            "glass",
            "terminal",
            "ledger",
            "blueprint",
            "studio",
        ):
            self.assertIn(f'data-style-choice="{style}"', body)
        for accent in ("cobalt", "cyan", "violet", "emerald", "amber", "rose"):
            self.assertIn(f'data-accent-choice="{accent}"', body)
        self.assertIn('id="background-image-input"', body)
        self.assertIn('accept="image/png,image/jpeg,image/webp,image/avif"', body)
        self.assertIn('id="background-visibility"', body)
        self.assertIn('id="remove-background-image"', body)
        self.assertNotIn('accept="image/svg+xml', body)
        self.assertIn('id="inventory-refresh"', body)
        self.assertIn('id="configured-host-list"', body)
        self.assertIn('id="available-host-list"', body)
        self.assertIn("维护窗口不会停止采集", body)
        self.assertIn('fetch("/api/settings/maintenance"', script)
        self.assertIn('fetch("/api/settings/host-group"', script)
        self.assertIn('<option value="group">节点分组</option>', body)
        self.assertIn('id="gpu-detail-dialog"', body)
        self.assertIn('id="gpu-task-list"', body)
        self.assertIn('id="gpu-history-grid"', body)
        self.assertIn('id="gpu-process-timeline"', body)
        self.assertIn('id="incident-detail-dialog"', body)
        self.assertIn('id="probe-now"', body)
        self.assertIn('id="export-diagnostics"', body)
        self.assertIn('id="test-notifications"', body)
        self.assertIn('fetch("/api/settings/incident-action"', script)
        self.assertIn('fetch("/api/probe"', script)
        self.assertIn('id="capacity-toggle"', body)
        self.assertIn('id="capacity-dialog"', body)
        self.assertIn('id="capacity-form"', body)
        self.assertIn('id="topology-toggle"', body)
        self.assertIn('id="topology-dialog"', body)
        self.assertIn('id="topology-tree"', body)
        self.assertIn('fetch("/api/topology"', script)
        self.assertIn("不会触发额外 SSH 请求", body)
        self.assertNotIn('class="heatmap-legend"', body)
        self.assertIn('class="gpu-col-temperature"', body)
        self.assertIn('class="gpu-col-power"', body)
        self.assertIn("age(snapshot.lastPollCompletedAt)", script)
        self.assertNotIn("age(snapshot.generatedAt)", script)
        self.assertIn("createProcessSearch", search_script)
        self.assertIn("MocopDashboardAuth", authentication_script)
        self.assertIn("MocopCapacityWatch", watch_script)
        self.assertIn("MocopFormat", format_script)
        self.assertIn('id="authentication-dialog"', body)
        self.assertIn('id="capacity-watch-toggle"', body)
        self.assertIn('id="capacity-watch-banner"', body)
        self.assertIn('id="gpu-detail-ssh"', body)
        self.assertIn('src="/dashboard-auth.js"', body)
        self.assertIn('src="/process-search.js"', body)
        self.assertIn('src="/capacity-watch.js"', body)
        self.assertIn('src="/format.js"', body)

    def test_static_assets_support_etag_revalidation(self) -> None:
        conn = self.open_connection(self.server.server_port)

        conn.request("GET", "/app.js")
        first = conn.getresponse()
        body = first.read()
        etag = first.getheader("ETag")
        self.assertEqual(first.status, 200)
        self.assertTrue(etag and etag.startswith('"') and etag.endswith('"'))
        # Negotiated caching: revalidate every time, then reuse on a match.
        self.assertEqual(first.getheader("Cache-Control"), "no-cache")

        conn.request("GET", "/app.js", headers={"If-None-Match": etag})
        cached = conn.getresponse()
        self.assertEqual(cached.status, 304)
        self.assertEqual(cached.getheader("ETag"), etag)
        self.assertEqual(cached.read(), b"")

        conn.request("GET", "/app.js", headers={"If-None-Match": f'"stale", W/{etag}'})
        listed = conn.getresponse()
        self.assertEqual(listed.status, 304)
        self.assertEqual(listed.read(), b"")

        conn.request("GET", "/app.js", headers={"If-None-Match": '"stale"'})
        refreshed = conn.getresponse()
        self.assertEqual(refreshed.status, 200)
        self.assertEqual(refreshed.read(), body)

    def test_service_restart_capability_is_explicit_and_disabled_by_default(
        self,
    ) -> None:
        with urlopen(f"{self.base}/api/service", timeout=2) as response:
            capability = json.load(response)
            # The endpoint survives as a compatible alias of /api/meta but
            # advertises its deprecation.
            self.assertEqual(response.headers["Deprecation"], "true")
        self.assertEqual(capability, {"restartSupported": False})

        request = self.poll_interval_request(
            b"{}",
            origin=self.base,
            path="/api/service/restart",
        )
        self.assert_http_error(request, 503)

    def test_service_restart_requires_same_origin_and_invokes_fixed_callback(
        self,
    ) -> None:
        requested = threading.Event()
        server, thread = serve_in_thread(
            "127.0.0.1",
            0,
            StateStore(5),
            restart=requested.set,
        )
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        with urlopen(f"{base}/api/service", timeout=2) as response:
            capability = json.load(response)
        self.assertEqual(capability, {"restartSupported": True})

        rejected = self.poll_interval_request(
            b"{}",
            origin="https://attacker.example",
            path="/api/service/restart",
            fetch_site="cross-site",
        )
        rejected.full_url = f"{base}/api/service/restart"
        self.assert_http_error(rejected, 403)
        self.assertFalse(requested.is_set())

        invalid = self.poll_interval_request(
            b'{"action":"restart"}',
            origin=base,
            path="/api/service/restart",
        )
        invalid.full_url = f"{base}/api/service/restart"
        self.assert_http_error(invalid, 400)
        self.assertFalse(requested.is_set())

        accepted = self.poll_interval_request(
            b"{}",
            origin=base,
            path="/api/service/restart",
        )
        accepted.full_url = f"{base}/api/service/restart"
        with urlopen(accepted, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(response.status, 202)
        self.assertEqual(payload, {"status": "restarting"})
        self.assertTrue(requested.wait(1))

    def test_meta_describes_versions_capabilities_and_endpoints(self) -> None:
        with urlopen(f"{self.base}/api/meta", timeout=2) as response:
            meta = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(meta["apiVersion"], "2")
        self.assertEqual(meta["appVersion"], __version__)
        self.assertEqual(meta["schemaVersion"], 1)
        self.assertEqual(
            meta["capabilities"],
            {
                "restartSupported": False,
                "manualProbeSupported": True,
                "configurationWriteSupported": True,
            },
        )
        # The endpoint list is the module-level manifest, verbatim.
        self.assertEqual(
            meta["endpoints"],
            [
                {"method": method, "path": path, "access": access}
                for method, path, access in API_ROUTES
            ],
        )

        self.assert_json_error(f"{self.base}/api/meta?x=1", 400, "QUERY_NOT_ALLOWED")

        head = self.open_connection(self.server.server_port)
        head.request("HEAD", "/api/meta")
        mirrored = head.getresponse()
        self.assertEqual(mirrored.status, 200)
        self.assertGreater(int(mirrored.getheader("Content-Length")), 0)
        self.assertEqual(mirrored.read(), b"")

        # Wiring flips the capability flags: restart present, inventory and
        # probe control absent.
        bare = self.standalone_server(restart=lambda: None)
        with urlopen(
            f"http://127.0.0.1:{bare.server_port}/api/meta", timeout=2
        ) as response:
            capabilities = json.load(response)["capabilities"]
        self.assertEqual(
            capabilities,
            {
                "restartSupported": True,
                "manualProbeSupported": False,
                "configurationWriteSupported": False,
            },
        )

    def test_meta_endpoint_manifest_matches_live_routes(self) -> None:
        for method, path, access in API_ROUTES:
            with self.subTest(method=method, path=path):
                if path == "/api/events":
                    sock = self.open_event_stream(self.server.server_port)
                    self.assertIn(b"200 OK", self.read_until(sock, b"event: snapshot"))
                    continue
                if method == "GET":
                    headers = (
                        {"X-Monitor-Request": "dashboard"} if access == "reader" else {}
                    )
                    status = self.request_status(
                        Request(f"{self.base}{path}", headers=headers)
                    )
                    # Routed paths may reject the bare probe (missing query,
                    # not ready), but they must exist and accept GET.
                    self.assertNotIn(status, {404, 405})
                else:
                    status = self.request_status(
                        Request(f"{self.base}{path}", data=b"{}", method="POST")
                    )
                    # The same-origin gate proves the POST route exists.
                    self.assertEqual(status, 403)

    def test_unknown_api_paths_and_method_mismatches_return_json(self) -> None:
        headers, _ = self.assert_json_error(
            f"{self.base}/api/unknown", 404, "NOT_FOUND"
        )
        self.assertIsNone(headers.get("Allow"))
        self.assert_json_error(f"{self.base}/healthz/extra", 404, "NOT_FOUND")
        self.assert_json_error(f"{self.base}/readyz/extra", 404, "NOT_FOUND")
        self.assert_json_error(f"{self.base}/metrics/extra", 404, "NOT_FOUND")
        self.assert_json_error(
            f"{self.base}/metrics?host=gpu-01", 400, "QUERY_NOT_ALLOWED"
        )

        post_to_read = Request(f"{self.base}/api/snapshot", data=b"{}", method="POST")
        headers, _ = self.assert_json_error(post_to_read, 405, "METHOD_NOT_ALLOWED")
        self.assertEqual(headers["Allow"], "GET, HEAD")

        post_to_events = Request(f"{self.base}/api/events", data=b"{}", method="POST")
        headers, _ = self.assert_json_error(post_to_events, 405, "METHOD_NOT_ALLOWED")
        self.assertEqual(headers["Allow"], "GET")

        headers, _ = self.assert_json_error(
            f"{self.base}/api/settings/collector", 405, "METHOD_NOT_ALLOWED"
        )
        self.assertEqual(headers["Allow"], "POST")

        post_to_unknown = Request(
            f"{self.base}/api/settings/unknown", data=b"{}", method="POST"
        )
        self.assert_json_error(post_to_unknown, 404, "NOT_FOUND")

        # Static page paths keep the HTML error page contract.
        headers = self.assert_http_error(f"{self.base}/missing", 404)
        self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_usage_endpoint_serves_the_rollup_and_validates_bounds(self) -> None:
        with urlopen(f"{self.base}/api/usage", timeout=2) as response:
            usage = json.load(response)
        self.assertEqual(usage["windowHours"], 24)
        self.assertEqual(usage["owners"], [])
        self.assertEqual(usage["totalOwners"], 0)
        self.assertEqual(usage["droppedRecords"], 0)
        with urlopen(f"{self.base}/api/usage?hours=1&limit=5", timeout=2) as response:
            self.assertEqual(json.load(response)["windowHours"], 1)

        self.assert_json_error(f"{self.base}/api/usage?hours=0", 400, "INVALID_HOURS")
        self.assert_json_error(f"{self.base}/api/usage?hours=abc", 400, "INVALID_HOURS")
        self.assert_json_error(f"{self.base}/api/usage?limit=0", 400, "INVALID_LIMIT")
        self.assert_json_error(
            f"{self.base}/api/usage?scope=all", 400, "UNKNOWN_QUERY_PARAMETER"
        )

    def test_api_errors_carry_stable_machine_readable_codes(self) -> None:
        self.assert_json_error(
            f"{self.base}/api/history?host=--proxy&limit=10", 400, "INVALID_QUERY"
        )
        self.assert_json_error(
            f"{self.base}/api/history?host=gpu-01&limit=999", 400, "INVALID_LIMIT"
        )
        self.assert_json_error(
            f"{self.base}/api/history?host=missing&limit=10", 404, "UNKNOWN_HOST"
        )
        self.assert_json_error(
            f"{self.base}/api/history?debug=1", 400, "UNKNOWN_QUERY_PARAMETER"
        )
        self.assert_json_error(f"{self.base}/api/inventory", 403, "UNTRUSTED_ORIGIN")

        cross_origin = self.poll_interval_request(
            b'{"pollIntervalSeconds":10}',
            origin="https://attacker.example",
            fetch_site="cross-site",
        )
        self.assert_json_error(cross_origin, 403, "UNTRUSTED_ORIGIN")

        wrong_type = self.poll_interval_request(
            b'{"pollIntervalSeconds":10}',
            origin=self.base,
            content_type="text/plain",
        )
        self.assert_json_error(wrong_type, 415, "UNSUPPORTED_MEDIA_TYPE")

        oversized = self.poll_interval_request(
            b'{"pollIntervalSeconds":10,"padding":"' + b"x" * 129 + b'"}',
            origin=self.base,
        )
        self.assert_json_error(oversized, 413, "PAYLOAD_TOO_LARGE")

        invalid_json = self.poll_interval_request(b"{", origin=self.base)
        self.assert_json_error(invalid_json, 400, "INVALID_JSON")

        bad_schema = self.poll_interval_request(b'{"other":1}', origin=self.base)
        self.assert_json_error(bad_schema, 400, "INVALID_SCHEMA")

        out_of_bounds = self.poll_interval_request(
            b'{"pollIntervalSeconds":1}', origin=self.base
        )
        self.assert_json_error(out_of_bounds, 400, "INVALID_SETTINGS")

        stale_inventory = self.poll_interval_request(
            b'{"action":"add","host":"unknown"}',
            origin=self.base,
            path="/api/settings/hosts",
        )
        self.assert_json_error(stale_inventory, 409, "INVENTORY_CHANGED")

    def test_readiness_and_history_contract(self) -> None:
        self.assert_http_error(f"{self.base}/readyz", 503)

        system = SystemMetrics(
            hostname="node-a",
            uptime_seconds=1,
            load_1m=0,
            load_5m=0,
            load_15m=0,
            cpu_cores=1,
            cpu_usage_pct=1,
            memory_total_mib=10,
            memory_used_mib=1,
            memory_available_mib=9,
            swap_total_mib=0,
            swap_used_mib=0,
            disk_total_mib=10,
            disk_used_mib=1,
            network_rx_bps=0,
            network_tx_bps=0,
        )
        self.state.set_hosts(("gpu-1",))
        self.state.apply(ProbeResult("gpu-1", "online", 1, system=system))
        with urlopen(f"{self.base}/readyz", timeout=2) as response:
            self.assertTrue(json.load(response)["ready"])
        with urlopen(
            f"{self.base}/api/history?host=gpu-1&limit=10", timeout=2
        ) as response:
            history = json.load(response)
        self.assertEqual(history["host"], "gpu-1")
        self.assertEqual(len(history["points"]), 1)

        self.assert_http_error(
            f"{self.base}/api/history?host=--proxy&limit=10",
            400,
        )

    def test_gpu_history_and_sanitized_diagnostics_require_dashboard_reads(
        self,
    ) -> None:
        gpu = GpuMetrics(
            index=0,
            uuid="GPU-SECRET-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=50,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
            processes=(GpuProcess(42, "private-train.py", 250),),
        )
        self.state.set_hosts(("gpu-01",))
        self.state.apply(ProbeResult("gpu-01", "online", 1, (gpu,)))
        read_headers = {"X-Monitor-Request": "dashboard"}

        with urlopen(
            Request(
                f"{self.base}/api/gpu-history?host=gpu-01&gpu=GPU-SECRET-1&limit=10",
                headers=read_headers,
            ),
            timeout=2,
        ) as response:
            history = json.load(response)
        with urlopen(
            Request(f"{self.base}/api/diagnostics", headers=read_headers),
            timeout=2,
        ) as response:
            diagnostics = json.load(response)

        self.assertEqual(history["gpuId"], "GPU-SECRET-1")
        serialized = json.dumps(diagnostics)
        self.assertNotIn("GPU-SECRET-1", serialized)
        self.assertNotIn("private-train.py", serialized)
        self.assertEqual(diagnostics["servers"][0]["node"], "node-001")
        self.assert_http_error(f"{self.base}/api/diagnostics", 403)

    def test_condition_action_and_manual_probe_are_same_origin_bounded_writes(
        self,
    ) -> None:
        self.state.set_hosts(("gpu-01",))
        self.state.apply(
            ProbeResult(
                "gpu-01",
                "unreachable",
                1,
                (),
                message="SSH failed",
            )
        )
        action_request = self.poll_interval_request(
            json.dumps(
                {
                    "host": "gpu-01",
                    "conditionKey": "connectivity",
                    "incidentStartedAt": self.state.active_incident_started_at(
                        "gpu-01", "connectivity"
                    ),
                    "action": "acknowledged",
                    "durationSeconds": 3600,
                    "reason": "owner notified",
                }
            ).encode(),
            origin=self.base,
            path="/api/settings/incident-action",
        )
        with urlopen(action_request, timeout=2) as response:
            action_result = json.load(response)
        self.assertEqual(action_result["incidentActions"][0]["action"], "acknowledged")

        probe_request = self.poll_interval_request(
            b'{"host":"gpu-01"}', origin=self.base, path="/api/probe"
        )
        with urlopen(probe_request, timeout=2) as response:
            probe_result = json.load(response)
        self.assertTrue(probe_result["accepted"])
        self.assertEqual(self.probe_control.hosts, ["gpu-01"])

        cross_origin = self.poll_interval_request(
            b'{"host":"gpu-01"}',
            origin="https://attacker.example",
            path="/api/probe",
            fetch_site="cross-site",
        )
        self.assert_http_error(cross_origin, 403)

    def test_rejects_incident_actions_for_future_inactive_conditions(self) -> None:
        request = self.poll_interval_request(
            json.dumps(
                {
                    "host": "gpu-01",
                    "conditionKey": "gpu_temperature:GPU-future",
                    "incidentStartedAt": "2026-08-14T00:00:00Z",
                    "action": "silenced",
                    "durationSeconds": 604800,
                    "reason": "future maintenance",
                }
            ).encode(),
            origin=self.base,
            path="/api/settings/incident-action",
        )

        self.assert_json_error(request, 409, "INCIDENT_NOT_ACTIVE")
        self.assertEqual(self.inventory.incident_actions, [])

    def test_rate_limited_probe_reports_retry_after_and_code(self) -> None:
        self.probe_control.responses["gpu-01"] = {
            "status": "rate_limited",
            "accepted": False,
            "host": "gpu-01",
            "retryAfterSeconds": 3.2,
        }
        request = self.poll_interval_request(
            b'{"host":"gpu-01"}', origin=self.base, path="/api/probe"
        )

        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        error = raised.exception
        try:
            self.assertEqual(error.code, 429)
            # The integral header rounds the cooldown up so clients never
            # retry early.
            self.assertEqual(error.headers["Retry-After"], "4")
            payload = json.load(error)
        finally:
            error.close()
        self.assertEqual(payload["status"], "rate_limited")
        self.assertEqual(payload["code"], "RATE_LIMITED")

    def test_notification_test_distinguishes_disabled_from_rate_limited(self) -> None:
        # Without a configured sink the endpoint is unavailable, not limited.
        disabled = self.poll_interval_request(
            b"{}", origin=self.base, path="/api/notifications/test"
        )
        self.assert_json_error(disabled, 503, "NOTIFICATIONS_DISABLED")

        sink = _StubNotificationSink(queued=False)
        state = StateStore(5, notifications=sink)
        server, thread = serve_in_thread("127.0.0.1", 0, state)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        limited = self.poll_interval_request(
            b"{}", origin=base, path="/api/notifications/test"
        )
        limited.full_url = f"{base}/api/notifications/test"
        self.assert_json_error(limited, 429, "RATE_LIMITED")

        sink.queued = True
        accepted = self.poll_interval_request(
            b"{}", origin=base, path="/api/notifications/test"
        )
        accepted.full_url = f"{base}/api/notifications/test"
        with urlopen(accepted, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(response.status, 202)
        self.assertEqual(payload, {"status": "queued"})

    def test_exposes_static_connection_topology_without_query_parameters(self) -> None:
        request = Request(
            f"{self.base}/api/topology",
            headers={"X-Monitor-Request": "dashboard"},
        )
        with urlopen(request, timeout=2) as response:
            topology = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(topology, self.inventory.connection_topology)

        self.assert_http_error(f"{self.base}/api/topology", 403)

        request = Request(
            f"{self.base}/api/topology?refresh=true",
            headers={"X-Monitor-Request": "dashboard"},
        )
        self.assert_http_error(request, 400)

    def test_incident_query_is_bounded(self) -> None:
        self.state.set_hosts(("offline",))
        self.state.apply(
            ProbeResult(
                "offline",
                "unreachable",
                5000,
                message="SSH connection timed out",
            )
        )
        with urlopen(f"{self.base}/api/incidents?limit=10", timeout=2) as response:
            incidents = json.load(response)
        self.assertEqual(incidents["version"], 1)
        self.assertEqual(incidents["events"][0]["host"], "offline")

        self.assert_http_error(f"{self.base}/api/incidents?limit=201", 400)
        self.assert_http_error(f"{self.base}/api/incidents?debug=true", 400)

    def test_scans_and_changes_the_constrained_host_inventory(self) -> None:
        scan = Request(
            f"{self.base}/api/inventory",
            headers={"X-Monitor-Request": "dashboard"},
        )
        with urlopen(scan, timeout=2) as response:
            inventory = json.load(response)
        self.assertEqual(inventory["configuredHosts"], ["gpu-01"])
        self.assertEqual(inventory["availableHosts"], ["gpu-02"])
        self.assertEqual(inventory["ignoredCodeHostCount"], 2)
        self.assertEqual(inventory["maintenanceWindows"], {})
        self.assertEqual(inventory["hostGroups"], {})

        add = self.poll_interval_request(
            b'{"action":"add","host":"gpu-02"}',
            origin=self.base,
            path="/api/settings/hosts",
        )
        with urlopen(add, timeout=2) as response:
            changed = json.load(response)
        self.assertEqual(changed["configuredHosts"], ["gpu-01", "gpu-02"])

        remove = self.poll_interval_request(
            b'{"action":"remove","host":"gpu-01"}',
            origin=self.base,
            path="/api/settings/hosts",
        )
        with urlopen(remove, timeout=2) as response:
            changed = json.load(response)
        self.assertEqual(changed["configuredHosts"], ["gpu-02"])

    def test_sets_and_clears_time_bounded_maintenance(self) -> None:
        request = self.poll_interval_request(
            json.dumps(
                {
                    "host": "gpu-01",
                    "durationSeconds": 14_400,
                    "reason": "Driver upgrade",
                }
            ).encode(),
            origin=self.base,
            path="/api/settings/maintenance",
        )
        with urlopen(request, timeout=2) as response:
            changed = json.load(response)

        self.assertEqual(
            changed["maintenanceWindows"]["gpu-01"]["reason"],
            "Driver upgrade",
        )

        clear = self.poll_interval_request(
            b'{"host":"gpu-01","durationSeconds":0,"reason":""}',
            origin=self.base,
            path="/api/settings/maintenance",
        )
        with urlopen(clear, timeout=2) as response:
            cleared = json.load(response)
        self.assertEqual(cleared["maintenanceWindows"], {})

    def test_sets_and_clears_shared_host_groups(self) -> None:
        request = self.poll_interval_request(
            b'{"host":"gpu-01","group":"Training"}',
            origin=self.base,
            path="/api/settings/host-group",
        )
        with urlopen(request, timeout=2) as response:
            changed = json.load(response)
        self.assertEqual(changed["hostGroups"], {"gpu-01": "Training"})

        clear = self.poll_interval_request(
            b'{"host":"gpu-01","group":""}',
            origin=self.base,
            path="/api/settings/host-group",
        )
        with urlopen(clear, timeout=2) as response:
            cleared = json.load(response)
        self.assertEqual(cleared["hostGroups"], {})

    def test_rejects_invalid_or_stale_host_group_writes(self) -> None:
        cases = (
            (b'{"host":"--bad","group":"Training"}', 400),
            (b'{"host":"gpu-01","group":"x\\u007f"}', 400),
            (b'{"host":"gpu-01","group":"x\\u202e"}', 400),
            (json.dumps({"host": "gpu-01", "group": "x" * 49}).encode(), 400),
            (b'{"host":"unknown","group":"Training"}', 409),
            (b'{"host":"gpu-01","group":"Training","extra":1}', 400),
            (b'{"host":"gpu-01","host":"gpu-01","group":"Training"}', 400),
        )
        for payload, status in cases:
            with self.subTest(payload=payload):
                request = self.poll_interval_request(
                    payload,
                    origin=self.base,
                    path="/api/settings/host-group",
                )
                self.assert_http_error(request, status)

    def test_rejects_invalid_or_stale_maintenance_writes(self) -> None:
        cases = (
            (b'{"host":"gpu-01","durationSeconds":60,"reason":"Work"}', 400),
            (b'{"host":"gpu-01","durationSeconds":3600,"reason":""}', 400),
            (b'{"host":"--bad","durationSeconds":3600,"reason":"Work"}', 400),
            (b'{"host":"gpu-01","durationSeconds":true,"reason":"Work"}', 400),
            (b'{"host":"gpu-01","durationSeconds":3600,"reason":"Work\\u007f"}', 400),
            (b'{"host":"gpu-01","durationSeconds":3600,"reason":"Work\\u202e"}', 400),
            (b'{"host":"unknown","durationSeconds":3600,"reason":"Work"}', 409),
            (
                b'{"host":"gpu-01","durationSeconds":3600,"reason":"Work","extra":1}',
                400,
            ),
            (
                b'{"host":"gpu-01","host":"gpu-01","durationSeconds":3600,"reason":"Work"}',
                400,
            ),
        )
        for payload, status in cases:
            with self.subTest(payload=payload):
                request = self.poll_interval_request(
                    payload,
                    origin=self.base,
                    path="/api/settings/maintenance",
                )
                self.assert_http_error(request, status)

    def test_rejects_unmarked_or_cross_site_inventory_scans(self) -> None:
        self.assert_http_error(f"{self.base}/api/inventory", 403)

        cross_site = Request(
            f"{self.base}/api/inventory",
            headers={
                "X-Monitor-Request": "dashboard",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assert_http_error(cross_site, 403)

    def test_rejects_invalid_or_stale_inventory_writes(self) -> None:
        cases = (
            (b'{"action":"add","host":"unknown"}', 409),
            (b'{"action":"add","host":"--proxy"}', 400),
            (b'{"action":"replace","host":"gpu-01"}', 400),
            (b'{"action":"add","host":"gpu-02","extra":true}', 400),
            (b'{"action":"add","action":"remove","host":"gpu-02"}', 400),
        )
        for payload, status in cases:
            with self.subTest(payload=payload):
                request = self.poll_interval_request(
                    payload,
                    origin=self.base,
                    path="/api/settings/hosts",
                )
                self.assert_http_error(request, status)

        cross_origin = self.poll_interval_request(
            b'{"action":"add","host":"gpu-02"}',
            origin="https://attacker.example",
            path="/api/settings/hosts",
            fetch_site="cross-site",
        )
        self.assert_http_error(cross_origin, 403)

    def poll_interval_request(
        self,
        payload: bytes,
        *,
        origin: str | None = None,
        content_type: str = "application/json",
        marker: str | None = "dashboard",
        path: str = "/api/settings/poll-interval",
        fetch_site: str | None = None,
    ) -> Request:
        headers = {"Content-Type": content_type}
        if origin is not None:
            headers["Origin"] = origin
        if marker is not None:
            headers["X-Monitor-Request"] = marker
        if fetch_site is not None:
            headers["Sec-Fetch-Site"] = fetch_site
        return Request(
            f"{self.base}{path}",
            data=payload,
            headers=headers,
            method="POST",
        )

    def test_updates_runtime_poll_interval_with_bounded_same_origin_json(self) -> None:
        request = self.poll_interval_request(
            b'{"pollIntervalSeconds":10}',
            origin=self.base,
        )

        with urlopen(request, timeout=2) as response:
            payload = json.load(response)

        # The alias keeps its legacy response shape but flags its deprecation.
        self.assertEqual(
            set(payload),
            {
                "version",
                "startedAt",
                "pollIntervalSeconds",
                "collectionStaleAfterSeconds",
            },
        )
        self.assertEqual(response.headers["Deprecation"], "true")
        self.assertEqual(payload["pollIntervalSeconds"], 10)
        self.assertEqual(payload["collectionStaleAfterSeconds"], 30)
        self.assertIsInstance(payload["version"], int)
        self.assertIsInstance(payload["startedAt"], str)
        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 10)
        self.assertEqual(self.inventory.collector_settings["pollIntervalSeconds"], 10)
        self.assertEqual(response.headers["Connection"], "close")

    def test_updates_all_persisted_collector_settings(self) -> None:
        request = self.poll_interval_request(
            json.dumps(
                {
                    "pollIntervalSeconds": 2,
                    "probeTimeoutSeconds": 24,
                    "maxWorkers": 8,
                }
            ).encode(),
            origin=self.base,
            path="/api/settings/collector",
        )

        with urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertEqual(
            payload["collectorSettings"],
            {
                "pollIntervalSeconds": 2,
                "probeTimeoutSeconds": 24,
                "connectTimeoutSeconds": 10,
                "maxWorkers": 8,
            },
        )
        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 2)

    def test_updates_a_collector_settings_subset(self) -> None:
        request = self.poll_interval_request(
            b'{"probeTimeoutSeconds":30}',
            origin=self.base,
            path="/api/settings/collector",
        )

        with urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertEqual(
            payload["collectorSettings"],
            {
                "pollIntervalSeconds": 5,
                "probeTimeoutSeconds": 30,
                "connectTimeoutSeconds": 10,
                "maxWorkers": 16,
            },
        )
        # Omitted fields stay untouched, including the runtime poll cadence.
        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 5)

    def test_rejects_invalid_collector_settings(self) -> None:
        cases = (
            b"{}",
            b'{"connectTimeoutSeconds":9}',
            b'{"pollIntervalSeconds":1,"probeTimeoutSeconds":24,"maxWorkers":8}',
            b'{"pollIntervalSeconds":2,"probeTimeoutSeconds":true,"maxWorkers":8}',
            b'{"pollIntervalSeconds":2,"probeTimeoutSeconds":24,"maxWorkers":2.5}',
            b'{"pollIntervalSeconds":2,"probeTimeoutSeconds":24,"maxWorkers":65}',
            b'{"pollIntervalSeconds":2,"probeTimeoutSeconds":24,"maxWorkers":8,"extra":1}',
            b'{"pollIntervalSeconds":2,"probeTimeoutSeconds":24,"maxWorkers":8,"maxWorkers":9}',
        )
        before = dict(self.inventory.collector_settings)

        for payload in cases:
            with self.subTest(payload=payload):
                request = self.poll_interval_request(
                    payload,
                    origin=self.base,
                    path="/api/settings/collector",
                )
                self.assert_http_error(request, 400)

        cross_origin = self.poll_interval_request(
            b'{"pollIntervalSeconds":2,"probeTimeoutSeconds":24,"maxWorkers":8}',
            origin="https://attacker.example",
            path="/api/settings/collector",
            fetch_site="cross-site",
        )
        self.assert_http_error(cross_origin, 403)

        oversized = self.poll_interval_request(
            b'{"pollIntervalSeconds":2,"probeTimeoutSeconds":24,"maxWorkers":8,"padding":"'
            + b"x" * 512
            + b'"}',
            origin=self.base,
            path="/api/settings/collector",
        )
        self.assert_http_error(oversized, 413)

        self.assertEqual(self.inventory.collector_settings, before)

    def test_rejects_external_origins_by_default_even_on_loopback(self) -> None:
        # An untrusted Origin must never authorize a write, even when the TCP
        # connection arrives on 127.0.0.1 (DNS rebinding delivers exactly that).
        for fetch_site in ("same-origin", None):
            with self.subTest(fetch_site=fetch_site):
                request = self.poll_interval_request(
                    b'{"pollIntervalSeconds":2}',
                    origin="https://workspace-preview.example",
                    fetch_site=fetch_site,
                )
                self.assert_http_error(request, 403)
        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 5)

    def test_trusted_hosts_allow_writes_through_host_rewriting_proxy(self) -> None:
        state = StateStore(5)
        server, thread = serve_in_thread(
            "127.0.0.1",
            0,
            state,
            _Inventory(),
            trusted_hosts=("workspace-preview.example",),
        )
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertIn("workspace-preview.example", server.trusted_hostnames)

        request = self.poll_interval_request(
            b'{"pollIntervalSeconds":2}',
            origin="https://workspace-preview.example",
        )
        request.full_url = (
            f"http://127.0.0.1:{server.server_port}/api/settings/poll-interval"
        )
        with urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["pollIntervalSeconds"], 2)
        self.assertEqual(state.snapshot()["pollIntervalSeconds"], 2)

    def test_trusted_https_origin_suffix_supports_ephemeral_proxy_hosts(self) -> None:
        state = StateStore(5)
        server, thread = serve_in_thread(
            "127.0.0.1",
            0,
            state,
            _Inventory(),
            trusted_hosts=("*.fwd.memory.whalent.com",),
        )
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertNotIn("fwd.memory.whalent.com", server.trusted_hostnames)
        self.assertEqual(
            server.trusted_origin_suffixes, frozenset({"fwd.memory.whalent.com"})
        )

        allowed = self.poll_interval_request(
            b'{"pollIntervalSeconds":2}',
            origin="https://35sc9ontjvbtfee57ow5sdynv.fwd.memory.whalent.com",
        )
        allowed.full_url = (
            f"http://127.0.0.1:{server.server_port}/api/settings/poll-interval"
        )
        with urlopen(allowed, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["pollIntervalSeconds"], 2)

        rejected_origins = (
            "http://35sc9ontjvbtfee57ow5sdynv.fwd.memory.whalent.com",
            "https://fwd.memory.whalent.com",
            "https://fwd.memory.whalent.com.attacker.example",
            "https://attacker.example",
        )
        for origin in rejected_origins:
            with self.subTest(origin=origin):
                request = self.poll_interval_request(
                    b'{"pollIntervalSeconds":3}', origin=origin
                )
                request.full_url = (
                    f"http://127.0.0.1:{server.server_port}/api/settings/poll-interval"
                )
                self.assert_http_error(request, 403)
        self.assertEqual(state.snapshot()["pollIntervalSeconds"], 2)

    def test_rejects_dns_rebinding_hosts_despite_loopback_delivery(self) -> None:
        port = self.server.server_port
        rebound = {
            "Host": f"monitor.attacker.example:{port}",
            "Origin": f"http://monitor.attacker.example:{port}",
            "X-Monitor-Request": "dashboard",
            "Sec-Fetch-Site": "same-origin",
        }

        write = self.open_connection(port)
        write.request(
            "POST",
            "/api/settings/poll-interval",
            body=b'{"pollIntervalSeconds":2}',
            headers={**rebound, "Content-Type": "application/json"},
        )
        response = write.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 5)

        read = self.open_connection(port)
        read.request("GET", "/api/inventory", headers=dict(rebound))
        response = read.getresponse()
        self.assertEqual(response.status, 403)
        response.read()

    def test_rejects_cross_origin_preflight_without_cors_permission(self) -> None:
        request = Request(
            f"{self.base}/api/settings/poll-interval",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-monitor-request",
            },
            method="OPTIONS",
        )

        headers = self.assert_http_error(request, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_rejects_unsafe_or_invalid_poll_interval_updates(self) -> None:
        cases = (
            (b'{"pollIntervalSeconds":10}', None, "application/json", "dashboard", 403),
            (
                b'{"pollIntervalSeconds":10}',
                "not-an-origin",
                "application/json",
                "dashboard",
                403,
            ),
            (
                b'{"pollIntervalSeconds":10}',
                "https://user:password@example.com",
                "application/json",
                "dashboard",
                403,
            ),
            (
                b'{"pollIntervalSeconds":10}',
                "https://example.com/path",
                "application/json",
                "dashboard",
                403,
            ),
            (
                b'{"pollIntervalSeconds":10}',
                "https://example.com:invalid",
                "application/json",
                "dashboard",
                403,
            ),
            (b'{"pollIntervalSeconds":10}', self.base, "text/plain", "dashboard", 415),
            (b'{"pollIntervalSeconds":10}', self.base, "application/json", None, 403),
            (
                b'{"pollIntervalSeconds":1}',
                self.base,
                "application/json",
                "dashboard",
                400,
            ),
            (
                b'{"pollIntervalSeconds":61}',
                self.base,
                "application/json",
                "dashboard",
                400,
            ),
            (
                b'{"pollIntervalSeconds":true}',
                self.base,
                "application/json",
                "dashboard",
                400,
            ),
            (
                b'{"pollIntervalSeconds":NaN}',
                self.base,
                "application/json",
                "dashboard",
                400,
            ),
            (
                b'{"pollIntervalSeconds":10,"extra":1}',
                self.base,
                "application/json",
                "dashboard",
                400,
            ),
            (
                b'{"pollIntervalSeconds":10,"pollIntervalSeconds":20}',
                self.base,
                "application/json",
                "dashboard",
                400,
            ),
            (
                b'{"pollIntervalSeconds":10,"padding":"' + b"x" * 129 + b'"}',
                self.base,
                "application/json",
                "dashboard",
                413,
            ),
        )
        for payload, origin, content_type, marker, status in cases:
            with self.subTest(
                payload=payload, origin=origin, content_type=content_type
            ):
                request = self.poll_interval_request(
                    payload,
                    origin=origin,
                    content_type=content_type,
                    marker=marker,
                )
                self.assert_http_error(request, status)

        for fetch_site in ("cross-site", "same-site", "invalid"):
            with self.subTest(fetch_site=fetch_site):
                cross_site = self.poll_interval_request(
                    b'{"pollIntervalSeconds":10}',
                    origin="https://workspace-preview.example",
                    fetch_site=fetch_site,
                )
                self.assert_http_error(cross_site, 403)

        query = self.poll_interval_request(
            b'{"pollIntervalSeconds":10}',
            origin=self.base,
            path="/api/settings/poll-interval?force=true",
        )
        self.assert_http_error(query, 400)

        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 5)

    def test_healthz_reports_cumulative_transport_retries(self) -> None:
        with urlopen(f"{self.base}/healthz", timeout=2) as response:
            initial = json.load(response)
        self.assertEqual(
            initial, {"status": "ok", "ready": False, "transportRetries": 0}
        )

        self.state.set_hosts(("gpu-1",))
        self.state.apply(ProbeResult("gpu-1", "online", 1, transport_retries=3))
        with urlopen(f"{self.base}/healthz", timeout=2) as response:
            retried = json.load(response)
        self.assertEqual(retried["transportRetries"], 3)

    def test_ipv6_literal_binds_and_serves_health_when_available(self) -> None:
        capability = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            capability.bind(("::1", 0))
        except OSError as exc:
            self.skipTest(f"IPv6 loopback is unavailable: {exc}")
        finally:
            capability.close()

        server, thread = serve_in_thread("::1", 0, StateStore(5))
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        connection = http.client.HTTPConnection("::1", server.server_port, timeout=2)
        self.addCleanup(connection.close)
        connection.request("GET", "/healthz")
        response = connection.getresponse()

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["status"], "ok")

    def test_sse_clients_beyond_the_limit_get_503(self) -> None:
        with patch.object(MonitorHttpServer, "max_sse_clients", 1):
            server = self.standalone_server()

            first = self.open_event_stream(server.server_port)
            stream = self.read_until(first, b"event: snapshot")
            self.assertIn(b"200 OK", stream)

            second = self.open_event_stream(server.server_port)
            rejected = self.read_until(second, b"too many event stream clients")
            self.assertIn(b"503", rejected.split(b"\r\n", 1)[0])

    def test_sse_emits_one_consistent_connection_header(self) -> None:
        sock = socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=5
        )
        self.addCleanup(sock.close)
        sock.sendall(
            b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )

        headers = self.read_until(sock, b"\r\n\r\n").split(b"\r\n\r\n", 1)[0]

        self.assertEqual(headers.lower().count(b"\r\nconnection:"), 1)
        self.assertIn(b"\r\nConnection: close\r\n", headers + b"\r\n")

    def test_connections_beyond_the_limit_get_503(self) -> None:
        with patch.object(MonitorHttpServer, "max_concurrent_connections", 1):
            server = self.standalone_server()

            holder = self.open_event_stream(server.server_port)
            self.read_until(holder, b"event: snapshot")

            overflow = socket.create_connection(
                ("127.0.0.1", server.server_port), timeout=5
            )
            self.addCleanup(overflow.close)
            rejected = self.read_until(overflow, b"\r\n\r\n")
            self.assertTrue(rejected.startswith(b"HTTP/1.1 503"), rejected)

    def test_server_close_terminates_live_event_streams(self) -> None:
        server, thread = serve_in_thread("127.0.0.1", 0, StateStore(5))
        self.addCleanup(thread.join, 2)

        sock = self.open_event_stream(server.server_port)
        self.read_until(sock, b"event: snapshot")

        server.shutdown()
        server.server_close()

        # The stream must end promptly; a hung worker would keep the socket
        # open and this read would time out.
        self.read_to_eof(sock)

    def test_idle_keep_alive_connections_are_closed_by_timeout(self) -> None:
        with patch.object(MonitorRequestHandler, "timeout", 1):
            server = self.standalone_server()
            sock = socket.create_connection(
                ("127.0.0.1", server.server_port), timeout=5
            )
            self.addCleanup(sock.close)
            sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            response = self.read_until(sock, b"}")
            self.assertIn(b"200 OK", response)

            # The idle keep-alive socket must be dropped by the read timeout.
            self.read_to_eof(sock)

    def test_request_headers_have_an_absolute_deadline(self) -> None:
        with patch.object(MonitorRequestHandler, "request_deadline_seconds", 0.2):
            server = self.standalone_server()
            sock = socket.create_connection(
                ("127.0.0.1", server.server_port), timeout=2
            )
            self.addCleanup(sock.close)
            fragments = (
                b"GET /healthz HTTP/1.1\r\n",
                b"Host: 127.0.0.1\r\n",
                b"X-Slow: one",
                b" two",
                b" three\r\n\r\n",
            )
            for fragment in fragments:
                with suppress(OSError):
                    sock.sendall(fragment)
                time.sleep(0.07)

            response = self.read_to_eof(sock)
            self.assertNotIn(b"200 OK", response)

    def test_http11_requires_one_valid_host_and_origin_form(self) -> None:
        cases = (
            b"GET /healthz HTTP/1.1\r\n\r\n",
            b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nHost: localhost\r\n\r\n",
            b"GET /healthz HTTP/1.1\r\nHost: bad/path\r\n\r\n",
            b"GET http://attacker.example/healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
        )
        for request in cases:
            with self.subTest(request=request):
                sock = socket.create_connection(
                    ("127.0.0.1", self.server.server_port), timeout=2
                )
                self.addCleanup(sock.close)
                sock.sendall(request)
                response = self.read_until(sock, b"\r\n\r\n")
                self.assertIn(b"400", response.split(b"\r\n", 1)[0])

    def test_posts_reject_ambiguous_http_framing(self) -> None:
        headers = b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
        cases = (
            headers + b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}",
            headers + b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            headers + b"Content-Length: +2\r\n\r\n{}",
        )
        for framing in cases:
            with self.subTest(framing=framing):
                sock = socket.create_connection(
                    ("127.0.0.1", self.server.server_port), timeout=2
                )
                self.addCleanup(sock.close)
                sock.sendall(b"POST /api/notifications/test HTTP/1.1\r\n" + framing)
                response = self.read_until(sock, b"INVALID_REQUEST_FRAMING")
                self.assertIn(b"400", response.split(b"\r\n", 1)[0])
                self.assertIn(b"INVALID_REQUEST_FRAMING", response)

    def test_extreme_content_length_is_rejected_without_integer_conversion(
        self,
    ) -> None:
        huge = b"9" * 5000
        for request, status in (
            (
                b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + b"Content-Length: "
                + huge
                + b"\r\n\r\n",
                b"400",
            ),
            (
                b"POST /api/notifications/test HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
                b"Origin: http://127.0.0.1\r\nX-Monitor-Request: dashboard\r\n"
                b"Content-Length: " + huge + b"\r\n\r\n",
                b"413",
            ),
        ):
            with self.subTest(status=status):
                sock = socket.create_connection(
                    ("127.0.0.1", self.server.server_port), timeout=2
                )
                self.addCleanup(sock.close)
                sock.sendall(request)
                response = self.read_until(sock, b"\r\n\r\n")
                self.assertIn(status, response.split(b"\r\n", 1)[0])

    def test_unsupported_api_methods_use_the_json_405_contract(self) -> None:
        for method in (
            "PUT",
            "PATCH",
            "DELETE",
            "TRACE",
            "CONNECT",
            "PROPFIND",
            "COPY",
            "FOO",
        ):
            with self.subTest(method=method):
                connection = self.open_connection(self.server.server_port)
                connection.request(method, "/api/snapshot")
                response = connection.getresponse()
                payload = json.load(response)
                self.assertEqual(response.status, 405)
                self.assertEqual(
                    response.getheader("Content-Type"),
                    "application/json; charset=utf-8",
                )
                self.assertEqual(response.getheader("Allow"), "GET, HEAD")
                self.assertEqual(payload["code"], "METHOD_NOT_ALLOWED")

        connection = self.open_connection(self.server.server_port)
        connection.request("FOO", "/api/unknown")
        response = connection.getresponse()
        payload = json.load(response)
        self.assertEqual(response.status, 404)
        self.assertEqual(payload["code"], "NOT_FOUND")

    def test_rejects_request_bodies_on_bodyless_methods(self) -> None:
        port = self.server.server_port

        with_body = self.open_connection(port)
        with_body.request("GET", "/healthz", body=b"12345")
        response = with_body.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(response.getheader("Connection"), "close")
        self.assertIn(b"request body is not allowed", response.read())

        head_with_body = self.open_connection(port)
        head_with_body.request("HEAD", "/healthz", body=b"12345")
        response = head_with_body.getresponse()
        self.assertEqual(response.status, 400)
        response.read()

        chunked = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(chunked.close)
        chunked.sendall(
            b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        rejected = self.read_until(chunked, b"\r\n\r\n")
        self.assertIn(b"400", rejected.split(b"\r\n", 1)[0])
        self.read_to_eof(chunked)

        harmless = self.open_connection(port)
        harmless.request("GET", "/healthz", headers={"Content-Length": "0"})
        response = harmless.getresponse()
        self.assertEqual(response.status, 200)
        response.read()

    def test_malformed_inputs_return_json_errors_not_crashes(self) -> None:
        port = self.server.server_port

        # Unbalanced-bracket request targets (absolute form, as a proxy would
        # send them) must not crash the handler thread.
        get_target = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(get_target.close)
        get_target.sendall(b"GET http://[ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        rejected = self.read_until(get_target, b"invalid request target")
        self.assertIn(b"400", rejected.split(b"\r\n", 1)[0])

        post_target = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(post_target.close)
        post_target.sendall(
            b"POST http://[ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        )
        rejected = self.read_until(post_target, b"invalid request target")
        self.assertIn(b"400", rejected.split(b"\r\n", 1)[0])

        # An unbalanced-bracket Origin is a 403, not an unhandled ValueError.
        bracket_origin = self.poll_interval_request(
            b'{"pollIntervalSeconds":10}', origin="http://["
        )
        self.assert_http_error(bracket_origin, 403)

        # Array-typed action fields are schema errors, not TypeErrors.
        inventory_action = self.poll_interval_request(
            b'{"action":["add"],"host":"gpu-02"}',
            origin=self.base,
            path="/api/settings/hosts",
        )
        self.assert_http_error(inventory_action, 400)

        incident_action = self.poll_interval_request(
            json.dumps(
                {
                    "host": "gpu-01",
                    "conditionKey": "connectivity",
                    "incidentStartedAt": None,
                    "action": ["acknowledged"],
                    "durationSeconds": 3600,
                    "reason": "",
                }
            ).encode(),
            origin=self.base,
            path="/api/settings/incident-action",
        )
        self.assert_http_error(incident_action, 400)

        # Integers too large for float conversion are 400s, not OverflowErrors.
        huge_number = (
            b'{"pollIntervalSeconds":'
            + b"9" * 400
            + b',"probeTimeoutSeconds":24,"maxWorkers":8}'
        )
        huge_case = self.poll_interval_request(
            huge_number, origin=self.base, path="/api/settings/collector"
        )
        self.assert_http_error(huge_case, 400)

    def test_head_mirrors_get_and_event_stream_rejects_head(self) -> None:
        conn = self.open_connection(self.server.server_port)

        conn.request("GET", "/healthz")
        get_response = conn.getresponse()
        body = get_response.read()
        self.assertEqual(get_response.status, 200)

        conn.request("HEAD", "/healthz")
        head_response = conn.getresponse()
        self.assertEqual(head_response.status, 200)
        self.assertEqual(head_response.getheader("Content-Length"), str(len(body)))
        self.assertEqual(head_response.read(), b"")

        # The reused connection stays in sync: HEAD wrote no body bytes.
        conn.request("GET", "/healthz")
        reused = conn.getresponse()
        self.assertEqual(reused.status, 200)
        self.assertEqual(reused.read(), body)

        conn.request("HEAD", "/app.js")
        static = conn.getresponse()
        self.assertEqual(static.status, 200)
        self.assertGreater(int(static.getheader("Content-Length")), 0)
        self.assertEqual(static.read(), b"")

        conn.request("HEAD", "/metrics")
        metrics = conn.getresponse()
        self.assertEqual(metrics.status, 200)
        self.assertEqual(metrics.read(), b"")

        conn.request("HEAD", "/api/events")
        events = conn.getresponse()
        self.assertEqual(events.status, 405)
        self.assertEqual(events.getheader("Allow"), "GET")
        self.assertEqual(events.read(), b"")

        conn.request("HEAD", "/missing")
        missing = conn.getresponse()
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.read(), b"")

    def test_head_state_does_not_suppress_a_later_post_response(self) -> None:
        connection = self.open_connection(self.server.server_port)
        connection.request("HEAD", "/healthz")
        head = connection.getresponse()
        self.assertEqual(head.status, 200)
        self.assertEqual(head.read(), b"")

        body = b'{"pollIntervalSeconds":10}'
        connection.request(
            "POST",
            "/api/settings/poll-interval",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": self.base,
                "X-Monitor-Request": "dashboard",
            },
        )
        response = connection.getresponse()
        payload = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["pollIntervalSeconds"], 10)


if __name__ == "__main__":
    unittest.main()
