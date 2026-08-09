from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mocop.models import ProbeResult, SystemMetrics
from mocop.service import StateStore
from mocop.web import serve_in_thread


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = StateStore(5)
        self.server, self.thread = serve_in_thread("127.0.0.1", 0, self.state)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_snapshot_and_security_headers(self) -> None:
        with urlopen(f"{self.base}/api/snapshot", timeout=2) as response:
            payload = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Server"], "mocop/0.8.0")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn(
                "default-src 'self'", response.headers["Content-Security-Policy"]
            )
        self.assertEqual(payload["stats"]["servers"], 0)
        self.assertEqual(payload["appVersion"], "0.8.0")

    def test_index_is_served(self) -> None:
        with urlopen(f"{self.base}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        with urlopen(f"{self.base}/app.js", timeout=2) as response:
            script = response.read().decode("utf-8")
        self.assertIn("Mocop", body)
        self.assertIn("AI-NATIVE GPU CLUSTER MONITOR", body)
        self.assertIn("GPU 集群控制台", body)
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
        self.assertIn('id="toggle-groups"', body)
        self.assertIn('id="gpu-groups"', body)
        self.assertIn("age(snapshot.lastPollCompletedAt)", script)
        self.assertNotIn("age(snapshot.generatedAt)", script)

    def test_readiness_and_history_contract(self) -> None:
        with self.assertRaises(HTTPError) as not_ready:
            urlopen(f"{self.base}/readyz", timeout=2)
        self.assertEqual(not_ready.exception.code, 503)

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

        with self.assertRaises(HTTPError) as invalid:
            urlopen(f"{self.base}/api/history?host=--proxy&limit=10", timeout=2)
        self.assertEqual(invalid.exception.code, 400)

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

        with self.assertRaises(HTTPError) as invalid:
            urlopen(f"{self.base}/api/incidents?limit=201", timeout=2)
        self.assertEqual(invalid.exception.code, 400)

        with self.assertRaises(HTTPError) as unknown:
            urlopen(f"{self.base}/api/incidents?debug=true", timeout=2)
        self.assertEqual(unknown.exception.code, 400)

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

        self.assertEqual(payload["pollIntervalSeconds"], 10)
        self.assertEqual(payload["collectionStaleAfterSeconds"], 30)
        self.assertIsInstance(payload["version"], int)
        self.assertIsInstance(payload["startedAt"], str)
        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 10)
        self.assertEqual(response.headers["Connection"], "close")

    def test_accepts_same_origin_browser_write_through_host_rewriting_proxy(
        self,
    ) -> None:
        for fetch_site in ("same-origin", None):
            with self.subTest(fetch_site=fetch_site):
                request = self.poll_interval_request(
                    b'{"pollIntervalSeconds":2}',
                    origin="https://workspace-preview.example",
                    fetch_site=fetch_site,
                )
                with urlopen(request, timeout=2) as response:
                    payload = json.load(response)
                self.assertEqual(payload["pollIntervalSeconds"], 2)
        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 2)

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

        with self.assertRaises(HTTPError) as rejected:
            urlopen(request, timeout=2)

        self.assertEqual(rejected.exception.code, 403)
        self.assertIsNone(rejected.exception.headers.get("Access-Control-Allow-Origin"))

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
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(request, timeout=2)
                self.assertEqual(rejected.exception.code, status)

        for fetch_site in ("cross-site", "same-site", "invalid"):
            with self.subTest(fetch_site=fetch_site):
                cross_site = self.poll_interval_request(
                    b'{"pollIntervalSeconds":10}',
                    origin="https://workspace-preview.example",
                    fetch_site=fetch_site,
                )
                with self.assertRaises(HTTPError) as rejected_cross_site:
                    urlopen(cross_site, timeout=2)
                self.assertEqual(rejected_cross_site.exception.code, 403)

        query = self.poll_interval_request(
            b'{"pollIntervalSeconds":10}',
            origin=self.base,
            path="/api/settings/poll-interval?force=true",
        )
        with self.assertRaises(HTTPError) as rejected_query:
            urlopen(query, timeout=2)
        self.assertEqual(rejected_query.exception.code, 400)

        self.assertEqual(self.state.snapshot()["pollIntervalSeconds"], 5)


if __name__ == "__main__":
    unittest.main()
