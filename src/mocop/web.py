from __future__ import annotations

import hmac
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
from urllib.parse import SplitResult, urlsplit

from . import __version__
from .api_manifest import (
    API_SCHEMA_VERSION,
    API_VERSION,
    DOCUMENTATION_URL,
    FIELD_CONVENTIONS,
    QUERY_SCHEMAS,
    ROUTE_METHODS,
    WRITE_BODY_LIMITS,
    WRITE_REQUIREMENTS,
    WRITE_SCHEMAS,
    describe_endpoints,
    describe_error_codes,
)
from .api_schema import BodyError, QueryError, parse_query, validate_body
from .capacity import CapacityRequest, match_capacity
from .config import (
    is_valid_host_group,
    is_valid_incident_action_reason,
    is_valid_incident_condition_key,
    is_valid_maintenance_reason,
)
from .hostnames import (
    is_dashboard_read,
    is_dashboard_write,
    normalize_web_hostname,
    trusted_web_policy,
)
from .inventory import (
    DashboardConfigController,
    InventoryError,
    InventoryRequestError,
)
from .metrics import (
    OPENMETRICS_CONTENT_TYPE,
    OpenMetricsLimitError,
    render_openmetrics,
)
from .service import ProbeControl, StateStore
from .static_assets import (
    STATIC_ROUTES as _STATIC_ROUTES,
)
from .static_assets import (
    client_cache_is_current,
    load_asset,
    strong_etag,
)
from .updates import UpdateStatusSource

_SSE_HEARTBEAT_SECONDS = 15.0
# SSE loops wake at this cadence to notice the server shutdown event.
_SSE_STOP_POLL_SECONDS = 1.0
_SSE_SNAPSHOT_PREFIX = b"event: snapshot\ndata: "
_SSE_HEARTBEAT_FRAME = b"event: heartbeat\ndata: {}\n\n"
_CONNECTION_LIMIT_BODY = b'{"error":"too many connections","code":"CONNECTION_LIMIT"}'
_SERVICE_UNAVAILABLE_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Connection: close\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_CONNECTION_LIMIT_BODY)).encode("ascii") + b"\r\n"
    b"\r\n" + _CONNECTION_LIMIT_BODY
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
    methods = ROUTE_METHODS[path]
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
        access_token: str,
        trusted_hosts: Iterable[str] | None = None,
        updates: UpdateStatusSource | None = None,
    ) -> None:
        # Every private route is Bearer-protected; there is no unauthenticated
        # server mode, so an empty capability is a programming error.
        if not access_token:
            raise ValueError("the HTTP server requires a non-empty access token")
        try:
            socket.inet_pton(socket.AF_INET6, address[0].split("%", 1)[0])
        except OSError:
            self.address_family = socket.AF_INET
        else:
            self.address_family = socket.AF_INET6
        self.state = state
        self.inventory = inventory
        self.restart = restart
        self.probe_control = probe_control
        self.updates = updates
        self.trusted_hostnames, self.trusted_origin_suffixes = trusted_web_policy(
            address[0], trusted_hosts
        )
        self.access_token = access_token
        self.shutdown_event = threading.Event()
        self._connection_slots = threading.BoundedSemaphore(
            self.max_concurrent_connections
        )
        self._sse_slots = threading.BoundedSemaphore(self.max_sse_clients)
        self._snapshot_cache_lock = threading.Lock()
        self._snapshot_cache_key: tuple[object, ...] | None = None
        self._snapshot_cache_payload = b""
        self._snapshot_cache_frame = b""
        self._metrics_cache_key: tuple[object, ...] | None = None
        self._metrics_cache_payload = b""
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

    @staticmethod
    def _projection_key(snapshot: dict[str, object]) -> tuple[object, ...]:
        # Version counters cover host and incident state; the two adapter
        # status blocks change on their own and are small enough to repr.
        return (
            snapshot.get("version"),
            snapshot.get("incidentVersion"),
            repr(snapshot.get("persistence")),
            repr(snapshot.get("notifications")),
        )

    def _snapshot_cache(self, snapshot: dict[str, object]) -> tuple[bytes, bytes]:
        key = self._projection_key(snapshot)
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

    def metrics_payload(self, snapshot: dict[str, object]) -> bytes:
        key = self._projection_key(snapshot)
        with self._snapshot_cache_lock:
            if key != self._metrics_cache_key:
                self._metrics_cache_payload = render_openmetrics(snapshot)
                self._metrics_cache_key = key
            return self._metrics_cache_payload

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
    # Unlike the socket's inactivity timeout, this deadline cannot be reset by
    # a slow client sending one byte at a time.
    request_deadline_seconds = 15.0
    _head_only = False
    _header_deadline_timer: threading.Timer | None = None

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Route every otherwise-unimplemented method through API policy.

        ``BaseHTTPRequestHandler`` normally emits an unauthenticated HTML 501
        before a ``do_*`` method can run. Intercept only that dispatcher case;
        the fallback then applies authentication and the stable JSON 404/405
        contract for API-family paths while retaining HTML for static paths.
        """
        if code == HTTPStatus.NOT_IMPLEMENTED and getattr(self, "command", ""):
            self._unsupported_method(self.command)
            return
        super().send_error(code, message, explain)

    def _abort_request_at_deadline(self) -> None:
        self.close_connection = True
        with suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def _cancel_header_deadline(self) -> None:
        timer = self._header_deadline_timer
        if timer is not None:
            timer.cancel()
            if timer is not threading.current_thread():
                timer.join()
            self._header_deadline_timer = None

    def handle_one_request(self) -> None:
        """Bound request-line and header parsing by one absolute deadline."""
        self._head_only = False
        self._request_deadline = time.monotonic() + self.request_deadline_seconds
        timer = threading.Timer(
            self.request_deadline_seconds, self._abort_request_at_deadline
        )
        timer.daemon = True
        self._header_deadline_timer = timer
        timer.start()
        try:
            super().handle_one_request()
        finally:
            self._cancel_header_deadline()

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if not parsed:
            self._cancel_header_deadline()
            return False
        host_values = self.headers.get_all("Host") or []
        host_required = self.request_version == "HTTP/1.1"
        invalid_host = (
            (host_required and len(host_values) != 1)
            or len(host_values) > 1
            or (
                bool(host_values)
                and normalize_web_hostname(host_values[0], allow_port=True) is None
            )
        )
        absolute_target = not self.path.startswith("/") and self.path != "*"
        if invalid_host or absolute_target:
            self._head_only = self.command == "HEAD"
            self.close_connection = True
            self._send_error(
                "invalid HTTP request authority",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_REQUEST_AUTHORITY",
            )
            self._cancel_header_deadline()
            return False
        self._cancel_header_deadline()
        return True

    def version_string(self) -> str:
        return self.server_version

    @property
    def monitor_server(self) -> MonitorHttpServer:
        return self.server  # type: ignore[return-value]

    def _read_only_snapshot(self) -> dict[str, object]:
        """Return the cached read-only state projection for serialization."""
        return self.monitor_server.state.snapshot_view()

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

    def _has_bearer_token(self) -> bool:
        """Authenticate a bootstrap request without accepting ambiguity."""
        expected = self.monitor_server.access_token
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1 or not values[0].startswith("Bearer "):
            return False
        candidate = values[0][len("Bearer ") :]
        try:
            return hmac.compare_digest(candidate, expected)
        except TypeError:
            # Header values are latin-1 and may carry non-ASCII bytes, which
            # compare_digest refuses; such a credential is simply wrong and
            # must produce the documented 403, not a reset connection.
            return False

    def _require_authentication(self, path: str) -> bool:
        """Protect every non-health API surface from other local users."""
        if path in {"/healthz", "/readyz", "/api/meta"}:
            return True
        if not (_is_api_family_path(path) or path == "/metrics"):
            return True
        if self._has_bearer_token():
            return True
        # An agent that reaches this cold learns where the capability lives
        # and where the contract is documented without leaving the response.
        self._send_json(
            {
                "error": "dashboard authentication required",
                "code": "AUTHENTICATION_REQUIRED",
                "hint": (
                    "Send 'Authorization: Bearer <capability>'. A managed "
                    "service stores it in the private access-token file beside "
                    "its configuration (~/.config/mocop/access-token by default); "
                    "a foreground run prints it once as the URL fragment."
                ),
                "documentation": DOCUMENTATION_URL,
            },
            HTTPStatus.FORBIDDEN,
        )
        return False

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
            ambiguous = not (
                length
                and length.isascii()
                and length.isdigit()
                and all(character == "0" for character in length)
            )
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
        path = request_url.path
        if not self._require_authentication(path):
            return
        if request_url.query and path in ROUTE_METHODS and path not in QUERY_SCHEMAS:
            # The manifest publishes an empty `query` for these routes, so a
            # query string is a contract violation rather than noise to ignore.
            self._send_error(
                "query parameters are not allowed",
                HTTPStatus.BAD_REQUEST,
                code="QUERY_NOT_ALLOWED",
            )
            return
        # Any authenticated dashboard-marked read (snapshot polling included)
        # is a live viewer; the event stream marks presence separately because
        # EventSource cannot attach the marker header.
        if self.headers.get("X-Monitor-Request") == "dashboard":
            self.monitor_server.state.record_dashboard_activity()
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
        if path == "/api/usage":
            self._send_usage(request_url.query)
            return
        if path == "/api/capacity":
            self._send_capacity(request_url.query)
            return
        if path == "/api/gpu-history":
            self._send_gpu_history(request_url.query)
            return
        if path == "/api/incidents":
            self._send_incidents(request_url.query)
            return
        if path == "/api/inventory":
            self._send_inventory()
            return
        if path == "/api/topology":
            self._send_topology()
            return
        if path == "/api/update":
            self._send_update_status()
            return
        if path == "/api/diagnostics":
            self._send_diagnostics(request_url.query)
            return
        if path == "/api/meta":
            self._send_meta()
            return
        if path == "/metrics":
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
        payload = load_asset(filename)
        if payload is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        etag = strong_etag(payload)
        if client_cache_is_current(self.headers.get("If-None-Match"), etag):
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

    def _send_meta(self) -> None:
        server = self.monitor_server
        self._send_json(
            {
                "apiVersion": API_VERSION,
                "appVersion": __version__,
                "schemaVersion": API_SCHEMA_VERSION,
                "documentation": DOCUMENTATION_URL,
                "capabilities": {
                    "restartSupported": server.restart is not None,
                    "manualProbeSupported": server.probe_control is not None,
                    "configurationWriteSupported": (
                        self._configuration_write_supported()
                    ),
                    "updateSupported": server.updates is not None,
                },
                "conventions": FIELD_CONVENTIONS,
                "write": WRITE_REQUIREMENTS,
                "errorCodes": describe_error_codes(),
                "endpoints": describe_endpoints(),
            }
        )

    def _configuration_write_supported(self) -> bool:
        """Writable-config capability without scanning or connecting over SSH."""
        inventory = self.monitor_server.inventory
        return inventory is not None and inventory.writable()

    def _send_route_fallback(self, method: str, path: str) -> None:
        """JSON 404/405 for API-family paths; static paths keep the HTML page."""
        if not _is_api_family_path(path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        methods = ROUTE_METHODS.get(path)
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
        if not self._require_authentication(request_url.path):
            return
        body_limit = WRITE_BODY_LIMITS.get(request_url.path)
        if body_limit is None:
            self._send_route_fallback("POST", request_url.path)
            return
        declared_lengths = self.headers.get_all("Content-Length") or []
        transfer_encoding = self.headers.get_all("Transfer-Encoding") or []
        if (
            transfer_encoding
            or len(declared_lengths) != 1
            or not declared_lengths[0].isascii()
            or not declared_lengths[0].isdigit()
        ):
            self._send_error(
                "invalid request framing",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_REQUEST_FRAMING",
            )
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
        normalized_length = declared_lengths[0].lstrip("0") or "0"
        content_length = (
            body_limit + 1
            if len(normalized_length) > len(str(body_limit))
            else int(normalized_length)
        )
        if not 1 <= content_length <= body_limit:
            self._send_error(
                "invalid request body size",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code="PAYLOAD_TOO_LARGE",
            )
            return
        try:
            remaining = self._request_deadline - time.monotonic()
            if remaining <= 0:
                self._abort_request_at_deadline()
                return
            timer = threading.Timer(remaining, self._abort_request_at_deadline)
            timer.daemon = True
            timer.start()
            try:
                body = self.rfile.read(content_length)
            finally:
                timer.cancel()
                timer.join()
            if len(body) != content_length:
                return
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_error(
                "invalid JSON body", HTTPStatus.BAD_REQUEST, code="INVALID_JSON"
            )
            return
        # Shape, field types, and published values/bounds are checked once
        # against the manifest; handlers only add cross-field rules.
        try:
            body = validate_body(WRITE_SCHEMAS[request_url.path], payload)
        except BodyError as error:
            self._send_error(
                str(error), HTTPStatus.BAD_REQUEST, code=error.code, field=error.field
            )
            return
        handlers: dict[str, Callable[[dict[str, object]], None]] = {
            "/api/settings/hosts": self._change_inventory,
            "/api/settings/collector": self._change_collector_settings,
            "/api/settings/maintenance": self._change_maintenance,
            "/api/settings/host-group": self._change_host_group,
            "/api/settings/incident-action": self._change_incident_action,
            "/api/probe": self._request_probe,
            "/api/notifications/test": self._test_notifications,
            "/api/service/restart": self._restart_service,
            "/api/update/apply": self._apply_update,
        }
        handlers[request_url.path](body)

    def _unsupported_method(self, method: str) -> None:
        self.close_connection = True
        request_url = self._split_request_target()
        if request_url is not None and self._require_authentication(request_url.path):
            self._send_route_fallback(method, request_url.path)

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._unsupported_method("PUT")

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._unsupported_method("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._unsupported_method("DELETE")

    def do_TRACE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._unsupported_method("TRACE")

    def _request_probe(self, body: dict[str, object]) -> None:
        control = self.monitor_server.probe_control
        if control is None:
            self._send_error(
                "manual probing is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return
        result = control.request_probe(body["host"])
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

    def _test_notifications(self, _body: dict[str, object]) -> None:
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

    def _change_incident_action(self, body: dict[str, object]) -> None:
        host = body["host"]
        condition_key = body["conditionKey"]
        action = body["action"]
        incident_started_at = body["incidentStartedAt"]
        duration = body["durationSeconds"]
        reason = body["reason"]
        clearing = action == "clear"
        rejected_field = (
            "conditionKey"
            if not is_valid_incident_condition_key(condition_key)
            else "reason"
            if not is_valid_incident_action_reason(reason)
            else "durationSeconds"
            if clearing != (duration == 0)
            else "incidentStartedAt"
            if clearing != (incident_started_at is None) or incident_started_at == ""
            else None
        )
        if rejected_field is not None:
            self._send_error(
                "invalid incident action settings",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SETTINGS",
                field=rejected_field,
            )
            return
        if not self._incident_generation_matches(
            host,
            condition_key,
            incident_started_at,
            "incident condition is no longer active",
        ):
            return
        snapshot = self._write_configuration(
            lambda inventory: inventory.update_incident_action(
                host,
                condition_key,
                action,
                duration,
                reason,
                incident_started_at,
            )
        )
        if snapshot is None:
            return
        if not self._incident_generation_matches(
            host,
            condition_key,
            incident_started_at,
            "incident condition changed while the action was saved",
        ):
            return
        self._send_json(snapshot)

    def _incident_generation_matches(
        self, host: str, condition_key: str, started_at: object, message: str
    ) -> bool:
        """Clearing never races; acknowledging binds to one incident generation."""
        if started_at is None:
            return True
        if (
            self.monitor_server.state.active_incident_started_at(host, condition_key)
            == started_at
        ):
            return True
        self._send_error(message, HTTPStatus.CONFLICT, code="INCIDENT_NOT_ACTIVE")
        return False

    def _write_configuration(
        self,
        operation: Callable[[DashboardConfigController], dict[str, object]],
        *,
        rejected: tuple[HTTPStatus, str, str] = (
            HTTPStatus.CONFLICT,
            "INVENTORY_CHANGED",
            "configuration changed underneath the request; re-read and retry",
        ),
    ) -> dict[str, object] | None:
        """Run one configuration write; ``None`` means the error was already sent.

        A missing controller and a failed scan or persist are 503s; a request
        the controller refuses maps to ``rejected``, which is the 409 conflict
        for every route except collector settings, whose only controller-level
        refusal is the cross-field timeout rule (400).
        """
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_error(
                "configuration management is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
            return None
        try:
            return operation(inventory)
        except InventoryRequestError:
            status, code, message = rejected
            self._send_error(message, status, code=code)
        except InventoryError:
            self._send_error(
                "configuration could not be updated",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
            )
        return None

    def _send_update_status(self) -> None:
        if not self._require_dashboard_read():
            return
        updates = self.monitor_server.updates
        self._send_json(
            updates.status()
            if updates is not None
            else {"mode": "off", "currentVersion": __version__}
        )

    def _apply_update(self, _body: dict[str, object]) -> None:
        updates = self.monitor_server.updates
        accepted, message = (
            updates.apply() if updates is not None else (False, "self-update is off")
        )
        if not accepted:
            self._send_error(message, HTTPStatus.CONFLICT, code="UPDATE_NOT_APPLICABLE")
            return
        self._send_json({"status": "updating"}, HTTPStatus.ACCEPTED)

    def _restart_service(self, _body: dict[str, object]) -> None:
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
        try:
            payload = self.monitor_server.metrics_payload(self._read_only_snapshot())
        except OpenMetricsLimitError:
            self._send_error(
                "metrics series budget exceeded",
                HTTPStatus.SERVICE_UNAVAILABLE,
                code="METRICS_LIMIT_EXCEEDED",
            )
            return
        self.send_response(HTTPStatus.OK)
        self._common_headers(OPENMETRICS_CONTENT_TYPE, cache="no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write_body(payload)

    def _change_host_group(self, body: dict[str, object]) -> None:
        host, group = body["host"], body["group"]
        if not is_valid_host_group(group, required=False):
            self._send_error(
                "invalid host group settings",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SETTINGS",
                field="group",
            )
            return
        snapshot = self._write_configuration(
            lambda inventory: inventory.update_host_group(host, group)
        )
        if snapshot is not None:
            self._send_json(snapshot)

    def _change_maintenance(self, body: dict[str, object]) -> None:
        host, duration, reason = body["host"], body["durationSeconds"], body["reason"]
        if not is_valid_maintenance_reason(reason, required=duration != 0):
            self._send_error(
                "invalid maintenance settings",
                HTTPStatus.BAD_REQUEST,
                code="INVALID_SETTINGS",
                field="reason",
            )
            return
        snapshot = self._write_configuration(
            lambda inventory: inventory.update_maintenance(host, duration, reason)
        )
        if snapshot is not None:
            self._send_json(snapshot)

    def _change_collector_settings(self, body: dict[str, object]) -> None:
        # The manifest checked types and per-field bounds; the controller
        # applies the probe-timeout-vs-connect-timeout rule to the merged
        # effective configuration, which is the one refusal left to map.
        settings = self._write_configuration(
            lambda inventory: inventory.update_collector_settings(body),
            rejected=(
                HTTPStatus.BAD_REQUEST,
                "INVALID_SETTINGS",
                "invalid collector settings",
            ),
        )
        if settings is None:
            return
        # The persisted interval is inside the configuration bounds, which the
        # runtime scheduler accepts by construction.
        self.monitor_server.state.set_poll_interval_seconds(
            settings["pollIntervalSeconds"]
        )
        # Three scalar fields do not justify deep-copying the whole projection.
        snapshot = self.monitor_server.state.snapshot_view()
        self._send_json(
            {
                "version": snapshot["version"],
                "startedAt": snapshot["startedAt"],
                "collectionStaleAfterSeconds": snapshot["collectionStaleAfterSeconds"],
                "collectorSettings": settings,
            }
        )

    def _change_inventory(self, body: dict[str, object]) -> None:
        action, host = body["action"], body["host"]
        snapshot = self._write_configuration(
            lambda inventory: inventory.change(action, host)
        )
        if snapshot is not None:
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

    def _is_dashboard_request(self) -> bool:
        server = self.monitor_server
        return is_dashboard_write(
            self.headers, server.trusted_hostnames, server.trusted_origin_suffixes
        )

    def _is_dashboard_read_request(self) -> bool:
        return is_dashboard_read(self.headers, self.monitor_server.trusted_hostnames)

    def _require_dashboard_read(self) -> bool:
        """Enforce the reader tier; every manifested R route must call this."""
        if self._is_dashboard_read_request():
            return True
        self._send_error(
            "same-origin dashboard request required",
            HTTPStatus.FORBIDDEN,
            code="UNTRUSTED_ORIGIN",
        )
        return False

    def _parse_query(self, path: str, query: str) -> dict[str, object] | None:
        """Validate a GET query against the manifest; None means responded."""
        try:
            return parse_query(QUERY_SCHEMAS[path], query)
        except QueryError as exc:
            self._send_error(
                str(exc), HTTPStatus.BAD_REQUEST, code=exc.code, field=exc.field
            )
            return None

    def _send_history(self, query: str) -> None:
        values = self._parse_query("/api/history", query)
        if values is None:
            return
        history = self.monitor_server.state.history(values["host"], values["limit"])
        if history is None:
            self._send_error(
                "unknown monitoring target",
                HTTPStatus.NOT_FOUND,
                code="UNKNOWN_HOST",
            )
            return
        self._send_json(history)

    def _send_usage(self, query: str) -> None:
        values = self._parse_query("/api/usage", query)
        if values is None:
            return
        self._send_json(
            self.monitor_server.state.usage(values["hours"], values["limit"])
        )

    def _send_capacity(self, query: str) -> None:
        """Rank idle GPU groups against a demand; observations, never reservations."""
        values = self._parse_query("/api/capacity", query)
        if values is None:
            return
        state = self.monitor_server.state
        snapshot = state.snapshot_view()
        thresholds = snapshot["thresholds"]
        assert isinstance(thresholds, dict)
        result = match_capacity(
            snapshot["servers"],  # type: ignore[arg-type]
            state.incidents(1)["active"],  # type: ignore[arg-type]
            CapacityRequest(values["gpus"], values["min_vram_gib"], values["model"]),
            busy_pct=float(thresholds["gpu_busy_pct"]),
            temperature_c=float(thresholds["gpu_temperature_warning_c"]),
        )
        result["generatedAt"] = snapshot["generatedAt"]
        result["lastPollCompletedAt"] = snapshot["lastPollCompletedAt"]
        self._send_json(result)

    def _send_gpu_history(self, query: str) -> None:
        if not self._require_dashboard_read():
            return
        values = self._parse_query("/api/gpu-history", query)
        if values is None:
            return
        history = self.monitor_server.state.gpu_history(
            values["host"], values["gpu"], values["limit"]
        )
        if history is None:
            self._send_error(
                "unknown GPU telemetry target",
                HTTPStatus.NOT_FOUND,
                code="UNKNOWN_GPU",
            )
            return
        self._send_json(history)

    def _send_diagnostics(self, query: str) -> None:
        if not self._require_dashboard_read():
            return
        values = self._parse_query("/api/diagnostics", query)
        if values is None:
            return
        bundle = self.monitor_server.state.diagnostic_bundle(values["host"])
        if bundle is None:
            self._send_error(
                "unknown monitoring target",
                HTTPStatus.NOT_FOUND,
                code="UNKNOWN_HOST",
            )
            return
        self._send_json(bundle)

    def _send_incidents(self, query: str) -> None:
        values = self._parse_query("/api/incidents", query)
        if values is None:
            return
        self._send_json(self.monitor_server.state.incidents(values["limit"]))

    def _send_inventory(self) -> None:
        if not self._require_dashboard_read():
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

    def _send_topology(self) -> None:
        if not self._require_dashboard_read():
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
        *,
        field: str | None = None,
    ) -> None:
        """Central JSON error envelope.

        `error` stays the human-readable string existing clients rely on;
        `code` is the stable machine-readable UPPER_SNAKE tag; `field` names
        the query parameter or body field at fault when exactly one is.
        """
        body: dict[str, object] = {"error": message}
        if code is not None:
            body["code"] = code
        if field is not None:
            body["field"] = field
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
