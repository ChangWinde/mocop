"""Bounded HTTPS delivery for webhooks: pinned DNS, SSRF guards, one attempt.

Every address a webhook hostname resolves to is validated before use and the
connection is made to that exact address with the hostname kept for TLS and
``Host``; resolution runs on a small bounded pool so a stalled resolver cannot
hold a delivery worker. The sender performs exactly one attempt per call and
reports whether the failure is worth retrying; retry, throttle, and pairing
policy live in ``notifications.py``.
"""

from __future__ import annotations

import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult

from .config import WebhookConfig

WEBHOOK_RESPONSE_LIMIT_BYTES = 65_536
RESOLVER_QUEUE_CAPACITY = 16
RESOLVER_WORKERS = 2


class NotificationError(RuntimeError):
    """Raised when an explicitly configured notification target is unsafe."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    config: WebhookConfig
    parsed_url: SplitResult
    secret: bytes | None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    success: bool
    retryable: bool
    error_code: str | None = None


class WebhookSender(Protocol):
    def send(
        self,
        endpoint: Endpoint,
        body: bytes,
        headers: dict[str, str],
    ) -> DeliveryResult: ...

    def close(self, timeout_seconds: float = 0.0) -> None: ...


AddressResolver = Callable[..., list[tuple[object, ...]]]


def validated_addresses(
    endpoint: Endpoint,
    resolver: AddressResolver,
) -> tuple[str, ...]:
    hostname = endpoint.parsed_url.hostname
    if hostname is None:
        raise NotificationError("webhook URL has no hostname")
    port = endpoint.parsed_url.port or 443
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
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
    return tuple(dict.fromkeys(addresses))[:64]


@dataclass(frozen=True, slots=True)
class _ResolutionTask:
    args: tuple[object, ...]
    kwargs: dict[str, object]
    outcome: queue.SimpleQueue[object]


class _ResolverStop:
    pass


class BoundedResolver:
    """Run blocking DNS calls on a fixed-size, bounded daemon pool."""

    def __init__(self, resolver: AddressResolver) -> None:
        self._resolver = resolver
        self._queue: queue.Queue[_ResolutionTask | _ResolverStop] = queue.Queue(
            RESOLVER_QUEUE_CAPACITY
        )
        self._lock = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        self._workers = tuple(
            threading.Thread(
                target=self._run,
                name=f"mocop-webhook-resolver-{index + 1}",
                daemon=True,
            )
            for index in range(RESOLVER_WORKERS)
        )
        for worker in self._workers:
            worker.start()

    def resolve(
        self, deadline: float, *args: object, **kwargs: object
    ) -> list[tuple[object, ...]]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("webhook DNS resolution deadline exceeded")
        outcome: queue.SimpleQueue[object] = queue.SimpleQueue()
        task = _ResolutionTask(args, dict(kwargs), outcome)
        with self._lock:
            if self._closed:
                raise TimeoutError("webhook DNS resolver is closed")
        try:
            self._queue.put(task, timeout=remaining)
        except queue.Full:
            raise TimeoutError("webhook DNS resolution queue is full") from None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("webhook DNS resolution deadline exceeded")
        try:
            value = task.outcome.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("webhook DNS resolution deadline exceeded") from None
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]

    def close(self, timeout_seconds: float = 0.2) -> None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._lock:
            self._closed = True
            self._stop.set()
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(task, _ResolutionTask):
                task.outcome.put(TimeoutError("webhook DNS resolver is closed"))
            self._queue.task_done()
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))

    def _run(self) -> None:
        while True:
            if self._stop.is_set() and self._queue.empty():
                return
            try:
                task = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if isinstance(task, _ResolverStop):
                    return
                try:
                    task.outcome.put(self._resolver(*task.args, **task.kwargs))
                except Exception as exc:
                    task.outcome.put(exc)
            finally:
                self._queue.task_done()


def resolver_with_deadline(
    resolver: BoundedResolver, deadline: float
) -> AddressResolver:
    def resolve(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        return resolver.resolve(deadline, *args, **kwargs)

    return resolve


class PinnedHttpsWebhookSender:
    """Send HTTPS to validated addresses under one delivery deadline.

    `timeout_seconds` bounds the whole attempt: DNS resolution, connecting,
    the TLS handshake, sending, and reading the response all draw from the
    same monotonic deadline. Every validated address is tried in order until
    one produces an HTTP response or the deadline expires.
    """

    def __init__(
        self,
        *,
        resolver: AddressResolver = socket.getaddrinfo,
        tls_context: ssl.SSLContext | None = None,
        connect: Callable[..., socket.socket] = socket.create_connection,
        resolver_pool: BoundedResolver | None = None,
    ) -> None:
        self._resolver_pool = resolver_pool or BoundedResolver(resolver)
        self._tls_context = tls_context or ssl.create_default_context()
        self._connect = connect

    def send(
        self,
        endpoint: Endpoint,
        body: bytes,
        headers: dict[str, str],
    ) -> DeliveryResult:
        deadline = time.monotonic() + endpoint.config.timeout_seconds
        try:
            addresses = validated_addresses(
                endpoint, resolver_with_deadline(self._resolver_pool, deadline)
            )
        except NotificationError as exc:
            code = (
                "unsafe_destination"
                if "non-public network" in str(exc) or "no usable address" in str(exc)
                else "dns_resolution_failed"
            )
            return DeliveryResult(False, True, code)
        hostname = endpoint.parsed_url.hostname
        assert hostname is not None
        port = endpoint.parsed_url.port or 443
        for address in addresses:
            if deadline - time.monotonic() <= 0:
                break
            status = self._request_status(
                endpoint, address, hostname, port, body, headers, deadline
            )
            if status is None:
                continue
            if 200 <= status < 300:
                return DeliveryResult(True, False)
            return DeliveryResult(
                False,
                status in {408, 425, 429} or status >= 500,
                f"http_{status}",
            )
        return DeliveryResult(False, True, "network_or_tls_failure")

    def close(self, timeout_seconds: float = 0.2) -> None:
        self._resolver_pool.close(timeout_seconds)

    def _request_status(
        self,
        endpoint: Endpoint,
        address: str,
        hostname: str,
        port: int,
        body: bytes,
        headers: dict[str, str],
        deadline: float,
    ) -> int | None:
        """POST via one pinned address; None means no HTTP response arrived."""
        connection = http.client.HTTPSConnection(
            hostname,
            port,
            timeout=endpoint.config.timeout_seconds,
            context=self._tls_context,
        )
        raw_socket: socket.socket | None = None
        watchdog = threading.Timer(
            max(0.0, deadline - time.monotonic()),
            lambda: self._abort_request(connection, raw_socket),
        )
        watchdog.daemon = True
        watchdog.start()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw_socket = self._connect((address, port), timeout=remaining)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw_socket.settimeout(remaining)
            connection.sock = self._tls_context.wrap_socket(
                raw_socket,
                server_hostname=hostname,
            )
            raw_socket = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            connection.sock.settimeout(remaining)
            target = endpoint.parsed_url.path or "/"
            if endpoint.parsed_url.query:
                target = f"{target}?{endpoint.parsed_url.query}"
            connection.request("POST", target, body=body, headers=headers)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            connection.sock.settimeout(remaining)
            response = connection.getresponse()
            self._drain(connection, response, deadline)
            return response.status
        except (OSError, ssl.SSLError, http.client.HTTPException):
            return None
        finally:
            watchdog.cancel()
            watchdog.join()
            if raw_socket is not None:
                raw_socket.close()
            connection.close()

    @staticmethod
    def _abort_request(
        connection: http.client.HTTPSConnection,
        raw_socket: socket.socket | None,
    ) -> None:
        active_socket = connection.sock or raw_socket
        if active_socket is not None:
            with suppress(OSError, AttributeError):
                active_socket.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                active_socket.close()
        connection.close()

    @staticmethod
    def _drain(
        connection: http.client.HTTPSConnection,
        response: http.client.HTTPResponse,
        deadline: float,
    ) -> None:
        """Read a bounded response body without outliving the deadline."""
        remaining_bytes = WEBHOOK_RESPONSE_LIMIT_BYTES + 1
        try:
            while remaining_bytes > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read(min(8192, remaining_bytes))
                if not chunk:
                    return
                remaining_bytes -= len(chunk)
        except (OSError, ssl.SSLError, http.client.HTTPException):
            # The status line already arrived; a stalled body is tolerable.
            return
