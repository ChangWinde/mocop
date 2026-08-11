from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import queue
import socket
import ssl
import threading
import time
import zlib
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from . import __version__
from .config import WebhookConfig
from .incidents import IncidentCondition, IncidentEvent
from .models import utc_now

_WEBHOOK_QUEUE_CAPACITY = 1024
_WEBHOOK_RESPONSE_LIMIT_BYTES = 65_536
_WEBHOOK_SEEN_CAPACITY = 4096


class NotificationError(RuntimeError):
    """Raised when an explicitly configured notification target is unsafe."""


@dataclass(frozen=True, slots=True)
class _Endpoint:
    config: WebhookConfig
    parsed_url: SplitResult
    secret: bytes | None


@dataclass(frozen=True, slots=True)
class NotificationEnvelope:
    event: IncidentEvent
    correlation: dict[str, object] | None = None
    is_test: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    success: bool
    retryable: bool


class WebhookSender(Protocol):
    def send(
        self,
        endpoint: _Endpoint,
        body: bytes,
        headers: dict[str, str],
    ) -> DeliveryResult: ...


class IncidentNotificationSink(Protocol):
    def publish(
        self,
        events: tuple[IncidentEvent, ...],
        correlations: Sequence[dict[str, object]],
    ) -> None: ...

    def status(self) -> dict[str, object]: ...

    def test(self) -> bool: ...

    def close(self, timeout_seconds: float = 5.0) -> None: ...


class DisabledNotificationSink:
    def publish(
        self,
        events: tuple[IncidentEvent, ...],
        correlations: Sequence[dict[str, object]],
    ) -> None:
        del events, correlations

    def status(self) -> dict[str, object]:
        return {
            "enabled": False,
            "healthy": True,
            "queuedDeliveries": 0,
            "droppedDeliveries": 0,
            "endpoints": [],
        }

    def test(self) -> bool:
        return False

    def close(self, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds


AddressResolver = Callable[..., list[tuple[object, ...]]]


def _validated_addresses(
    endpoint: _Endpoint,
    resolver: AddressResolver,
) -> tuple[str, ...]:
    hostname = endpoint.parsed_url.hostname
    if hostname is None:
        raise NotificationError("webhook URL has no hostname")
    port = endpoint.parsed_url.port or 443
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NotificationError("webhook hostname cannot be resolved") from exc
    addresses = []
    for record in records:
        try:
            address = str(record[4][0])
            parsed = ipaddress.ip_address(address)
        except (IndexError, TypeError, ValueError):
            continue
        if not endpoint.config.allow_private_networks and not parsed.is_global:
            raise NotificationError("webhook hostname resolves to a non-public network")
        addresses.append(address)
    if not addresses:
        raise NotificationError("webhook hostname has no usable address")
    return tuple(dict.fromkeys(addresses))


class PinnedHttpsWebhookSender:
    """Send HTTPS while pinning the validated address for this request."""

    def __init__(
        self,
        *,
        resolver: AddressResolver = socket.getaddrinfo,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self._resolver = resolver
        self._tls_context = tls_context or ssl.create_default_context()

    def send(
        self,
        endpoint: _Endpoint,
        body: bytes,
        headers: dict[str, str],
    ) -> DeliveryResult:
        addresses = _validated_addresses(endpoint, self._resolver)
        hostname = endpoint.parsed_url.hostname
        assert hostname is not None
        port = endpoint.parsed_url.port or 443
        connection = http.client.HTTPSConnection(
            hostname,
            port,
            timeout=endpoint.config.timeout_seconds,
            context=self._tls_context,
        )
        raw_socket: socket.socket | None = None
        try:
            raw_socket = socket.create_connection(
                (addresses[0], port),
                timeout=endpoint.config.timeout_seconds,
            )
            connection.sock = self._tls_context.wrap_socket(
                raw_socket,
                server_hostname=hostname,
            )
            raw_socket = None
            target = endpoint.parsed_url.path or "/"
            if endpoint.parsed_url.query:
                target = f"{target}?{endpoint.parsed_url.query}"
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            response.read(_WEBHOOK_RESPONSE_LIMIT_BYTES + 1)
            if 200 <= response.status < 300:
                return DeliveryResult(True, False)
            return DeliveryResult(
                False,
                response.status in {408, 425, 429} or response.status >= 500,
            )
        except (OSError, ssl.SSLError, http.client.HTTPException):
            return DeliveryResult(False, True)
        finally:
            if raw_socket is not None:
                raw_socket.close()
            connection.close()


class _Stop:
    pass


class _WebhookWorker:
    def __init__(self, endpoint: _Endpoint, sender: WebhookSender) -> None:
        self._endpoint = endpoint
        self._sender = sender
        self._queue: queue.Queue[NotificationEnvelope | _Stop] = queue.Queue(
            _WEBHOOK_QUEUE_CAPACITY
        )
        self._stop = threading.Event()
        self._status_lock = threading.Lock()
        self._seen_order: deque[int] = deque()
        self._seen: set[int] = set()
        self._dropped = 0
        self._delivered = 0
        self._last_error: str | None = None
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._last_test_queued_at = float("-inf")
        self._active_conditions: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._thread = threading.Thread(
            target=self._run,
            name=f"mocop-webhook-{endpoint.config.name}",
            daemon=True,
        )
        self._thread.start()

    def publish(self, envelope: NotificationEnvelope) -> None:
        event = envelope.event
        if event.state not in self._endpoint.config.events:
            return
        with self._status_lock:
            if event.event_id in self._seen:
                return
            self._seen.add(event.event_id)
            self._seen_order.append(event.event_id)
            while len(self._seen_order) > _WEBHOOK_SEEN_CAPACITY:
                self._seen.discard(self._seen_order.popleft())
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            with self._status_lock:
                self._dropped += 1
                self._last_error = "delivery queue is full"

    def publish_test(self, envelope: NotificationEnvelope) -> bool:
        now = time.monotonic()
        with self._status_lock:
            if now - self._last_test_queued_at < 30:
                return False
            self._last_test_queued_at = now
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            with self._status_lock:
                self._dropped += 1
                self._last_error = "delivery queue is full"
            return False
        return True

    def status(self) -> dict[str, object]:
        with self._status_lock:
            return {
                "name": self._endpoint.config.name,
                "healthy": self._last_error is None and self._thread.is_alive(),
                "queuedDeliveries": self._queue.qsize(),
                "deliveredEvents": self._delivered,
                "droppedDeliveries": self._dropped,
                "lastError": self._last_error,
                "lastAttemptAt": self._last_attempt_at,
                "lastSuccessAt": self._last_success_at,
            }

    def close(self, timeout_seconds: float) -> None:
        timeout = max(0.0, timeout_seconds)
        try:
            self._queue.put(_Stop(), timeout=timeout)
        except queue.Full:
            self._stop.set()
            self._thread.join(timeout)
            return
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._stop.set()
            self._thread.join(min(1.0, timeout))

    def _run(self) -> None:
        next_delivery_at = 0.0
        while True:
            item = self._queue.get()
            try:
                if isinstance(item, _Stop):
                    return
                condition_key = (item.event.host, item.event.condition.key)
                paired_delivery = not item.is_test and {"opened", "resolved"}.issubset(
                    self._endpoint.config.events
                )
                if (
                    paired_delivery
                    and item.event.state != "opened"
                    and condition_key not in self._active_conditions
                ):
                    continue
                body = self._payload(item)
                headers = self._headers(item.event, body)
                delivered = False
                for attempt in range(self._endpoint.config.max_attempts):
                    if attempt:
                        retry_delay = self._retry_delay(item.event.event_id, attempt)
                        if self._stop.wait(retry_delay):
                            return
                    throttle_delay = max(0.0, next_delivery_at - time.monotonic())
                    if self._stop.wait(throttle_delay):
                        return
                    try:
                        with self._status_lock:
                            self._last_attempt_at = utc_now()
                        result = self._sender.send(self._endpoint, body, headers)
                    except Exception:
                        # A sender adapter failure must not terminate this endpoint.
                        result = DeliveryResult(False, True)
                    next_delivery_at = (
                        time.monotonic() + self._endpoint.config.min_interval_seconds
                    )
                    if result.success:
                        delivered = True
                        break
                    if not result.retryable:
                        break
                with self._status_lock:
                    if delivered:
                        self._delivered += 1
                        self._last_error = None
                        self._last_success_at = utc_now()
                        if paired_delivery and item.event.state == "opened":
                            self._active_conditions[condition_key] = None
                            self._active_conditions.move_to_end(condition_key)
                            while len(self._active_conditions) > _WEBHOOK_SEEN_CAPACITY:
                                self._active_conditions.popitem(last=False)
                        elif paired_delivery and item.event.state == "resolved":
                            self._active_conditions.pop(condition_key, None)
                    else:
                        self._dropped += 1
                        self._last_error = "delivery failed"
            finally:
                self._queue.task_done()

    def _retry_delay(self, event_id: int, attempt: int) -> float:
        jitter = (
            zlib.crc32(f"{self._endpoint.config.name}\0{event_id}".encode())
            / 0xFFFFFFFF
        )
        return (
            self._endpoint.config.retry_base_seconds
            * (2 ** (attempt - 1))
            * (0.85 + 0.3 * jitter)
        )

    @staticmethod
    def _payload(envelope: NotificationEnvelope) -> bytes:
        event = envelope.event.to_dict()
        payload: dict[str, object] = {
            "schemaVersion": 1,
            "source": "mocop",
            "sourceVersion": __version__,
            "event": event,
        }
        if envelope.correlation is not None:
            payload["correlation"] = envelope.correlation
        if envelope.is_test:
            payload["test"] = True
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _headers(self, event: IncidentEvent, body: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"mocop/{__version__}",
            "X-Mocop-Event-ID": str(event.event_id),
        }
        if self._endpoint.secret is not None:
            signature = hmac.new(
                self._endpoint.secret, body, hashlib.sha256
            ).hexdigest()
            headers["X-Mocop-Signature"] = f"sha256={signature}"
        return headers


class WebhookNotificationSink:
    def __init__(
        self,
        endpoints: tuple[_Endpoint, ...],
        sender: WebhookSender | None = None,
    ) -> None:
        selected_sender = sender or PinnedHttpsWebhookSender()
        self._workers = tuple(
            _WebhookWorker(endpoint, selected_sender) for endpoint in endpoints
        )

    def publish(
        self,
        events: tuple[IncidentEvent, ...],
        correlations: Sequence[dict[str, object]],
    ) -> None:
        for event in events:
            correlation = next(
                (
                    dict(item)
                    for item in correlations
                    if event.host in item.get("hosts", ())
                ),
                None,
            )
            envelope = NotificationEnvelope(event, correlation)
            for worker in self._workers:
                worker.publish(envelope)

    def status(self) -> dict[str, object]:
        endpoints = [worker.status() for worker in self._workers]
        return {
            "enabled": bool(endpoints),
            "healthy": all(bool(endpoint["healthy"]) for endpoint in endpoints),
            "queuedDeliveries": sum(
                int(endpoint["queuedDeliveries"]) for endpoint in endpoints
            ),
            "droppedDeliveries": sum(
                int(endpoint["droppedDeliveries"]) for endpoint in endpoints
            ),
            "endpoints": endpoints,
        }

    def test(self) -> bool:
        observed_at = utc_now()
        event = IncidentEvent(
            event_id=0,
            host="mocop",
            condition=IncidentCondition(
                key="notification_test",
                category="notification_test",
                resource="Webhook delivery",
                severity="warning",
                value=None,
                threshold=None,
                observed_at=observed_at,
                detail="Operator-requested delivery test",
                open_after_cycles=1,
                recovery_cycles=1,
            ),
            state="opened",
            observed_at=observed_at,
        )
        envelope = NotificationEnvelope(event, is_test=True)
        queued = [worker.publish_test(envelope) for worker in self._workers]
        return any(queued)

    def close(self, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for worker in self._workers:
            worker.close(max(0.0, deadline - time.monotonic()))


def _resolve_endpoints(
    configs: tuple[WebhookConfig, ...],
    environ: dict[str, str],
    resolver: AddressResolver,
) -> tuple[_Endpoint, ...]:
    endpoints = []
    for config in configs:
        url = environ.get(config.url_env, "").strip()
        if not url:
            raise NotificationError(
                f"webhook {config.name!r} URL environment variable is missing"
            )
        if len(url) > 2048:
            raise NotificationError(f"webhook {config.name!r} URL is too long")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise NotificationError(f"webhook {config.name!r} URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise NotificationError(
                f"webhook {config.name!r} must use a credential-free HTTPS URL"
            )
        secret: bytes | None = None
        if config.secret_env is not None:
            secret_value = environ.get(config.secret_env, "")
            if not secret_value or len(secret_value.encode("utf-8")) > 4096:
                raise NotificationError(
                    f"webhook {config.name!r} signing secret is missing or too long"
                )
            secret = secret_value.encode("utf-8")
        endpoint = _Endpoint(config, parsed, secret)
        _validated_addresses(endpoint, resolver)
        endpoints.append(endpoint)
    return tuple(endpoints)


def create_notification_sink(
    configs: tuple[WebhookConfig, ...],
    *,
    environ: dict[str, str] | None = None,
    resolver: AddressResolver = socket.getaddrinfo,
    sender: WebhookSender | None = None,
) -> IncidentNotificationSink:
    if not configs:
        return DisabledNotificationSink()
    endpoints = _resolve_endpoints(
        configs,
        dict(os.environ if environ is None else environ),
        resolver,
    )
    return WebhookNotificationSink(endpoints, sender)
