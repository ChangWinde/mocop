from __future__ import annotations

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
# Hostnames a browser can present when it genuinely reached this server over
# the loopback interface. DNS rebinding presents the attacker's own domain in
# Host/Origin instead, so pinning these names closes the rebinding bypass.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_WILDCARD_BIND_HOSTS = frozenset({"", "0.0.0.0", "::"})
_SSE_HEARTBEAT_SECONDS = 15.0
# SSE loops wake at this cadence to notice the server shutdown event.
_SSE_STOP_POLL_SECONDS = 1.0
_SERVICE_UNAVAILABLE_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
)


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

    def snapshot_payload(self, snapshot: dict[str, object]) -> bytes:
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
                self._snapshot_cache_payload = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._snapshot_cache_key = key
            return self._snapshot_cache_payload

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

    def _split_request_target(self) -> SplitResult | None:
        """Parse the request target, mapping malformed URLs to a JSON 400."""
        try:
            return urlsplit(self.path)
        except ValueError:
            self.close_connection = True
            self._send_json({"error": "invalid request target"}, HTTPStatus.BAD_REQUEST)
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
            self._send_json(
                {"error": "request body is not allowed"}, HTTPStatus.BAD_REQUEST
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
        if path == "/api/snapshot":
            snapshot = self.monitor_server.state.snapshot()
            self._send_json_payload(self.monitor_server.snapshot_payload(snapshot))
            return
        if path == "/api/events":
            if self._head_only:
                self._send_json(
                    {"error": "method not allowed"},
                    HTTPStatus.METHOD_NOT_ALLOWED,
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
        if path == "/api/service":
            if request_url.query:
                self._send_json(
                    {"error": "query parameters are not allowed"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json(
                {"restartSupported": self.monitor_server.restart is not None}
            )
            return
        if path == "/metrics":
            if request_url.query:
                self.send_error(HTTPStatus.BAD_REQUEST)
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
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        filename, content_type = static
        try:
            payload = (_STATIC_ROOT / filename).read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self._common_headers(content_type, cache="no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write_body(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # Settings writes are rare, and invalid requests may intentionally leave an
        # unread body. Closing this HTTP/1.1 connection prevents those bytes from
        # being parsed as a second request on the same socket.
        self.close_connection = True
        request_url = self._split_request_target()
        if request_url is None:
            return
        write_limits = {
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
        body_limit = write_limits.get(request_url.path)
        if body_limit is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if request_url.query:
            self._send_json(
                {"error": "query parameters are not allowed"}, HTTPStatus.BAD_REQUEST
            )
            return
        if not self._is_dashboard_request():
            self._send_json(
                {"error": "same-origin dashboard request required"},
                HTTPStatus.FORBIDDEN,
            )
            return
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            self._send_json(
                {"error": "application/json required"},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = 0
        if not 1 <= content_length <= body_limit:
            self._send_json(
                {"error": "invalid request body size"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        try:
            payload = json.loads(
                self.rfile.read(content_length).decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_json({"error": "invalid JSON body"}, HTTPStatus.BAD_REQUEST)
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
            self._send_json(
                {"error": "invalid probe request schema"}, HTTPStatus.BAD_REQUEST
            )
            return
        control = self.monitor_server.probe_control
        if control is None:
            self._send_json(
                {"error": "manual probing is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        result = control.request_probe(payload["host"])
        status = str(result.get("status"))
        response_status = {
            "unknown_host": HTTPStatus.NOT_FOUND,
            "rate_limited": HTTPStatus.TOO_MANY_REQUESTS,
            "in_progress": HTTPStatus.CONFLICT,
        }.get(status, HTTPStatus.ACCEPTED)
        self._send_json(result, response_status)

    def _test_notifications(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload:
            self._send_json(
                {"error": "invalid notification test schema"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if not self.monitor_server.state.test_notifications():
            self._send_json(
                {"error": "notification test is unavailable or rate limited"},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        self._send_json({"status": "queued"}, HTTPStatus.ACCEPTED)

    def _change_incident_action(self, payload: object) -> None:
        expected = {"host", "conditionKey", "action", "durationSeconds", "reason"}
        if not isinstance(payload, dict) or set(payload) != expected:
            self._send_json(
                {"error": "invalid incident action schema"},
                HTTPStatus.BAD_REQUEST,
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
            self._send_json(
                {"error": "invalid incident action settings"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_json(
                {"error": "incident action management is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            snapshot = inventory.update_incident_action(
                host, condition_key, action, duration, reason
            )
        except InventoryRequestError:
            self._send_json(
                {"error": "incident action is no longer valid"},
                HTTPStatus.CONFLICT,
            )
            return
        except InventoryError:
            self._send_json(
                {"error": "incident action could not be saved"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(snapshot)

    def _restart_service(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload:
            self._send_json(
                {"error": "invalid restart request schema"}, HTTPStatus.BAD_REQUEST
            )
            return
        restart = self.monitor_server.restart
        if restart is None:
            self._send_json(
                {"error": "managed service restart is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        # Acknowledge before asking the supervised process to stop this server.
        self._send_json({"status": "restarting"}, HTTPStatus.ACCEPTED)
        with suppress(OSError):
            self.wfile.flush()
        restart()

    def _send_openmetrics(self) -> None:
        payload = render_openmetrics(self.monitor_server.state.snapshot())
        self.send_response(HTTPStatus.OK)
        self._common_headers(OPENMETRICS_CONTENT_TYPE, cache="no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write_body(payload)

    def _change_host_group(self, payload: object) -> None:
        if not isinstance(payload, dict) or set(payload) != {"host", "group"}:
            self._send_json(
                {"error": "invalid host group schema"}, HTTPStatus.BAD_REQUEST
            )
            return
        host = payload["host"]
        group = payload["group"]
        if (
            not isinstance(host, str)
            or not is_safe_alias(host)
            or not is_valid_host_group(group, required=False)
        ):
            self._send_json(
                {"error": "invalid host group settings"}, HTTPStatus.BAD_REQUEST
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_json(
                {"error": "host group management is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            snapshot = inventory.update_host_group(host, group)
        except InventoryRequestError:
            self._send_json(
                {"error": "monitored inventory changed; scan again"},
                HTTPStatus.CONFLICT,
            )
            return
        except InventoryError:
            self._send_json(
                {"error": "host group could not be updated"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_json(snapshot)

    def _change_maintenance(self, payload: object) -> None:
        if not isinstance(payload, dict) or set(payload) != {
            "host",
            "durationSeconds",
            "reason",
        }:
            self._send_json(
                {"error": "invalid maintenance settings schema"},
                HTTPStatus.BAD_REQUEST,
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
            self._send_json(
                {"error": "invalid maintenance settings"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_json(
                {"error": "maintenance management is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            snapshot = inventory.update_maintenance(host, duration, reason)
        except InventoryRequestError:
            self._send_json(
                {"error": "monitored inventory changed; scan again"},
                HTTPStatus.CONFLICT,
            )
            return
        except InventoryError:
            self._send_json(
                {"error": "maintenance settings could not be updated"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(snapshot)

    def _change_poll_interval(self, payload: object) -> None:
        if not isinstance(payload, dict) or set(payload) != {"pollIntervalSeconds"}:
            self._send_json(
                {"error": "invalid settings schema"}, HTTPStatus.BAD_REQUEST
            )
            return
        value = payload["pollIntervalSeconds"]
        if not self._valid_number(value, 2, 60):
            self._send_json(
                {"error": "pollIntervalSeconds must be between 2 and 60"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        settings = self._persist_collector_settings({"pollIntervalSeconds": value})
        if settings is None:
            return
        interval = self.monitor_server.state.set_poll_interval_seconds(
            settings["pollIntervalSeconds"]
        )
        snapshot = self.monitor_server.state.snapshot()
        self._send_json(
            {
                "version": snapshot["version"],
                "startedAt": snapshot["startedAt"],
                "pollIntervalSeconds": interval,
                "collectionStaleAfterSeconds": snapshot["collectionStaleAfterSeconds"],
            }
        )

    def _change_collector_settings(self, payload: object) -> None:
        if (
            not isinstance(payload, dict)
            or set(payload) != _COLLECTOR_SETTINGS_KEYS
            or not self._valid_number(payload["pollIntervalSeconds"], 2, 60)
            or not self._valid_number(payload["probeTimeoutSeconds"], 2, 300)
            or isinstance(payload["maxWorkers"], bool)
            or not isinstance(payload["maxWorkers"], int)
            or not 1 <= payload["maxWorkers"] <= 64
        ):
            self._send_json(
                {"error": "invalid collector settings schema"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        settings = self._persist_collector_settings(payload)
        if settings is None:
            return
        try:
            self.monitor_server.state.set_poll_interval_seconds(
                settings["pollIntervalSeconds"]
            )
        except (KeyError, ValueError):
            self._send_json(
                {"error": "collector settings synchronization failed"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        snapshot = self.monitor_server.state.snapshot()
        self._send_json(
            {
                "version": snapshot["version"],
                "startedAt": snapshot["startedAt"],
                "collectionStaleAfterSeconds": snapshot["collectionStaleAfterSeconds"],
                "collectorSettings": settings,
            }
        )

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
            self._send_json(
                {"error": "configuration management is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return None
        try:
            return inventory.update_collector_settings(settings)
        except InventoryRequestError:
            self._send_json(
                {"error": "invalid collector settings"},
                HTTPStatus.BAD_REQUEST,
            )
        except InventoryError:
            self._send_json(
                {"error": "collector settings could not be updated"},
                HTTPStatus.SERVICE_UNAVAILABLE,
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
            self._send_json(
                {"error": "invalid inventory settings schema"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_json(
                {"error": "inventory management is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            snapshot = inventory.change(payload["action"], payload["host"])
        except InventoryRequestError:
            self._send_json(
                {"error": "inventory changed; scan again and retry"},
                HTTPStatus.CONFLICT,
            )
            return
        except InventoryError:
            self._send_json(
                {"error": "inventory could not be updated"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(snapshot)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # The settings write intentionally has no cross-origin API contract. A
        # browser must not receive CORS permission to send its non-simple POST.
        self.close_connection = True
        self._send_json(
            {"error": "cross-origin requests are not allowed"},
            HTTPStatus.FORBIDDEN,
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
            self._send_json(
                {"error": "unknown query parameter"}, HTTPStatus.BAD_REQUEST
            )
            return
        hosts = parameters.get("host", [])
        limits = parameters.get("limit", ["120"])
        if len(hosts) != 1 or not is_safe_alias(hosts[0]) or len(limits) != 1:
            self._send_json({"error": "invalid host or limit"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            limit = int(limits[0])
        except ValueError:
            limit = 0
        if not 2 <= limit <= 300:
            self._send_json(
                {"error": "limit must be between 2 and 300"}, HTTPStatus.BAD_REQUEST
            )
            return
        history = self.monitor_server.state.history(hosts[0], limit)
        if history is None:
            self._send_json(
                {"error": "unknown monitoring target"}, HTTPStatus.NOT_FOUND
            )
            return
        self._send_json(history)

    def _send_gpu_history(self, query: str) -> None:
        if not self._is_dashboard_read_request():
            self._send_json(
                {"error": "same-origin dashboard request required"},
                HTTPStatus.FORBIDDEN,
            )
            return
        parameters = parse_qs(query, keep_blank_values=True)
        if set(parameters) - {"host", "gpu", "limit"}:
            self._send_json(
                {"error": "unknown query parameter"}, HTTPStatus.BAD_REQUEST
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
            self._send_json(
                {"error": "invalid host, GPU, or limit"}, HTTPStatus.BAD_REQUEST
            )
            return
        try:
            limit = int(limits[0])
        except ValueError:
            limit = 0
        if not 2 <= limit <= 300:
            self._send_json(
                {"error": "limit must be between 2 and 300"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        history = self.monitor_server.state.gpu_history(hosts[0], gpu_ids[0], limit)
        if history is None:
            self._send_json(
                {"error": "unknown GPU telemetry target"}, HTTPStatus.NOT_FOUND
            )
            return
        self._send_json(history)

    def _send_diagnostics(self, query: str) -> None:
        if not self._is_dashboard_read_request():
            self._send_json(
                {"error": "same-origin dashboard request required"},
                HTTPStatus.FORBIDDEN,
            )
            return
        parameters = parse_qs(query, keep_blank_values=True)
        if set(parameters) - {"host"}:
            self._send_json(
                {"error": "unknown query parameter"}, HTTPStatus.BAD_REQUEST
            )
            return
        hosts = parameters.get("host", [])
        if len(hosts) > 1 or (hosts and not is_safe_alias(hosts[0])):
            self._send_json({"error": "invalid host"}, HTTPStatus.BAD_REQUEST)
            return
        bundle = self.monitor_server.state.diagnostic_bundle(
            hosts[0] if hosts else None
        )
        if bundle is None:
            self._send_json(
                {"error": "unknown monitoring target"}, HTTPStatus.NOT_FOUND
            )
            return
        self._send_json(bundle)

    def _send_incidents(self, query: str) -> None:
        parameters = parse_qs(query, keep_blank_values=True)
        if set(parameters) - {"limit"}:
            self._send_json(
                {"error": "unknown query parameter"}, HTTPStatus.BAD_REQUEST
            )
            return
        limits = parameters.get("limit", ["50"])
        if len(limits) != 1:
            self._send_json({"error": "invalid limit"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            limit = int(limits[0])
        except ValueError:
            limit = 0
        if not 1 <= limit <= 200:
            self._send_json(
                {"error": "limit must be between 1 and 200"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        self._send_json(self.monitor_server.state.incidents(limit))

    def _send_inventory(self, query: str) -> None:
        if query:
            self._send_json(
                {"error": "query parameters are not allowed"}, HTTPStatus.BAD_REQUEST
            )
            return
        if not self._is_dashboard_read_request():
            self._send_json(
                {"error": "same-origin dashboard request required"},
                HTTPStatus.FORBIDDEN,
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_json(
                {"error": "inventory management is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            snapshot = inventory.snapshot()
        except InventoryError:
            self._send_json(
                {"error": "inventory scan failed"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(snapshot)

    def _send_topology(self, query: str) -> None:
        if query:
            self._send_json(
                {"error": "query parameters are not allowed"}, HTTPStatus.BAD_REQUEST
            )
            return
        if not self._is_dashboard_read_request():
            self._send_json(
                {"error": "same-origin dashboard request required"},
                HTTPStatus.FORBIDDEN,
            )
            return
        inventory = self.monitor_server.inventory
        if inventory is None:
            self._send_json(
                {"error": "connection topology is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            topology = inventory.topology()
        except InventoryError:
            self._send_json(
                {"error": "connection topology could not be loaded"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(topology)

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
            self._send_json(
                {"error": "too many event stream clients"},
                HTTPStatus.SERVICE_UNAVAILABLE,
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
                snapshot = server.state.wait_for_update(version, _SSE_STOP_POLL_SECONDS)
                if snapshot is None:
                    if time.monotonic() < heartbeat_at:
                        continue
                    self.wfile.write(b": heartbeat\n\n")
                else:
                    version = int(snapshot["version"])
                    payload = server.snapshot_payload(snapshot)
                    self.wfile.write(b"event: snapshot\ndata: " + payload + b"\n\n")
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
