from __future__ import annotations

import hashlib
import json
import math
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import SplitResult, parse_qs, urlsplit

from . import __version__
from .config import (
    is_safe_alias,
    is_valid_host_group,
    is_valid_incident_action_reason,
    is_valid_incident_condition_key,
    is_valid_maintenance_reason,
    normalize_web_hostname,
)
from .inventory import (
    DASHBOARD_INCIDENT_ACTION_DURATIONS,
    DASHBOARD_MAINTENANCE_DURATIONS,
    DashboardConfigController,
    InventoryError,
    InventoryRequestError,
)
from .metrics import OPENMETRICS_CONTENT_TYPE, render_openmetrics
from .service import ProbeControl, StateStore

_STATIC_ROOT = Path(__file__).with_name("static")
_STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
_MAX_SETTINGS_BODY_BYTES = 128
_MAX_COLLECTOR_BODY_BYTES = 512
_MAX_INVENTORY_BODY_BYTES = 512
_MAX_MAINTENANCE_BODY_BYTES = 512
_MAX_HOST_GROUP_BODY_BYTES = 512
_MAX_RESTART_BODY_BYTES = 32
_MAX_INCIDENT_ACTION_BODY_BYTES = 1024
_MAX_PROBE_BODY_BYTES = 512
_MAX_NOTIFICATION_TEST_BODY_BYTES = 32
_COLLECTOR_SETTINGS_KEYS = {
    "pollIntervalSeconds",
    "probeTimeoutSeconds",
    "maxWorkers",
}
_API_VERSION = "1"
_API_SCHEMA_VERSION = 1
# Single source of truth for the HTTP API surface. `/api/meta` serializes this
# manifest and the JSON 404/405 fallbacks consult it, so a route change must
# land here to stay visible; tests compare it against live routing behavior.
# Access levels: listener = open read, reader = dashboard-marked read,
# writer = same-origin dashboard write.
API_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/api/snapshot", "listener"),
    ("GET", "/api/events", "listener"),
    ("GET", "/api/history", "listener"),
    ("GET", "/api/incidents", "listener"),
    ("GET", "/api/meta", "listener"),
    ("GET", "/api/service", "listener"),
    ("GET", "/healthz", "listener"),
    ("GET", "/readyz", "listener"),
    ("GET", "/metrics", "listener"),
    ("GET", "/api/gpu-history", "reader"),
    ("GET", "/api/diagnostics", "reader"),
    ("GET", "/api/inventory", "reader"),
    ("GET", "/api/topology", "reader"),
    ("POST", "/api/settings/collector", "writer"),
    ("POST", "/api/settings/poll-interval", "writer"),
    ("POST", "/api/settings/hosts", "writer"),
    ("POST", "/api/settings/maintenance", "writer"),
    ("POST", "/api/settings/host-group", "writer"),
    ("POST", "/api/settings/incident-action", "writer"),
    ("POST", "/api/probe", "writer"),
    ("POST", "/api/notifications/test", "writer"),
    ("POST", "/api/service/restart", "writer"),
)
_API_ENDPOINTS: tuple[dict[str, str], ...] = tuple(
    {"method": method, "path": path, "access": access}
    for method, path, access in API_ROUTES
)
_ROUTE_METHODS: dict[str, frozenset[str]] = {
    path: frozenset(
        route_method for route_method, route_path, _ in API_ROUTES if route_path == path
    )
    for _, path, _ in API_ROUTES
}
_WRITE_BODY_LIMITS = {
    "/api/settings/poll-interval": _MAX_SETTINGS_BODY_BYTES,
    "/api/settings/collector": _MAX_COLLECTOR_BODY_BYTES,
    "/api/settings/hosts": _MAX_INVENTORY_BODY_BYTES,
    "/api/settings/maintenance": _MAX_MAINTENANCE_BODY_BYTES,
    "/api/settings/host-group": _MAX_HOST_GROUP_BODY_BYTES,
    "/api/service/restart": _MAX_RESTART_BODY_BYTES,
    "/api/settings/incident-action": _MAX_INCIDENT_ACTION_BODY_BYTES,
    "/api/probe": _MAX_PROBE_BODY_BYTES,
    "/api/notifications/test": _MAX_NOTIFICATION_TEST_BODY_BYTES,
}
_DEPRECATED_ENDPOINT_HEADERS = (("Deprecation", "true"),)
# Hostnames a browser can present when it genuinely reached this server over
# the loopback interface. DNS rebinding presents the attacker's own domain in
# Host/Origin instead, so pinning these names closes the rebinding bypass.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_WILDCARD_BIND_HOSTS = frozenset({"", "0.0.0.0", "::"})
_SSE_HEARTBEAT_SECONDS = 15.0
# SSE loops wake at this cadence to notice the server shutdown event.
_SSE_STOP_POLL_SECONDS = 1.0
_SSE_SNAPSHOT_PREFIX = b"event: snapshot\ndata: "
_SSE_HEARTBEAT_FRAME = b"event: heartbeat\ndata: {}\n\n"
_SERVICE_UNAVAILABLE_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
)


def _is_api_family_path(path: str) -> bool:
    """Paths whose failures must stay JSON instead of the HTML error page."""
    if path == "/api" or path.startswith("/api/"):
        return True
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in ("/healthz", "/readyz", "/metrics")
    )


def _allowed_methods_header(path: str) -> str:
    methods = _ROUTE_METHODS[path]
    allowed = []
    if "GET" in methods:
        allowed.append("GET")
        if path != "/api/events":  # the event stream refuses HEAD
            allowed.append("HEAD")
    if "POST" in methods:
        allowed.append("POST")
    return ", ".join(allowed)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _trusted_hostnames(
    bind_host: str, trusted_hosts: Iterable[str] | None
) -> frozenset[str]:
    """Hostnames accepted in Host/Origin for writes and protected reads."""
    trusted = set(_LOOPBACK_HOSTNAMES)
    if str(bind_host).strip().lower() not in _WILDCARD_BIND_HOSTS:
        bind_hostname = normalize_web_hostname(bind_host)
        if bind_hostname is not None:
            trusted.add(bind_hostname)
    for candidate in trusted_hosts or ():
        hostname = normalize_web_hostname(candidate)
        if hostname is None:
            raise ValueError(f"invalid trusted web host: {candidate!r}")
        trusted.add(hostname)
    return frozenset(trusted)


class MonitorHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Slow or hostile clients hold a worker thread and a file descriptor each,
    # so refuse connections beyond these bounds instead of growing forever.
    max_concurrent_connections = 64
    max_sse_clients = 16

    def __init__(
        self,
        address: tuple[str, int],
        state: StateStore,
        inventory: DashboardConfigController | None = None,
        restart: Callable[[], None] | None = None,
        probe_control: ProbeControl | None = None,
        *,
        trusted_hosts: Iterable[str] | None = None,
    ) -> None:
        self.state = state
        self.inventory = inventory
        self.restart = restart
        self.probe_control = probe_control
        self.trusted_hostnames = _trusted_hostnames(address[0], trusted_hosts)
        self.shutdown_event = threading.Event()
        self._connection_slots = threading.BoundedSemaphore(
            self.max_concurrent_connections
        )
        self._sse_slots = threading.BoundedSemaphore(self.max_sse_clients)
        self._snapshot_cache_lock = threading.Lock()
        self._snapshot_cache_key: tuple[object, ...] | None = None
        self._snapshot_cache_payload = b""
        self._snapshot_cache_frame = b""
        super().__init__(address, MonitorRequestHandler)

    def process_request(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        if not self._connection_slots.acquire(blocking=False):
            # Writing this short canned response cannot block the accept loop:
            # a fresh socket always has send-buffer room for it.
            with suppress(OSError):
                request.sendall(_SERVICE_UNAVAILABLE_RESPONSE)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def server_close(self) -> None:
        # Wake live SSE loops so their worker threads exit promptly instead of
        # surviving until the next heartbeat write fails.
        self.shutdown_event.set()
        super().server_close()

    def _snapshot_cache(self, snapshot: dict[str, object]) -> tuple[bytes, bytes]:
        persistence = snapshot.get("persistence", {})
        notifications = snapshot.get("notifications", {})
        key = (
            snapshot.get("version"),
            snapshot.get("incidentVersion"),
            repr(persistence),
            repr(notifications),
        )
        with self._snapshot_cache_lock:
            if key != self._snapshot_cache_key:
                payload = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._snapshot_cache_payload = payload
                # The framed copy is cached too so each SSE client write
                # reuses one bytes object instead of concatenating per send.
                self._snapshot_cache_frame = _SSE_SNAPSHOT_PREFIX + payload + b"\n\n"
                self._snapshot_cache_key = key
            return self._snapshot_cache_payload, self._snapshot_cache_frame

    def snapshot_payload(self, snapshot: dict[str, object]) -> bytes:
        return self._snapshot_cache(snapshot)[0]

    def snapshot_frame(self, snapshot: dict[str, object]) -> bytes:
        return self._snapshot_cache(snapshot)[1]

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        """Ignore expected client disconnects while preserving real server errors."""
        error = sys.exc_info()[1]
        if isinstance(
            error, BrokenPipeError | ConnectionResetError | ConnectionAbortedError
        ):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server_version = f"mocop/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # Headers and JSON are separate writes; avoid delayed ACK stalls on reused sockets.
    disable_nagle_algorithm = True
    # Socket read/write deadline: stalled or half-open clients must release
    # their worker thread instead of holding it forever.
    timeout = 60.0
    _head_only = False

    def version_string(self) -> str:
        return self.server_version

    @property
    def monitor_server(self) -> MonitorHttpServer:
        return self.server  # type: ignore[return-value]

    def _read_only_snapshot(self) -> dict[str, object]:
        """State projection for pure serialization; never mutated by callers.

        The service layer is introducing StateStore.snapshot_view() (a
        read-only reference to the internal cached projection) in parallel;
        fall back to the deep-copying snapshot() until it exists.
        """
        state = self.monitor_server.state
        return getattr(state, "snapshot_view", state.snapshot)()

    def _split_request_target(self) -> SplitResult | None:
        """Parse the request target, mapping malformed URLs to a JSON 400."""
        try:
            return urlsplit(self.path)
        except ValueError:
            self.close_connection = True
            self._send_error(
                "invalid request target",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_REQUEST_TARGET",
            )
            return None

    def _refuse_request_body(self) -> bool:
        """Reject GET/HEAD requests that declare a body.

        The handler would never read such a body, so on a keep-alive
        connection its bytes would be parsed as the next request and could
        desynchronize request/response pairing behind a reverse proxy.
        """
        declared = self.headers.get_all("Content-Length") or []
        ambiguous = (
            self.headers.get("Transfer-Encoding") is not None or len(declared) > 1
        )
        if not ambiguous and declared:
            length = declared[0].strip()
            ambiguous = not (length.isascii() and length.isdigit()) or int(length) != 0
        if ambiguous:
            self.close_connection = True
            self._send_error(
                "request body is not allowed",
                HTTPStatus.BAD_REQUEST,
                code="REQUEST_BODY_NOT_ALLOWED",
            )
        return ambiguous

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._head_only = False
        self._respond_to_read_request()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._head_only = True
        self._respond_to_read_request()

    def _respond_to_read_request(self) -> None:
        if self._refuse_request_body():
            return
        request_url = self._split_request_target()
        if request_url is None:
            return
        # Any dashboard-marked read (snapshot polling included) is a live
        # viewer; the event stream marks presence separately because
        # EventSource cannot attach the marker header.
        if self.headers.get("X-Monitor-Request") == "dashboard":
            self.monitor_server.state.record_dashboard_activity()
        path = request_url.path
        if path == "/api/snapshot":
            snapshot = self._read_only_snapshot()
            self._send_json_payload(self.monitor_server.snapshot_payload(snapshot))
            return
        if path == "/api/events":
            if self._head_only:
                self._send_error(
                    "method not allowed",
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    code="METHOD_NOT_ALLOWED",
                    extra_headers=(("Allow", "GET"),),
                )
                return
            self._send_events()
            return
        if path == "/api/history":
            self._send_history(request_url.query)
            return
        if path == "/api/gpu-history":
            self._send_gpu_history(request_url.query)
            return
        if path == "/api/incidents":
            self._send_incidents(request_url.query)
            return
        if path == "/api/inventory":
            self._send_inventory(request_url.query)
            return
        if path == "/api/topology":
            self._send_topology(request_url.query)
            return
        if path == "/api/diagnostics":
            self._send_diagnostics(request_url.query)
            return
        if path == "/api/meta":
            self._send_meta(request_url.query)
            return
        if path == "/api/service":
            if request_url.query:
                self._send_error(
                    "query parameters are not allowed",
                    HTTPStatus.BAD_REQUEST,
                    code="QUERY_NOT_ALLOWED",
                )
                return
            # Deprecated alias of the /api/meta capabilities block.
            self._send_json(
                {"restartSupported": self.monitor_server.restart is not None},
                extra_headers=_DEPRECATED_ENDPOINT_HEADERS,
            )
            return
        if path == "/metrics":
            if request_url.query:
                self._send_error(
                    "query parameters are not allowed",
                    HTTPStatus.BAD_REQUEST,
                    code="QUERY_NOT_ALLOWED",
                )
                return
            self._send_openmetrics()
            return
        if path == "/healthz":
            health = self.monitor_server.state.health()
            self._send_json(
                {
                    "status": "ok",
                    "ready": health["ready"],
                    "transportRetries": health["transportRetries"],
                }
            )
            return
        if path == "/readyz":
            health = self.monitor_server.state.health()
            self._send_json(
                health,
                HTTPStatus.OK if health["ready"] else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        static = _STATIC_ROUTES.get(path)
        if static is None:
            # HEAD mirrors GET, so the method to match against stays GET.
            self._send_route_fallback("GET", path)
            return
        self._send_static(*static)

    def _send_static(self, filename: str, content_type: str) -> None:
        try:
            payload = (_STATIC_ROOT / filename).read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        # Strong content validator: revalidation stays correct across restarts
        # and deployments because it depends only on the bytes served.
        etag = f'"{hashlib.sha256(payload).hexdigest()}"'
        if self._client_cache_is_current(etag):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self._common_headers(content_type, cache="no-cache")
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self._common_headers(content_type, cache="no-cache")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write_body(payload)

    def _client_cache_is_current(self, etag: str) -> bool:
        header = self.headers.get("If-None-Match")
        if header is None:
            return False
        if header.strip() == "*":
            return True
        # If-None-Match uses the weak comparison: a W/ prefix still matches.
        candidates = {value.strip().removeprefix("W/") for value in header.split(",")}
        return etag in candidates

    def _send_meta(self, query: str) -> None:
        if query:
            self._send_error(
                "query parameters are not allowed",
                HTTPStatus.BAD_REQUEST,
                code="QUERY_NOT_ALLOWED",
            )
            return
        server = self.monitor_server
        self._send_json(
            {
                "apiVersion": _API_VERSION,
                "appVersion": __version__,
                "schemaVersion": _API_SCHEMA_VERSION,
                "capabilities": {
                    "restartSupported": server.restart is not None,
                    "manualProbeSupported": server.probe_control is not None,
                    "configurationWriteSupported": (
                        self._configuration_write_supported()
                    ),
                },
                "endpoints": list(_API_ENDPOINTS),
            }
        )

    def _configuration_write_supported(self) -> bool:
        """Writable-config capability without scanning or connecting over SSH."""
        inventory = self.monitor_server.inventory
        if inventory is None:
            return False
        writable = getattr(inventory, "writable", None)
        if not callable(writable):
            # Controllers predating the lightweight check degrade to existence.
            return True
        return bool(writable())

    def _send_route_fallback(self, method: str, path: str) -> None:
        """JSON 404/405 for API-family paths; static paths keep the HTML page."""
        if not _is_api_family_path(path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        methods = _ROUTE_METHODS.get(path)
        if methods and method not in methods:
            self._send_error(
                "method not allowed",
                HTTPStatus.METHOD_NOT_ALLOWED,
                code="METHOD_NOT_ALLOWED",
                extra_headers=(("Allow", _allowed_methods_header(path)),),
            )
            return
        self._send_error("unknown API path", HTTPStatus.NOT_FOUND, code="NOT_FOUND")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # Settings writes are rare, and invalid requests may intentionally leave an
        # unread body. Closing this HTTP/1.1 connection prevents those bytes from
        # being parsed as a second request on the same socket.
        self.close_connection = True
        request_url = self._split_request_target()
        if request_url is None:
            return
        body_limit = _WRITE_BODY_LIMITS.get(request_url.path)
        if body_limit is None:
            self._send_route_fallback("POST", request_url.path)
            return
        if request_url.query:
            self._send_error(
                "query parameters are not allowed",
                HTTPStatus.BAD_REQUEST,
                code="QUERY_NOT_ALLOWED",
            )
            return
        if not self._is_dashboard_request():
            self._send_error(
                "same-origin dashboard request required",
                HTTPStatus.FORBIDDEN,
                code="UNTRUSTED_ORIGIN",
            )
            return
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            self._send_error(
                "application/json required",
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                code="UNSUPPORTED_MEDIA_TYPE",
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = 0
        if not 1 <= content_length <= body_limit:
            self._send_error(
                "invalid request body size",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code="PAYLOAD_TOO_LARGE",
            )
            return
        try:
            payload = json.loads(
                self.rfile.read(content_length).decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_error(
                "invalid JSON body", HTTPStatus.BAD_REQUEST, code="INVALID_JSON"
            )
            return
        if request_url.path == "/api/settings/hosts":
            self._change_inventory(payload)
            return
        if request_url.path == "/api/settings/collector":
            self._change_collector_settings(payload)
            return
        if request_url.path == "/api/settings/maintenance":
            self._change_maintenance(payload)
            return
        if request_url.path == "/api/settings/host-group":
            self._change_host_group(payload)
            return
        if request_url.path == "/api/settings/incident-action":
            self._change_incident_action(payload)
            return
        if request_url.path == "/api/probe":
            self._request_probe(payload)
            return
        if request_url.path == "/api/notifications/test":
            self._test_notifications(payload)
            return
        if request_url.path == "/api/service/restart":
            self._restart_service(payload)
            return
        self._change_poll_interval(payload)

    def _request_probe(self, payload: object) -> None:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"host"}
            or not isinstance(payload["host"], str)
            or not is_safe_alias(payload["host"])
        ):
            self._send_error(
                "invalid probe request schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        control = self.monitor_server.probe_control
        if control is None:
            self._send_error(
                "manual probing is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        result = control.request_probe(payload["host"])
        status = str(result.get("status"))
        response_status, code = {
            "unknown_host": (HTTPStatus.NOT_FOUND, "UNKNOWN_HOST"),
            "rate_limited": (HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED"),
            "in_progress": (HTTPStatus.CONFLICT, "PROBE_IN_PROGRESS"),
        }.get(status, (HTTPStatus.ACCEPTED, None))
        extra_headers: tuple[tuple[str, str], ...] = ()
        if code is not None:
            result = {**result, "code": code}
        if response_status is HTTPStatus.TOO_MANY_REQUESTS:
            retry_after = result.get("retryAfterSeconds")
            if (
                not isinstance(retry_after, bool)
                and isinstance(retry_after, int | float)
                and math.isfinite(retry_after)
            ):
                extra_headers = (("Retry-After", str(max(0, math.ceil(retry_after)))),)
        self._send_json(result, response_status, extra_headers)

    def _test_notifications(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload:
            self._send_error(
                "invalid notification test schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        notifications = self._read_only_snapshot().get("notifications")
        if not (isinstance(notifications, dict) and notifications.get("enabled")):
            self._send_error(
                "notifications are not configured",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="NOTIFICATIONS_DISABLED",
            )
            return
        if not self.monitor_server.state.test_notifications():
            self._send_error(
                "notification test is rate limited",
                HTTPStatus.TOO_MANY_REQUESTS,
                code="RATE_LIMITED",
            )
            return
        self._send_json({"status": "queued"}, HTTPStatus.ACCEPTED)

    def _change_incident_action(self, payload: object) -> None:
        expected = {"host", "conditionKey", "action", "durationSeconds", "reason"}
        if not isinstance(payload, dict) or set(payload) != expected:
            self._send_error(
                "invalid incident action schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        host = payload["host"]
        condition_key = payload["conditionKey"]
        action = payload["action"]
        duration = payload["durationSeconds"]
        reason = payload["reason"]
        if (
            not isinstance(host, str)
            or not is_safe_alias(host)
            or not is_valid_incident_condition_key(condition_key)
            or not isinstance(action, str)
            or action not in {"acknowledged", "silenced", "clear"}
            or isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration not in DASHBOARD_INCIDENT_ACTION_DURATIONS
            or (action == "clear") != (duration == 0)
            or not is_valid_incident_action_reason(reason)
        ):
            self._send_error(
                "invalid incident action settings",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SETTINGS",
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_error(
                "incident action management is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            snapshot = inventory.update_incident_action(
                host, condition_key, action, duration, reason
            )
        except InventoryRequestError:
            self._send_error(
                "incident action is no longer valid",
                HTTPStatus.CONFLICT,
                code="INVENTORY_CHANGED",
            )
            return
        except InventoryError:
            self._send_error(
                "incident action could not be saved",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        self._send_json(snapshot)

    def _restart_service(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload:
            self._send_error(
                "invalid restart request schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        restart = self.monitor_server.restart
        if restart is None:
            self._send_error(
                "managed service restart is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return

        # Acknowledge before asking the supervised process to stop this server.
        self._send_json({"status": "restarting"}, HTTPStatus.ACCEPTED)
        with suppress(OSError):
            self.wfile.flush()
        restart()

    def _send_openmetrics(self) -> None:
        payload = render_openmetrics(self._read_only_snapshot())
        self.send_response(HTTPStatus.OK)
        self._common_headers(OPENMETRICS_CONTENT_TYPE, cache="no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write_body(payload)

    def _change_host_group(self, payload: object) -> None:
        if not isinstance(payload, dict) or set(payload) != {"host", "group"}:
            self._send_error(
                "invalid host group schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        host = payload["host"]
        group = payload["group"]
        if (
            not isinstance(host, str)
            or not is_safe_alias(host)
            or not is_valid_host_group(group, required=False)
        ):
            self._send_error(
                "invalid host group settings",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SETTINGS",
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_error(
                "host group management is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            snapshot = inventory.update_host_group(host, group)
        except InventoryRequestError:
            self._send_error(
                "monitored inventory changed; scan again",
                HTTPStatus.CONFLICT,
                code="INVENTORY_CHANGED",
            )
            return
        except InventoryError:
            self._send_error(
                "host group could not be updated",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                code="INTERNAL_ERROR",
            )
            return
        self._send_json(snapshot)

    def _change_maintenance(self, payload: object) -> None:
        if not isinstance(payload, dict) or set(payload) != {
            "host",
            "durationSeconds",
            "reason",
        }:
            self._send_error(
                "invalid maintenance settings schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        host = payload["host"]
        duration = payload["durationSeconds"]
        reason = payload["reason"]
        if (
            not isinstance(host, str)
            or not is_safe_alias(host)
            or isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration not in DASHBOARD_MAINTENANCE_DURATIONS
            or not is_valid_maintenance_reason(reason, required=duration != 0)
        ):
            self._send_error(
                "invalid maintenance settings",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SETTINGS",
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_error(
                "maintenance management is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            snapshot = inventory.update_maintenance(host, duration, reason)
        except InventoryRequestError:
            self._send_error(
                "monitored inventory changed; scan again",
                HTTPStatus.CONFLICT,
                code="INVENTORY_CHANGED",
            )
            return
        except InventoryError:
            self._send_error(
                "maintenance settings could not be updated",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        self._send_json(snapshot)

    def _change_poll_interval(self, payload: object) -> None:
        """Deprecated single-field alias of the collector settings endpoint."""
        if not isinstance(payload, dict) or set(payload) != {"pollIntervalSeconds"}:
            self._send_error(
                "invalid settings schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        value = payload["pollIntervalSeconds"]
        if not self._valid_number(value, 2, 60):
            self._send_error(
                "pollIntervalSeconds must be between 2 and 60",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SETTINGS",
            )
            return
        applied = self._apply_collector_settings({"pollIntervalSeconds": value})
        if applied is None:
            return
        _, interval = applied
        snapshot = self.monitor_server.state.snapshot()
        self._send_json(
            {
                "version": snapshot["version"],
                "startedAt": snapshot["startedAt"],
                "pollIntervalSeconds": interval,
                "collectionStaleAfterSeconds": snapshot["collectionStaleAfterSeconds"],
            },
            extra_headers=_DEPRECATED_ENDPOINT_HEADERS,
        )

    def _change_collector_settings(self, payload: object) -> None:
        if not self._valid_collector_subset(payload):
            self._send_error(
                "invalid collector settings schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        assert isinstance(payload, dict)
        applied = self._apply_collector_settings(payload)
        if applied is None:
            return
        settings, _ = applied
        snapshot = self.monitor_server.state.snapshot()
        self._send_json(
            {
                "version": snapshot["version"],
                "startedAt": snapshot["startedAt"],
                "collectionStaleAfterSeconds": snapshot["collectionStaleAfterSeconds"],
                "collectorSettings": settings,
            }
        )

    def _valid_collector_subset(self, payload: object) -> bool:
        """Accept any non-empty subset of the dashboard collector settings.

        Per-field bounds match the full-payload rules; the cross-field
        probe-timeout-vs-connect-timeout constraint is enforced by the
        inventory against the merged effective configuration.
        """
        if (
            not isinstance(payload, dict)
            or not payload
            or set(payload) - _COLLECTOR_SETTINGS_KEYS
        ):
            return False
        if "pollIntervalSeconds" in payload and not self._valid_number(
            payload["pollIntervalSeconds"], 2, 60
        ):
            return False
        if "probeTimeoutSeconds" in payload and not self._valid_number(
            payload["probeTimeoutSeconds"], 2, 300
        ):
            return False
        if "maxWorkers" in payload:
            workers = payload["maxWorkers"]
            if (
                isinstance(workers, bool)
                or not isinstance(workers, int)
                or not 1 <= workers <= 64
            ):
                return False
        return True

    def _apply_collector_settings(
        self, payload: dict[str, object]
    ) -> tuple[dict[str, object], float] | None:
        """Persist settings and sync the runtime cadence; None means responded."""
        settings = self._persist_collector_settings(payload)
        if settings is None:
            return None
        try:
            interval = self.monitor_server.state.set_poll_interval_seconds(
                settings["pollIntervalSeconds"]
            )
        except (KeyError, ValueError):
            self._send_error(
                "collector settings synchronization failed",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return None
        return settings, interval

    @staticmethod
    def _valid_number(value: object, minimum: float, maximum: float) -> bool:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        try:
            numeric = float(value)
        except OverflowError:
            # JSON integers have unbounded precision; huge ones are invalid.
            return False
        return math.isfinite(numeric) and minimum <= value <= maximum

    def _persist_collector_settings(
        self, settings: dict[str, object]
    ) -> dict[str, object] | None:
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_error(
                "configuration management is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return None
        try:
            return inventory.update_collector_settings(settings)
        except InventoryRequestError:
            self._send_error(
                "invalid collector settings",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SETTINGS",
            )
        except InventoryError:
            self._send_error(
                "collector settings could not be updated",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
        return None

    def _change_inventory(self, payload: object) -> None:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"action", "host"}
            or not isinstance(payload["action"], str)
            or payload["action"] not in {"add", "remove"}
            or not isinstance(payload["host"], str)
            or not is_safe_alias(payload["host"])
        ):
            self._send_error(
                "invalid inventory settings schema",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SCHEMA",
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_error(
                "inventory management is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            snapshot = inventory.change(payload["action"], payload["host"])
        except InventoryRequestError:
            self._send_error(
                "inventory changed; scan again and retry",
                HTTPStatus.CONFLICT,
                code="INVENTORY_CHANGED",
            )
            return
        except InventoryError:
            self._send_error(
                "inventory could not be updated",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        self._send_json(snapshot)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # The settings write intentionally has no cross-origin API contract. A
        # browser must not receive CORS permission to send its non-simple POST.
        self.close_connection = True
        self._send_error(
            "cross-origin requests are not allowed",
            HTTPStatus.FORBIDDEN,
            code="UNTRUSTED_ORIGIN",
        )

    def _has_trusted_host(self) -> bool:
        """Require a Host header naming this server's own trusted hostnames.

        A loopback bind alone does not stop DNS rebinding: an attacker domain
        re-resolved to 127.0.0.1 makes the victim's browser send same-origin
        requests here, but with the attacker's hostname in Host. Pinning Host
        to the loopback/configured allowlist closes that path.
        """
        host_values = self.headers.get_all("Host") or []
        if len(host_values) != 1:
            return False
        hostname = normalize_web_hostname(host_values[0], allow_port=True)
        return (
            hostname is not None and hostname in self.monitor_server.trusted_hostnames
        )

    def _is_dashboard_request(self) -> bool:
        if not self._has_trusted_host():
            return False
        if self.headers.get("X-Monitor-Request") != "dashboard":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            parsed = urlsplit(origin)
            _ = parsed.port
        except ValueError:
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in self.monitor_server.trusted_hostnames
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and fetch_site in {"", "same-origin", "none"}
        )

    def _is_dashboard_read_request(self) -> bool:
        fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        return (
            self._has_trusted_host()
            and self.headers.get("X-Monitor-Request") == "dashboard"
            and fetch_site in {"", "same-origin", "none"}
        )

    def _send_history(self, query: str) -> None:
        parameters = parse_qs(query, keep_blank_values=True)
        if set(parameters) - {"host", "limit"}:
            self._send_error(
                "unknown query parameter",
                HTTPStatus.BAD_REQUEST,
                code="UNKNOWN_QUERY_PARAMETER",
            )
            return
        hosts = parameters.get("host", [])
        limits = parameters.get("limit", ["120"])
        if len(hosts) != 1 or not is_safe_alias(hosts[0]) or len(limits) != 1:
            self._send_error(
                "invalid host or limit",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_QUERY",
            )
            return
        try:
            limit = int(limits[0])
        except ValueError:
            limit = 0
        if not 2 <= limit <= 300:
            self._send_error(
                "limit must be between 2 and 300",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_LIMIT",
            )
            return
        history = self.monitor_server.state.history(hosts[0], limit)
        if history is None:
            self._send_error(
                "unknown monitoring target",
                HTTPStatus.NOT_FOUND,
                code="UNKNOWN_HOST",
            )
            return
        self._send_json(history)

    def _send_gpu_history(self, query: str) -> None:
        if not self._is_dashboard_read_request():
            self._send_error(
                "same-origin dashboard request required",
                HTTPStatus.FORBIDDEN,
                code="UNTRUSTED_ORIGIN",
            )
            return
        parameters = parse_qs(query, keep_blank_values=True)
        if set(parameters) - {"host", "gpu", "limit"}:
            self._send_error(
                "unknown query parameter",
                HTTPStatus.BAD_REQUEST,
                code="UNKNOWN_QUERY_PARAMETER",
            )
            return
        hosts = parameters.get("host", [])
        gpu_ids = parameters.get("gpu", [])
        limits = parameters.get("limit", ["120"])
        if (
            len(hosts) != 1
            or not is_safe_alias(hosts[0])
            or len(gpu_ids) != 1
            or not 1 <= len(gpu_ids[0]) <= 128
            or any(ord(character) < 32 for character in gpu_ids[0])
            or len(limits) != 1
        ):
            self._send_error(
                "invalid host, GPU, or limit",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_QUERY",
            )
            return
        try:
            limit = int(limits[0])
        except ValueError:
            limit = 0
        if not 2 <= limit <= 300:
            self._send_error(
                "limit must be between 2 and 300",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_LIMIT",
            )
            return
        history = self.monitor_server.state.gpu_history(hosts[0], gpu_ids[0], limit)
        if history is None:
            self._send_error(
                "unknown GPU telemetry target",
                HTTPStatus.NOT_FOUND,
                code="UNKNOWN_GPU",
            )
            return
        self._send_json(history)

    def _send_diagnostics(self, query: str) -> None:
        if not self._is_dashboard_read_request():
            self._send_error(
                "same-origin dashboard request required",
                HTTPStatus.FORBIDDEN,
                code="UNTRUSTED_ORIGIN",
            )
            return
        parameters = parse_qs(query, keep_blank_values=True)
        if set(parameters) - {"host"}:
            self._send_error(
                "unknown query parameter",
                HTTPStatus.BAD_REQUEST,
                code="UNKNOWN_QUERY_PARAMETER",
            )
            return
        hosts = parameters.get("host", [])
        if len(hosts) > 1 or (hosts and not is_safe_alias(hosts[0])):
            self._send_error(
                "invalid host", HTTPStatus.BAD_REQUEST, code="INVALID_HOST"
            )
            return
        bundle = self.monitor_server.state.diagnostic_bundle(
            hosts[0] if hosts else None
        )
        if bundle is None:
            self._send_error(
                "unknown monitoring target",
                HTTPStatus.NOT_FOUND,
                code="UNKNOWN_HOST",
            )
            return
        self._send_json(bundle)

    def _send_incidents(self, query: str) -> None:
        parameters = parse_qs(query, keep_blank_values=True)
        if set(parameters) - {"limit"}:
            self._send_error(
                "unknown query parameter",
                HTTPStatus.BAD_REQUEST,
                code="UNKNOWN_QUERY_PARAMETER",
            )
            return
        limits = parameters.get("limit", ["50"])
        if len(limits) != 1:
            self._send_error(
                "invalid limit", HTTPStatus.BAD_REQUEST, code="INVALID_LIMIT"
            )
            return
        try:
            limit = int(limits[0])
        except ValueError:
            limit = 0
        if not 1 <= limit <= 200:
            self._send_error(
                "limit must be between 1 and 200",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_LIMIT",
            )
            return
        self._send_json(self.monitor_server.state.incidents(limit))

    def _send_inventory(self, query: str) -> None:
        if query:
            self._send_error(
                "query parameters are not allowed",
                HTTPStatus.BAD_REQUEST,
                code="QUERY_NOT_ALLOWED",
            )
            return
        if not self._is_dashboard_read_request():
            self._send_error(
                "same-origin dashboard request required",
                HTTPStatus.FORBIDDEN,
                code="UNTRUSTED_ORIGIN",
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_error(
                "inventory management is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            snapshot = inventory.snapshot()
        except InventoryError:
            self._send_error(
                "inventory scan failed",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        self._send_json(snapshot)

    def _send_topology(self, query: str) -> None:
        if query:
            self._send_error(
                "query parameters are not allowed",
                HTTPStatus.BAD_REQUEST,
                code="QUERY_NOT_ALLOWED",
            )
            return
        if not self._is_dashboard_read_request():
            self._send_error(
                "same-origin dashboard request required",
                HTTPStatus.FORBIDDEN,
                code="UNTRUSTED_ORIGIN",
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_error(
                "connection topology is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            topology = inventory.topology()
        except InventoryError:
            self._send_error(
                "connection topology could not be loaded",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        self._send_json(topology)

    def _send_error(
        self,
        message: str,
        status: HTTPStatus,
        code: str | None = None,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Central JSON error envelope.

        `error` stays the human-readable string existing clients rely on;
        `code` is the stable machine-readable UPPER_SNAKE tag.
        """
        body: dict[str, object] = {"error": message}
        if code is not None:
            body["code"] = code
        self._send_json(body, status, extra_headers)

    def _send_json(
        self,
        value: object,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._send_json_payload(payload, status, extra_headers)

    def _send_json_payload(
        self,
        payload: bytes,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self._common_headers("application/json; charset=utf-8", cache="no-store")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write_body(payload)

    def _write_body(self, payload: bytes) -> None:
        if not self._head_only:
            self.wfile.write(payload)

    def _send_events(self) -> None:
        server = self.monitor_server
        if not server._sse_slots.acquire(blocking=False):
            self.close_connection = True
            self._send_error(
                "too many event stream clients",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            self.send_response(HTTPStatus.OK)
            self._common_headers("text/event-stream; charset=utf-8", cache="no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            version = -1
            heartbeat_at = time.monotonic() + _SSE_HEARTBEAT_SECONDS
            while not server.shutdown_event.is_set():
                # A connected event stream is a live viewer: refreshing this
                # every wake keeps the probes on the attended cadence.
                server.state.record_dashboard_activity()
                snapshot = server.state.wait_for_update(version, _SSE_STOP_POLL_SECONDS)
                if snapshot is None:
                    if time.monotonic() < heartbeat_at:
                        continue
                    # Named event (not a comment) so EventSource clients can
                    # observe stream liveness and reconnect when it stalls.
                    self.wfile.write(_SSE_HEARTBEAT_FRAME)
                else:
                    version = int(snapshot["version"])
                    self.wfile.write(server.snapshot_frame(snapshot))
                self.wfile.flush()
                heartbeat_at = time.monotonic() + _SSE_HEARTBEAT_SECONDS
        except OSError:
            return
        finally:
            # The stream is over either way; never reuse this connection.
            self.close_connection = True
            server._sse_slots.release()

    def _common_headers(self, content_type: str, cache: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data: blob:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args: object) -> None:
        # Avoid putting URL query strings or browser-controlled values in logs.
        return


def serve_in_thread(
    host: str,
    port: int,
    state: StateStore,
    inventory: DashboardConfigController | None = None,
    *,
    restart: Callable[[], None] | None = None,
    probe_control: ProbeControl | None = None,
    trusted_hosts: Iterable[str] | None = None,
) -> tuple[MonitorHttpServer, threading.Thread]:
    server = MonitorHttpServer(
        (host, port),
        state,
        inventory,
        restart,
        probe_control,
        trusted_hosts=trusted_hosts,
    )
    thread = threading.Thread(
        target=server.serve_forever, name="mocop-http", daemon=True
    )
    thread.start()
    return server, thread
