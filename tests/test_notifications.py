from __future__ import annotations

import io
import json
import socket
import ssl
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlsplit

from mocop.config import WebhookConfig
from mocop.incidents import IncidentCondition, IncidentEvent
from mocop.notifications import (
    DeliveryResult,
    NotificationEnvelope,
    NotificationError,
    PinnedHttpsWebhookSender,
    WebhookNotificationSink,
    _Endpoint,
    _WebhookWorker,
    create_notification_sink,
)


def resolver_for(address: str):
    def resolve(host, port, *, type):
        del host
        return [(socket.AF_INET, type, 6, "", (address, port))]

    return resolve


def incident() -> IncidentEvent:
    return IncidentEvent(
        event_id=42,
        host="gpu-01",
        condition=IncidentCondition(
            key="connectivity",
            category="connectivity",
            resource="SSH",
            severity="critical",
            value=None,
            threshold=None,
            observed_at="2026-08-10T00:00:00Z",
            detail="SSH connection timed out",
        ),
        state="opened",
        observed_at="2026-08-10T00:00:00Z",
    )


class _RecordingSender:
    def __init__(self) -> None:
        self.calls = []
        self.delivered = threading.Event()

    def send(self, endpoint, body, headers):
        self.calls.append((endpoint, body, headers))
        if len(self.calls) == 1:
            return DeliveryResult(False, True)
        self.delivered.set()
        return DeliveryResult(True, False)


class _ImmediateSender:
    def __init__(self) -> None:
        self.calls = []

    def send(self, endpoint, body, headers):
        self.calls.append((endpoint, body, headers))
        return DeliveryResult(True, False)


class _AlwaysRetryableSender:
    def __init__(self) -> None:
        self.calls = []

    def send(self, endpoint, body, headers):
        self.calls.append((endpoint, body, headers))
        return DeliveryResult(False, True)


class _GateSender:
    def __init__(self) -> None:
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()

    def send(self, endpoint, body, headers):
        del endpoint, body
        self.calls.append(headers["X-Mocop-Event-ID"])
        self.started.set()
        if len(self.calls) == 1:
            self.release.wait(2)
        return DeliveryResult(True, False)


class _DrippingBody(io.RawIOBase):
    """Serves canned header bytes, then a throttled synthetic body."""

    def __init__(
        self, header: bytes, body_length: int, chunk_size: int, chunk_delay: float
    ) -> None:
        super().__init__()
        self._header = header
        self._header_offset = 0
        self._body_remaining = body_length
        self._chunk_size = chunk_size
        self._chunk_delay = chunk_delay

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._header_offset < len(self._header):
            amount = min(len(buffer), len(self._header) - self._header_offset)
            view = self._header[self._header_offset : self._header_offset + amount]
            buffer[:amount] = view
            self._header_offset += amount
            return amount
        if self._body_remaining <= 0:
            return 0
        if self._chunk_delay:
            time.sleep(self._chunk_delay)
        amount = min(len(buffer), self._chunk_size, self._body_remaining)
        buffer[:amount] = b"x" * amount
        self._body_remaining -= amount
        return amount


class _ScriptedTlsSocket:
    def __init__(
        self,
        response: bytes,
        body_length: int = 0,
        chunk_size: int = 65536,
        chunk_delay: float = 0.0,
    ) -> None:
        self.sent = bytearray()
        self._reader = io.BufferedReader(
            _DrippingBody(response, body_length, chunk_size, chunk_delay)
        )

    def settimeout(self, value) -> None:
        del value

    def sendall(self, data) -> None:
        self.sent += data

    def makefile(self, mode):
        del mode
        return self._reader

    def close(self) -> None:
        pass


class _ScriptedTlsContext:
    """Duck-typed TLS context that hands back a scripted socket."""

    # Python 3.10's http.client reads these attributes from the context when
    # constructing an HTTPSConnection; newer versions do not.
    verify_mode = ssl.CERT_REQUIRED
    post_handshake_auth = None
    check_hostname = True

    def __init__(self, tls_socket: _ScriptedTlsSocket) -> None:
        self._tls_socket = tls_socket
        self.server_hostnames = []

    def wrap_socket(self, sock, server_hostname=None):
        sock.close()
        self.server_hostnames.append(server_hostname)
        return self._tls_socket


class _PassThroughTlsContext:
    verify_mode = ssl.CERT_REQUIRED
    post_handshake_auth = None
    check_hostname = True

    def wrap_socket(self, sock, server_hostname=None):
        del server_hostname
        return sock


class _ScriptedRawSocket:
    def __init__(self) -> None:
        self.closed = False

    def settimeout(self, value) -> None:
        del value

    def close(self) -> None:
        self.closed = True


class NotificationTests(unittest.TestCase):
    def config(self, **changes) -> WebhookConfig:
        values = {
            "name": "ops",
            "url_env": "MOCOP_WEBHOOK_URL",
            "secret_env": "MOCOP_WEBHOOK_SECRET",
            "events": ("opened", "resolved"),
            "timeout_seconds": 1,
            "max_attempts": 2,
            "retry_base_seconds": 0.1,
            "min_interval_seconds": 0,
            "allow_private_networks": False,
        }
        values.update(changes)
        return WebhookConfig(**values)

    def test_rejects_missing_insecure_or_private_targets_by_default(self) -> None:
        cases = (
            ({}, resolver_for("8.8.8.8")),
            (
                {"MOCOP_WEBHOOK_URL": "http://hooks.example.test/event"},
                resolver_for("8.8.8.8"),
            ),
            (
                {"MOCOP_WEBHOOK_URL": "https://hooks.example.test/event"},
                resolver_for("127.0.0.1"),
            ),
            (
                {"MOCOP_WEBHOOK_URL": "https://[broken/event"},
                resolver_for("8.8.8.8"),
            ),
            (
                {"MOCOP_WEBHOOK_URL": "https://hooks.example.test:bad/event"},
                resolver_for("8.8.8.8"),
            ),
        )
        for environment, resolver in cases:
            environment["MOCOP_WEBHOOK_SECRET"] = "secret"
            with (
                self.subTest(environment=environment),
                self.assertRaises(NotificationError),
            ):
                create_notification_sink(
                    (self.config(),), environ=environment, resolver=resolver
                )

    def test_rejects_non_utf8_webhook_environment_without_leaking_unicode_errors(
        self,
    ) -> None:
        cases = (
            {
                "MOCOP_WEBHOOK_URL": "https://\udcff.example.test/events",
                "MOCOP_WEBHOOK_SECRET": "secret",
            },
            {
                "MOCOP_WEBHOOK_URL": "https://hooks.example.test/events",
                "MOCOP_WEBHOOK_SECRET": "\udcff",
            },
        )
        for environment in cases:
            with (
                self.subTest(environment=environment),
                self.assertRaises(NotificationError),
            ):
                create_notification_sink(
                    (self.config(),),
                    environ=environment,
                    resolver=resolver_for("8.8.8.8"),
                )

    def test_retries_signs_deduplicates_and_includes_correlation_context(self) -> None:
        sender = _RecordingSender()
        sink = create_notification_sink(
            (self.config(),),
            environ={
                "MOCOP_WEBHOOK_URL": "https://hooks.example.test/events",
                "MOCOP_WEBHOOK_SECRET": "top-secret",
            },
            resolver=resolver_for("8.8.8.8"),
            sender=sender,
        )
        self.addCleanup(sink.close)
        event = incident()
        correlation = {
            "kind": "configured_shared_path",
            "anchor": "gateway",
            "hosts": ["gpu-01", "gpu-02"],
            "confidence": "possible",
        }

        sink.publish((event,), (correlation,))
        sink.publish((event,), (correlation,))

        self.assertTrue(sender.delivered.wait(2), "webhook retry did not complete")
        self.assertEqual(len(sender.calls), 2)
        payload = json.loads(sender.calls[-1][1])
        headers = sender.calls[-1][2]
        self.assertEqual(payload["event"]["eventId"], 42)
        self.assertEqual(payload["correlation"]["anchor"], "gateway")
        self.assertRegex(headers["X-Mocop-Signature"], r"^sha256=[0-9a-f]{64}$")
        self.assertEqual(sink.status()["endpoints"][0]["deliveredEvents"], 1)

    def test_operator_test_delivery_is_bounded_and_identified(self) -> None:
        sender = _RecordingSender()
        sink = create_notification_sink(
            (self.config(),),
            environ={
                "MOCOP_WEBHOOK_URL": "https://hooks.example.test/events",
                "MOCOP_WEBHOOK_SECRET": "top-secret",
            },
            resolver=resolver_for("8.8.8.8"),
            sender=sender,
        )
        self.addCleanup(sink.close)

        self.assertTrue(sink.test())
        self.assertFalse(sink.test())
        self.assertTrue(sender.delivered.wait(2))

        payload = json.loads(sender.calls[-1][1])
        status = sink.status()["endpoints"][0]
        self.assertTrue(payload["test"])
        self.assertEqual(payload["event"]["category"], "notification_test")
        self.assertIsNotNone(status["lastAttemptAt"])
        self.assertIsNotNone(status["lastSuccessAt"])

    def test_suppresses_an_unpaired_recovery_for_a_silenced_open(self) -> None:
        sender = _RecordingSender()
        sink = create_notification_sink(
            (self.config(max_attempts=1),),
            environ={
                "MOCOP_WEBHOOK_URL": "https://hooks.example.test/events",
                "MOCOP_WEBHOOK_SECRET": "top-secret",
            },
            resolver=resolver_for("8.8.8.8"),
            sender=sender,
        )
        recovered = replace(incident(), event_id=43, state="resolved")

        sink.publish((recovered,), ())
        sink.close()

        self.assertEqual(sender.calls, [])
        self.assertEqual(sink.status()["endpoints"][0]["droppedDeliveries"], 1)

    def test_severity_transitions_deliver_and_pair_without_prior_open(self) -> None:
        sender = _ImmediateSender()
        sink = create_notification_sink(
            (self.config(events=("opened", "resolved", "escalated", "deescalated")),),
            environ={
                "MOCOP_WEBHOOK_URL": "https://hooks.example.test/events",
                "MOCOP_WEBHOOK_SECRET": "top-secret",
            },
            resolver=resolver_for("8.8.8.8"),
            sender=sender,
        )
        escalated = replace(incident(), event_id=50, state="escalated")
        recovered = replace(incident(), event_id=51, state="resolved")
        unpaired = replace(
            incident(),
            event_id=52,
            state="resolved",
            condition=replace(incident().condition, key="cpu", category="cpu"),
        )

        sink.publish((escalated,), ())
        sink.publish((recovered,), ())
        sink.publish((unpaired,), ())
        sink.close()

        states = [json.loads(call[1])["event"]["state"] for call in sender.calls]
        self.assertEqual(states, ["escalated", "resolved"])
        status = sink.status()["endpoints"][0]
        self.assertEqual(status["deliveredEvents"], 2)
        self.assertEqual(status["droppedDeliveries"], 1)

    def test_actionable_check_runs_before_every_delivery_attempt(self) -> None:
        checks = []

        def actionable(event) -> bool:
            del event
            checks.append(True)
            return len(checks) == 1

        sender = _AlwaysRetryableSender()
        sink = create_notification_sink(
            (self.config(),),
            environ={
                "MOCOP_WEBHOOK_URL": "https://hooks.example.test/events",
                "MOCOP_WEBHOOK_SECRET": "top-secret",
            },
            resolver=resolver_for("8.8.8.8"),
            sender=sender,
            actionable_check=actionable,
        )

        sink.publish((incident(),), ())
        sink.close()

        self.assertEqual(len(sender.calls), 1)
        status = sink.status()["endpoints"][0]
        self.assertEqual(status["deliveredEvents"], 0)
        self.assertEqual(status["droppedDeliveries"], 1)
        self.assertIsNone(status["lastError"])

    def test_queue_full_event_can_be_retried_after_capacity_returns(self) -> None:
        sender = _GateSender()
        endpoint = _Endpoint(
            self.config(max_attempts=1),
            urlsplit("https://hooks.example.test/events"),
            None,
        )
        with patch("mocop.notifications._WEBHOOK_QUEUE_CAPACITY", 1):
            worker = _WebhookWorker(endpoint, sender)
        first = incident()
        second = replace(first, event_id=43)
        third = replace(first, event_id=44)
        worker.publish(NotificationEnvelope(first))
        self.assertTrue(sender.started.wait(1))
        worker.publish(NotificationEnvelope(second))
        worker.publish(NotificationEnvelope(third))
        test_envelope = NotificationEnvelope(replace(first, event_id=0), is_test=True)
        with worker._status_lock:
            self.assertFalse(worker._test_available_locked(time.monotonic()))

        sender.release.set()
        deadline = time.monotonic() + 1
        while len(sender.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        with worker._status_lock:
            now = time.monotonic()
            self.assertTrue(worker._test_available_locked(now))
            worker._queue_test_locked(test_envelope, now)
        while len(sender.calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.publish(NotificationEnvelope(third))
        worker.close(2)

        self.assertEqual(sender.calls, ["42", "43", "0", "44"])

    def test_worker_close_uses_one_deadline_and_reports_a_stuck_sender(self) -> None:
        sender = _GateSender()
        endpoint = _Endpoint(
            self.config(max_attempts=1),
            urlsplit("https://hooks.example.test/events"),
            None,
        )
        worker = _WebhookWorker(endpoint, sender)
        worker.publish(NotificationEnvelope(incident()))
        self.assertTrue(sender.started.wait(1))

        begun = time.monotonic()
        worker.close(0.1)
        elapsed = time.monotonic() - begun

        self.assertLess(elapsed, 0.16)
        self.assertEqual(
            worker.status()["lastError"],
            "notification worker did not stop cleanly",
        )
        sender.release.set()
        worker.close(1)
        self.assertFalse(worker._thread.is_alive())

    def test_multi_endpoint_test_is_queued_atomically(self) -> None:
        endpoint_a = _Endpoint(
            self.config(name="a"), urlsplit("https://a.example.test/events"), None
        )
        endpoint_b = _Endpoint(
            self.config(name="b"), urlsplit("https://b.example.test/events"), None
        )
        sink = WebhookNotificationSink(
            (endpoint_a, endpoint_b), sender=_ImmediateSender()
        )
        self.addCleanup(sink.close)
        blocked_worker = sink._workers[1]
        blocked_worker._last_test_queued_at = time.monotonic()

        self.assertFalse(sink.test())

        self.assertEqual(sink._workers[0]._queue.qsize(), 0)
        self.assertEqual(sink._workers[0]._last_test_queued_at, float("-inf"))

    def test_dns_timeouts_use_a_fixed_number_of_resolver_threads(self) -> None:
        release = threading.Event()

        def stuck_resolver(host, port, *, type):
            del host, type
            release.wait(2)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("mocop-webhook-resolver-")
        }
        sender = PinnedHttpsWebhookSender(resolver=stuck_resolver)
        endpoint = _Endpoint(
            self.config(timeout_seconds=0.02),
            urlsplit("https://hooks.example.test/events"),
            None,
        )
        try:
            for _index in range(24):
                self.assertEqual(
                    sender.send(endpoint, b"{}", {}),
                    DeliveryResult(False, True, "dns_resolution_failed"),
                )
            active = {
                thread.ident
                for thread in threading.enumerate()
                if thread.name.startswith("mocop-webhook-resolver-")
            }
            self.assertLessEqual(len(active - before), 2)
        finally:
            release.set()
            sender.close()

    def test_send_enforces_deadline_while_response_headers_trickle(self) -> None:
        client, peer = socket.socketpair()
        self.addCleanup(peer.close)

        def drip_headers() -> None:
            response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
            for byte in response:
                try:
                    peer.sendall(bytes((byte,)))
                except OSError:
                    return
                time.sleep(0.03)

        server = threading.Thread(target=drip_headers, daemon=True)
        server.start()
        sender = PinnedHttpsWebhookSender(
            resolver=resolver_for("8.8.8.8"),
            tls_context=_PassThroughTlsContext(),
            connect=lambda address, timeout=None: client,
        )
        self.addCleanup(sender.close)
        endpoint = _Endpoint(
            self.config(timeout_seconds=0.2),
            urlsplit("https://hooks.example.test/events"),
            None,
        )

        begun = time.monotonic()
        result = sender.send(endpoint, b"{}", {})
        elapsed = time.monotonic() - begun

        server.join(1)
        self.assertEqual(result, DeliveryResult(False, True, "network_or_tls_failure"))
        self.assertLess(elapsed, 0.8)

    def test_set_actionable_check_suppresses_queued_events_but_not_tests(self) -> None:
        sender = _ImmediateSender()
        sink = create_notification_sink(
            (self.config(),),
            environ={
                "MOCOP_WEBHOOK_URL": "https://hooks.example.test/events",
                "MOCOP_WEBHOOK_SECRET": "top-secret",
            },
            resolver=resolver_for("8.8.8.8"),
            sender=sender,
        )
        sink.set_actionable_check(lambda event: event.host != "gpu-01")

        sink.publish((incident(),), ())
        self.assertTrue(sink.test())
        sink.close()

        payloads = [json.loads(call[1]) for call in sender.calls]
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["test"])
        self.assertEqual(sink.status()["endpoints"][0]["droppedDeliveries"], 1)

    def test_send_bounds_dns_resolution_by_the_delivery_deadline(self) -> None:
        started = threading.Event()

        def stuck_resolver(host, port, *, type):
            del host, port, type
            started.set()
            time.sleep(2)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

        sender = PinnedHttpsWebhookSender(resolver=stuck_resolver)
        self.addCleanup(sender.close)
        endpoint = _Endpoint(
            self.config(timeout_seconds=0.2),
            urlsplit("https://hooks.example.test/events"),
            None,
        )

        begun = time.monotonic()
        result = sender.send(endpoint, b"{}", {})
        elapsed = time.monotonic() - begun

        self.assertTrue(started.wait(1))
        self.assertEqual(result, DeliveryResult(False, True, "dns_resolution_failed"))
        self.assertLess(elapsed, 1.0)

    def test_send_bounds_slow_response_bodies_by_the_deadline(self) -> None:
        tls_socket = _ScriptedTlsSocket(
            b"HTTP/1.1 200 OK\r\nContent-Length: 200000\r\n\r\n",
            body_length=200_000,
            chunk_size=1024,
            chunk_delay=0.08,
        )
        sender = PinnedHttpsWebhookSender(
            resolver=resolver_for("8.8.8.8"),
            tls_context=_ScriptedTlsContext(tls_socket),
            connect=lambda address, timeout=None: _ScriptedRawSocket(),
        )
        self.addCleanup(sender.close)
        endpoint = _Endpoint(
            self.config(timeout_seconds=0.3),
            urlsplit("https://hooks.example.test/events"),
            None,
        )

        begun = time.monotonic()
        result = sender.send(endpoint, b"{}", {})
        elapsed = time.monotonic() - begun

        self.assertEqual(result, DeliveryResult(True, False))
        self.assertLess(elapsed, 2.5)

    def test_send_fails_over_to_the_next_validated_address(self) -> None:
        tls_socket = _ScriptedTlsSocket(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        )
        context = _ScriptedTlsContext(tls_socket)
        attempted = []
        raw_socket = _ScriptedRawSocket()

        def connect(address, timeout=None):
            del timeout
            attempted.append(address[0])
            if address[0] == "9.9.9.9":
                raise ConnectionRefusedError("primary address is down")
            return raw_socket

        def resolver(host, port, *, type):
            del host
            return [
                (socket.AF_INET, type, 6, "", ("9.9.9.9", port)),
                (socket.AF_INET, type, 6, "", ("8.8.8.8", port)),
            ]

        sender = PinnedHttpsWebhookSender(
            resolver=resolver, tls_context=context, connect=connect
        )
        self.addCleanup(sender.close)
        endpoint = _Endpoint(
            self.config(), urlsplit("https://hooks.example.test/events"), None
        )

        result = sender.send(endpoint, b"{}", {"Content-Type": "application/json"})

        self.assertEqual(result, DeliveryResult(True, False))
        self.assertEqual(attempted, ["9.9.9.9", "8.8.8.8"])
        self.assertEqual(context.server_hostnames, ["hooks.example.test"])
        self.assertIn(b"Host: hooks.example.test", bytes(tls_socket.sent))


if __name__ == "__main__":
    unittest.main()
