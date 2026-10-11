from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from email.message import Message
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from mocop.training_client import TrainingClient
from mocop.training_config import TrainingJobsConfig
from mocop.training_projection import ReadError

LIST = "/pt/api/ai/jobs?page=1&page_size=100"


class TrainingClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "token"
        self.token = "znt_" + "x" * 48
        self.path.write_text(self.token + "\n")
        self.path.chmod(0o600)
        self.client = TrainingClient(
            TrainingJobsConfig(True, "https://training.example.invalid", self.path)
        )

    def assert_error(self, status, call):
        with self.assertRaises(ReadError) as error:
            call()
        self.assertEqual(error.exception.status, status)
        self.assertNotIn(self.token, str(error.exception))
        return error.exception

    def test_only_private_regular_non_symlink_token_files_are_read(self):
        self.assertEqual(self.client.token(), self.token)
        self.path.chmod(0o644)
        self.assert_error(401, self.client.token)
        self.path.chmod(0o600)
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        self.client.config = replace(self.client.config, token_file=link)
        self.assert_error(401, self.client.token)
        fifo = self.path.with_name("fifo")
        os.mkfifo(fifo, 0o600)
        self.client.config = replace(self.client.config, token_file=fifo)
        self.assert_error(401, self.client.token)
        self.client.config = replace(
            self.client.config, token_file=self.path.with_name("absent")
        )
        self.assert_error(401, self.client.token)
        self.client.config = replace(self.client.config, token_file=None)
        self.assert_error(401, self.client.token)

    def test_token_format_size_and_rotation(self):
        for value in [b"not-a-token", b"znt_\xff", (self.token + "\n" * 300).encode()]:
            self.path.write_bytes(value)
            self.assert_error(401, self.client.token)
        replacement = "znt_" + "y" * 48
        self.path.write_text(replacement)
        self.assertEqual(self.client.token(), replacement)

    def test_fixed_get_transport_has_no_proxy_or_browser_credentials(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"jobs": [], "total": 0}')
        with patch("mocop.training_client.build_opener", return_value=opener) as build:
            self.assertEqual(self.client.read(LIST)["total"], 0)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://training.example.invalid" + LIST)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.token)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        for path in [
            "https://outside.invalid",
            "/pt/api/ai/jobs/../secrets",
            "/pt/api/ai/jobsevil",
            "/pt/api/ai/jobs?url=https://outside.invalid",
        ]:
            self.assert_error(400, lambda path=path: self.client.read(path))
        self.client.config = replace(self.client.config, origin=None)
        self.assert_error(503, lambda: self.client.read(LIST))

    def test_payload_is_bounded_and_malformed_errors_are_sanitized(self):
        for payload, status in [
            (b"x" * 2_000_001, 502),
            (b"[]", 502),
            (b"invalid", 503),
            (b"\xff", 503),
        ]:
            opener = Mock()
            opener.open.return_value = io.BytesIO(payload)
            with patch("mocop.training_client.build_opener", return_value=opener):
                self.assert_error(status, lambda: self.client.read(LIST))
        opener.open.return_value = io.BytesIO(b'{"sample": NaN}')
        with patch("mocop.training_client.build_opener", return_value=opener):
            self.assertIsNone(self.client.read(LIST)["sample"])
        opener.open.side_effect = URLError(self.token)
        with patch("mocop.training_client.build_opener", return_value=opener):
            self.assert_error(503, lambda: self.client.read(LIST))

    def test_rate_budget_and_expiration_apply_across_callers(self):
        self.client.requests.extend([time.monotonic()] * 60)
        with patch("mocop.training_client.build_opener") as build:
            error = self.assert_error(429, lambda: self.client.read(LIST))
            self.assertGreater(error.retry_after, 0)
            build.assert_not_called()
        self.client.requests.clear()
        self.client.requests.append(time.monotonic() - 61)
        opener = Mock()
        opener.open.return_value = io.BytesIO(b"{}")
        with patch("mocop.training_client.build_opener", return_value=opener):
            self.client.read(LIST)
        self.assertEqual(len(self.client.requests), 1)

    def test_retry_after_seconds_dates_and_invalid_values(self):
        for header, minimum in [
            ("120", 120),
            ("invalid", 30),
            (formatdate(time.time() + 180, usegmt=True), 179),
        ]:
            self.client.cooldown_until = 0
            headers = Message()
            headers["Retry-After"] = header
            opener = Mock()
            opener.open.side_effect = HTTPError(
                "https://training.example.invalid", 429, self.token, headers, None
            )
            with patch("mocop.training_client.build_opener", return_value=opener):
                error = self.assert_error(429, lambda: self.client.read(LIST))
                self.assertGreaterEqual(error.retry_after, minimum)
                self.assert_error(429, lambda: self.client.read(LIST))
                self.assertEqual(opener.open.call_count, 1)
        self.client.cooldown_until = 0
        opener.open.side_effect = HTTPError(
            "https://training.example.invalid", 403, self.token, Message(), None
        )
        with patch("mocop.training_client.build_opener", return_value=opener):
            self.assert_error(403, lambda: self.client.read(LIST))

    def test_redirect_does_not_replay_platform_credential(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                received.append(handler.path)
                handler.send_response(302)
                handler.send_header("Location", "/outside")
                handler.end_headers()

            def log_message(handler, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.client.config = replace(
                self.client.config,
                origin=f"http://127.0.0.1:{server.server_port}",
                allow_http=True,
            )
            self.assert_error(302, lambda: self.client.read(LIST))
            self.assertEqual(received, [LIST])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_simultaneous_reads_never_exceed_two_connections(self):
        lock = threading.Lock()
        current = maximum = 0
        barrier = threading.Barrier(3)
        failures = []

        class Response(io.BytesIO):
            def __enter__(response):
                nonlocal current, maximum
                with lock:
                    current += 1
                    maximum = max(maximum, current)
                time.sleep(0.025)
                return super().__enter__()

            def __exit__(response, *args):
                nonlocal current
                with lock:
                    current -= 1
                return super().__exit__(*args)

        def read():
            try:
                barrier.wait(timeout=2)
                self.client.read(LIST)
            except Exception as error:
                failures.append(error)

        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(
            json.dumps({}).encode()
        )
        with patch("mocop.training_client.build_opener", return_value=opener):
            threads = [threading.Thread(target=read) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
        self.assertFalse(failures)
        self.assertEqual(maximum, 2)

    def test_queued_read_observes_cooldown_before_network(self):
        semaphore = threading.BoundedSemaphore(2)
        semaphore.acquire()
        semaphore.acquire()
        queued = threading.Event()
        failures = []

        class ObservedSemaphore:
            def __enter__(permit):
                queued.set()
                semaphore.acquire()

            def __exit__(permit, *args):
                semaphore.release()

        def read():
            try:
                self.client.read(LIST)
            except ReadError as error:
                failures.append(error.status)

        self.client.connections = ObservedSemaphore()
        opener = Mock()
        opener.open.return_value = io.BytesIO(b"{}")
        with patch("mocop.training_client.build_opener", return_value=opener):
            thread = threading.Thread(target=read)
            thread.start()
            self.assertTrue(queued.wait(timeout=2))
            with self.client.lock:
                self.client.cooldown_until = time.monotonic() + 120
            semaphore.release()
            thread.join(timeout=2)
        semaphore.release()
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [429])
        opener.open.assert_not_called()
