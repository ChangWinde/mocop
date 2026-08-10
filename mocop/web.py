from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import is_safe_alias
from .inventory import InventoryController, InventoryError, InventoryRequestError
from .service import StateStore

_STATIC_ROOT = Path(__file__).with_name("static")
_STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
_MAX_SETTINGS_BODY_BYTES = 128
_MAX_INVENTORY_BODY_BYTES = 512


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

    def __init__(
        self,
        address: tuple[str, int],
        state: StateStore,
        inventory: InventoryController | None = None,
    ) -> None:
        self.state = state
        self.inventory = inventory
        super().__init__(address, MonitorRequestHandler)


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server_version = f"mocop/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        return self.server_version

    @property
    def monitor_server(self) -> MonitorHttpServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request_url = urlsplit(self.path)
        path = request_url.path
        if path == "/api/snapshot":
            self._send_json(self.monitor_server.state.snapshot())
            return
        if path == "/api/events":
            self._send_events()
            return
        if path == "/api/history":
            self._send_history(request_url.query)
            return
        if path == "/api/incidents":
            self._send_incidents(request_url.query)
            return
        if path == "/api/inventory":
            self._send_inventory(request_url.query)
            return
        if path == "/healthz":
            self._send_json(
                {
                    "status": "ok",
                    "ready": self.monitor_server.state.health()["ready"],
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
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # Settings writes are rare, and invalid requests may intentionally leave an
        # unread body. Closing this HTTP/1.1 connection prevents those bytes from
        # being parsed as a second request on the same socket.
        self.close_connection = True
        request_url = urlsplit(self.path)
        write_limits = {
            "/api/settings/poll-interval": _MAX_SETTINGS_BODY_BYTES,
            "/api/settings/hosts": _MAX_INVENTORY_BODY_BYTES,
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
        self._change_poll_interval(payload)

    def _change_poll_interval(self, payload: object) -> None:
        if not isinstance(payload, dict) or set(payload) != {"pollIntervalSeconds"}:
            self._send_json(
                {"error": "invalid settings schema"}, HTTPStatus.BAD_REQUEST
            )
            return
        try:
            interval = self.monitor_server.state.set_poll_interval_seconds(
                payload["pollIntervalSeconds"]
            )
        except ValueError:
            self._send_json(
                {"error": "pollIntervalSeconds must be between 2 and 60"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        snapshot = self.monitor_server.state.snapshot()
        self._send_json(
            {
                "version": snapshot["version"],
                "startedAt": snapshot["startedAt"],
                "pollIntervalSeconds": interval,
                "collectionStaleAfterSeconds": snapshot["collectionStaleAfterSeconds"],
            }
        )

    def _change_inventory(self, payload: object) -> None:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"action", "host"}
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

    def _is_dashboard_request(self) -> bool:
        if self.headers.get("X-Monitor-Request") != "dashboard":
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return False
        parsed = urlsplit(origin)
        try:
            _ = parsed.port
        except ValueError:
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and fetch_site in {"", "same-origin", "none"}
        )

    def _is_dashboard_read_request(self) -> bool:
        fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        return self.headers.get("X-Monitor-Request") == "dashboard" and fetch_site in {
            "",
            "same-origin",
            "none",
        }

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

    def _send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self._common_headers("application/json; charset=utf-8", cache="no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self._common_headers("text/event-stream; charset=utf-8", cache="no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        version = -1
        try:
            while True:
                snapshot = self.monitor_server.state.wait_for_update(version, 15)
                if snapshot is None:
                    self.wfile.write(b": heartbeat\n\n")
                else:
                    version = int(snapshot["version"])
                    payload = json.dumps(
                        snapshot, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    self.wfile.write(b"event: snapshot\ndata: " + payload + b"\n\n")
                self.wfile.flush()
        except OSError:
            return

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
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args: object) -> None:
        # Avoid putting URL query strings or browser-controlled values in logs.
        return


def serve_in_thread(
    host: str,
    port: int,
    state: StateStore,
    inventory: InventoryController | None = None,
) -> tuple[MonitorHttpServer, threading.Thread]:
    server = MonitorHttpServer((host, port), state, inventory)
    thread = threading.Thread(
        target=server.serve_forever, name="mocop-http", daemon=True
    )
    thread.start()
    return server, thread
