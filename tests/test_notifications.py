from __future__ import annotations

import json
import socket
import threading
import unittest
from dataclasses import replace

from mocop.config import WebhookConfig
from mocop.incidents import IncidentCondition, IncidentEvent
from mocop.notifications import (
    DeliveryResult,
    NotificationError,
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


if __name__ == "__main__":
    unittest.main()
