from __future__ import annotations

import http.client
import json
import threading
import unittest
from unittest.mock import Mock

from mocop.service import StateStore
from mocop.training_projection import ReadError
from mocop.web import MonitorHttpServer

TOKEN = "T" * 43
JOB_ID = "1" * 25


class TrainingWebTests(unittest.TestCase):
    def setUp(self):
        self.source = Mock()
        self.source.snapshot.return_value = {"enabled": True, "jobs": []}
        self.source.detail.return_value = {"job": {"id": JOB_ID}, "logs": {"lines": []}}
        self.server = MonitorHttpServer(
            ("127.0.0.1", 0),
            StateStore(5),
            access_token=TOKEN,
            training_jobs=self.source,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", *, authenticated=True, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        self.addCleanup(connection.close)
        request_headers = {"Authorization": "Bearer " + TOKEN} if authenticated else {}
        request_headers.update(headers or {})
        connection.request(method, path, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        return (
            response.status,
            dict(response.getheaders()),
            json.loads(raw) if raw else None,
        )

    def test_authentication_runs_before_private_source(self):
        for path in ["/api/training-jobs", f"/api/training-job-detail?id={JOB_ID}"]:
            status, _, body = self.request(path, authenticated=False)
            self.assertEqual(status, 403)
            self.assertEqual(body["code"], "AUTHENTICATION_REQUIRED")
        self.source.snapshot.assert_not_called()
        self.source.detail.assert_not_called()

    def test_manifest_and_authenticated_get_head_contracts(self):
        status, _, meta = self.request("/api/meta", authenticated=False)
        self.assertEqual(status, 200)
        routes = {row["path"]: row for row in meta["endpoints"]}
        self.assertEqual(routes["/api/training-jobs"]["access"], "authenticated")
        self.assertTrue(routes["/api/training-job-detail"]["query"]["id"]["required"])
        status, headers, body = self.request("/api/training-jobs")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.source.snapshot.return_value)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("/api/training-jobs", "HEAD")[2], None)
        status, _, body = self.request(f"/api/training-job-detail?id={JOB_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(body["job"]["id"], JOB_ID)
        self.source.detail.assert_called_once_with(JOB_ID)

    def test_strict_queries_and_methods_never_become_platform_writes(self):
        for query in [
            "",
            "?id=",
            f"?id={JOB_ID}&id={JOB_ID}",
            f"?id={JOB_ID}&url=https://outside.invalid",
        ]:
            self.assertEqual(self.request("/api/training-job-detail" + query)[0], 400)
        self.assertEqual(self.request("/api/training-jobs?url=x")[0], 400)
        self.source.detail.assert_not_called()
        for path in ["/api/training-jobs", "/api/training-job-detail"]:
            status, headers, body = self.request(path, "POST")
            self.assertEqual(status, 405)
            self.assertEqual(body["code"], "METHOD_NOT_ALLOWED")
            self.assertEqual(headers["Allow"], "GET, HEAD")

    def test_source_failures_use_stable_sanitized_codes(self):
        for upstream, expected, code in [
            (400, 400, "INVALID_TRAINING_JOB"),
            (401, 403, "TRAINING_ACCESS_DENIED"),
            (403, 403, "TRAINING_ACCESS_DENIED"),
            (404, 404, "TRAINING_JOB_NOT_FOUND"),
            (429, 429, "TRAINING_RATE_LIMITED"),
            (502, 503, "TRAINING_READ_FAILED"),
        ]:
            self.source.detail.side_effect = ReadError(upstream, 45)
            status, headers, body = self.request(
                f"/api/training-job-detail?id={JOB_ID}"
            )
            self.assertEqual((status, body["code"]), (expected, code))
            if upstream == 429:
                self.assertEqual(headers["Retry-After"], "45")
            self.assertNotIn(TOKEN, json.dumps(body))

    def test_disabled_feature_is_hidden_and_detail_unavailable(self):
        self.server.training_jobs = None
        self.assertEqual(self.request("/api/training-jobs")[2], {"enabled": False})
        status, _, body = self.request(f"/api/training-job-detail?id={JOB_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "TRAINING_NOT_CONFIGURED")

    def test_enabled_source_cannot_bypass_monitor_bearer_requirement(self):
        with self.assertRaises(ValueError):
            MonitorHttpServer(
                ("127.0.0.1", 0),
                StateStore(5),
                access_token="",
                authentication="none",
                training_jobs=self.source,
            )

    def test_untrusted_browser_and_declared_read_body_are_rejected(self):
        self.assertEqual(
            self.request(
                "/api/training-jobs",
                authenticated=False,
                headers={"Sec-Fetch-Site": "cross-site"},
            )[0],
            403,
        )
        self.assertEqual(
            self.request("/api/training-jobs", headers={"Content-Length": "1"})[0], 400
        )
        self.source.snapshot.assert_not_called()
