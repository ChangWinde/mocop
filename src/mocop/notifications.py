from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import socket
import threading
import time
import zlib
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from . import __version__
from .config import WebhookConfig
from .incident_types import IncidentCondition, IncidentEvent
from .models import utc_now
from .webhook_transport import (
    AddressResolver,
    BoundedResolver,
    DeliveryResult,
    Endpoint,
    NotificationError,
    PinnedHttpsWebhookSender,
    WebhookSender,
    resolver_with_deadline,
    validated_addresses,
)

_WEBHOOK_QUEUE_CAPACITY = 1024
_WEBHOOK_SEEN_CAPACITY = 4096


@dataclass(frozen=True, slots=True)
class NotificationEnvelope:
    event: IncidentEvent
    correlation: dict[str, object] | None = None
    is_test: bool = False


ActionableCheck = Callable[[IncidentEvent], bool]


class IncidentNotificationSink(Protocol):
    def publish(
        self,
        events: tuple[IncidentEvent, ...],
        correlations: Sequence[dict[str, object]],
    ) -> None: ...

    def set_actionable_check(self, check: ActionableCheck | None) -> None: ...

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

    def set_actionable_check(self, check: ActionableCheck | None) -> None:
        del check

    def status(self) -> dict[str, object]:
        return {
            "enabled": False,
            "healthy": True,
            "queuedDeliveries": 0,
            "droppedDeliveries": 0,
            "suppressedDeliveries": 0,
            "endpoints": [],
        }

    def test(self) -> bool:
        return False

    def close(self, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds


class _WebhookWorker:
    def __init__(
        self,
        endpoint: Endpoint,
        sender: WebhookSender,
        actionable_check: ActionableCheck | None = None,
        known_conditions: Iterable[tuple[str, str]] = (),
    ) -> None:
        self._endpoint = endpoint
        self._sender = sender
        self._actionable_check = actionable_check
        self._queue: queue.Queue[NotificationEnvelope] = queue.Queue(
            _WEBHOOK_QUEUE_CAPACITY
        )
        self._stop = threading.Event()
        self._closing = threading.Event()
        self._status_lock = threading.Lock()
        self._seen_order: deque[int] = deque()
        self._seen: set[int] = set()
        self._dropped = 0
        self._suppressed = 0
        self._delivered = 0
        self._last_error: str | None = None
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._last_test_queued_at = float("-inf")
        self._closed = False
        self._active_conditions: OrderedDict[tuple[str, str], None] = OrderedDict()
        # Worker-thread state: once capacity eviction discards a pairing
        # record, table absence stops proving "the receiver never saw an
        # open", so unpaired-recovery suppression turns off for the rest of
        # this process (a spurious resolved is recoverable; a hanging alert
        # is not). Conditions restored as open at startup were opened, and
        # delivered, by an earlier process, so their recovery must pair.
        self._pairing_saturated = False
        for condition_key in known_conditions:
            self._active_conditions[condition_key] = None
        while len(self._active_conditions) > _WEBHOOK_SEEN_CAPACITY:
            self._active_conditions.popitem(last=False)
            self._pairing_saturated = True
        self._thread = threading.Thread(
            target=self._run,
            name=f"mocop-webhook-{endpoint.config.name}",
            daemon=True,
        )
        self._thread.start()

    def set_actionable_check(self, check: ActionableCheck | None) -> None:
        self._actionable_check = check

    def _still_actionable(self, envelope: NotificationEnvelope) -> bool:
        if envelope.is_test:
            return True
        check = self._actionable_check
        if check is None:
            return True
        try:
            return bool(check(envelope.event))
        except Exception:
            # A broken callback must not silence alerts or kill this worker.
            return True

    def publish(self, envelope: NotificationEnvelope) -> None:
        event = envelope.event
        if event.state not in self._endpoint.config.events:
            return
        with self._status_lock:
            if self._closed:
                self._dropped += 1
                self._last_error = "notification endpoint is closed"
                return
            if event.event_id in self._seen:
                return
            try:
                self._queue.put_nowait(envelope)
            except queue.Full:
                self._dropped += 1
                self._last_error = "delivery queue is full"
                return
            self._seen.add(event.event_id)
            self._seen_order.append(event.event_id)
            while len(self._seen_order) > _WEBHOOK_SEEN_CAPACITY:
                self._seen.discard(self._seen_order.popleft())

    def _test_available_locked(self, now: float) -> bool:
        return (
            not self._closed
            and now - self._last_test_queued_at >= 30
            and not self._queue.full()
        )

    def _queue_test_locked(self, envelope: NotificationEnvelope, now: float) -> None:
        self._queue.put_nowait(envelope)
        self._last_test_queued_at = now

    def status(self) -> dict[str, object]:
        with self._status_lock:
            return {
                "name": self._endpoint.config.name,
                "healthy": self._last_error is None and self._thread.is_alive(),
                "queuedDeliveries": self._queue.qsize(),
                "deliveredEvents": self._delivered,
                "droppedDeliveries": self._dropped,
                "suppressedDeliveries": self._suppressed,
                "lastError": self._last_error,
                "lastAttemptAt": self._last_attempt_at,
                "lastSuccessAt": self._last_success_at,
            }

    def close(self, timeout_seconds: float) -> None:
        timeout = max(0.0, timeout_seconds)
        deadline = time.monotonic() + timeout
        with self._status_lock:
            self._closed = True
        self._closing.set()
        # Reserve half the caller's budget for forced cancellation instead of
        # applying the full timeout twice.
        self._thread.join(timeout / 2)
        if self._thread.is_alive():
            self._stop.set()
            self._thread.join(max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive():
            with self._status_lock:
                self._last_error = "notification worker did not stop cleanly"

    def _run(self) -> None:
        next_delivery_at = 0.0
        while True:
            if self._closing.is_set() and self._queue.empty():
                return
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                condition_key = (item.event.host, item.event.condition.key)
                paired_delivery = not item.is_test and {"opened", "resolved"}.issubset(
                    self._endpoint.config.events
                )
                if (
                    paired_delivery
                    and item.event.state == "resolved"
                    and condition_key not in self._active_conditions
                    and not self._pairing_saturated
                ):
                    # A recovery for a condition the receiver never saw would
                    # only confuse it. This is a deliberate suppression (for
                    # example a recovery inside a maintenance window), not a
                    # delivery failure, so it is counted separately and never
                    # inflates droppedDeliveries or flips the healthy flag.
                    with self._status_lock:
                        self._suppressed += 1
                    continue
                body = self._payload(item)
                headers = self._headers(item.event, body)
                delivered = False
                suppressed = False
                last_error_code = "delivery_failed"
                for attempt in range(self._endpoint.config.max_attempts):
                    if attempt:
                        retry_delay = self._retry_delay(item.event.event_id, attempt)
                        if self._stop.wait(retry_delay):
                            return
                    throttle_delay = max(0.0, next_delivery_at - time.monotonic())
                    if self._stop.wait(throttle_delay):
                        return
                    if not self._still_actionable(item):
                        # The event stopped being actionable (for example a
                        # maintenance window started) while queued or retried.
                        suppressed = True
                        break
                    try:
                        with self._status_lock:
                            self._last_attempt_at = utc_now()
                        result = self._sender.send(self._endpoint, body, headers)
                    except Exception:
                        # A sender adapter failure must not terminate this endpoint.
                        result = DeliveryResult(False, True, "sender_internal_failure")
                    next_delivery_at = (
                        time.monotonic() + self._endpoint.config.min_interval_seconds
                    )
                    if result.success:
                        delivered = True
                        break
                    last_error_code = result.error_code or "delivery_failed"
                    if not result.retryable:
                        break
                with self._status_lock:
                    if delivered:
                        self._delivered += 1
                        self._last_error = None
                        self._last_success_at = utc_now()
                        if paired_delivery and item.event.state == "resolved":
                            self._active_conditions.pop(condition_key, None)
                        elif paired_delivery:
                            # opened, escalated, and deescalated all prove the
                            # receiver knows about this condition.
                            self._active_conditions[condition_key] = None
                            self._active_conditions.move_to_end(condition_key)
                            while len(self._active_conditions) > _WEBHOOK_SEEN_CAPACITY:
                                self._active_conditions.popitem(last=False)
                                self._pairing_saturated = True
                    else:
                        self._dropped += 1
                        if not suppressed:
                            self._last_error = last_error_code
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
        endpoints: tuple[Endpoint, ...],
        sender: WebhookSender | None = None,
        actionable_check: ActionableCheck | None = None,
        known_conditions: Iterable[tuple[str, str]] = (),
    ) -> None:
        selected_sender = sender or PinnedHttpsWebhookSender()
        self._sender = selected_sender
        known = tuple(known_conditions)
        workers = []
        try:
            for endpoint in endpoints:
                workers.append(
                    _WebhookWorker(endpoint, selected_sender, actionable_check, known)
                )
        except Exception:
            for worker in reversed(workers):
                worker.close(1.0)
            selected_sender.close()
            raise
        self._workers = tuple(workers)

    def set_actionable_check(self, check: ActionableCheck | None) -> None:
        """Re-check queued events with this callback right before delivery."""
        for worker in self._workers:
            worker.set_actionable_check(check)

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
            "suppressedDeliveries": sum(
                int(endpoint["suppressedDeliveries"]) for endpoint in endpoints
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
        now = time.monotonic()
        acquired: list[threading.Lock] = []
        try:
            for worker in self._workers:
                worker._status_lock.acquire()
                acquired.append(worker._status_lock)
            if not self._workers or not all(
                worker._test_available_locked(now) for worker in self._workers
            ):
                return False
            for worker in self._workers:
                worker._queue_test_locked(envelope, now)
            return True
        finally:
            for lock in reversed(acquired):
                lock.release()

    def close(self, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for worker in self._workers:
            worker.close(max(0.0, deadline - time.monotonic()))
        self._sender.close(max(0.0, deadline - time.monotonic()))


def _resolve_endpoints(
    configs: tuple[WebhookConfig, ...],
    environ: dict[str, str],
    resolver: BoundedResolver,
) -> tuple[Endpoint, ...]:
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
            # Environment strings may contain surrogateescaped bytes on Unix.
            # Reject them here so URL/DNS/TLS layers never leak raw Unicode
            # exceptions or partially initialize a configured sink.
            url.encode("utf-8")
            parsed = urlsplit(url)
            port = parsed.port
        except (ValueError, UnicodeError) as exc:
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
            try:
                encoded_secret = secret_value.encode("utf-8")
            except UnicodeError as exc:
                raise NotificationError(
                    f"webhook {config.name!r} signing secret is invalid"
                ) from exc
            if not encoded_secret or len(encoded_secret) > 4096:
                raise NotificationError(
                    f"webhook {config.name!r} signing secret is missing or too long"
                )
            secret = encoded_secret
        endpoint = Endpoint(config, parsed, secret)
        deadline = time.monotonic() + config.timeout_seconds
        validated_addresses(endpoint, resolver_with_deadline(resolver, deadline))
        endpoints.append(endpoint)
    return tuple(endpoints)


def create_notification_sink(
    configs: tuple[WebhookConfig, ...],
    *,
    environ: dict[str, str] | None = None,
    resolver: AddressResolver = socket.getaddrinfo,
    sender: WebhookSender | None = None,
    actionable_check: ActionableCheck | None = None,
    known_conditions: Iterable[tuple[str, str]] = (),
) -> IncidentNotificationSink:
    """Build the sink; ``known_conditions`` are ``(host, conditionKey)`` pairs
    whose opened transition an earlier process delivered (the incidents
    restored as open at startup), so their eventual resolved is not
    suppressed as unpaired."""
    if not configs:
        return DisabledNotificationSink()
    resolver_pool = BoundedResolver(resolver)
    try:
        endpoints = _resolve_endpoints(
            configs,
            dict(os.environ if environ is None else environ),
            resolver_pool,
        )
    except Exception:
        resolver_pool.close()
        raise
    if sender is None:
        selected_sender: WebhookSender = PinnedHttpsWebhookSender(
            resolver_pool=resolver_pool
        )
    else:
        resolver_pool.close()
        selected_sender = sender
    return WebhookNotificationSink(
        endpoints, selected_sender, actionable_check, known_conditions
    )
