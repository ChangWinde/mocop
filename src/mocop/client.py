"""Local read-only HTTP client behind ``mocop api``.

An agent on the monitor's own host should not have to discover the listen
address, locate the private capability file, or spell the Bearer header to
ask the running service a question. This module does exactly that plumbing
for the public and authenticated GET routes and nothing more: the reader and
writer tiers stay reserved for the same-origin dashboard, because their
marker header changes the collection cadence and their writes are protected
by the browser's origin checks.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import __version__
from .api_manifest import API_ROUTES, EVENT_STREAM_RESPONSE_TYPE
from .config import ConfigError, MonitorConfig, load_config, resolve_config_path
from .lifecycle import LifecycleError, access_token_path, read_access_token

_WILDCARD_BINDS = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}
# No path is routed for both methods, so one tier per path is exact.
_ROUTE_ACCESS = {path: access for _method, path, access in API_ROUTES}
_PUBLIC = "public"
_DASHBOARD_TIERS = frozenset({"reader", "writer"})


class ApiClientError(Exception):
    """A request that could not be made; ``exit_code`` follows the CLI table."""

    def __init__(self, message: str, code: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    content_type: str
    body: bytes = b""
    lines: Iterator[bytes] | None = None

    @property
    def streaming(self) -> bool:
        return self.lines is not None


def service_url(config: MonitorConfig) -> str:
    """The loopback-reachable base URL of the configured listener."""
    host = _WILDCARD_BINDS.get(config.listen_host.strip().lower(), config.listen_host)
    authority = f"[{host.replace('%', '%25')}]" if ":" in host else host
    return f"http://{authority}:{config.listen_port}"


def route_access(path: str) -> str | None:
    """The manifest access tier of a route, or ``None`` when unrouted."""
    return _ROUTE_ACCESS.get(path)


def request(
    target: str,
    *,
    config_path: Path | str | None,
    token_file: Path | None = None,
    timeout: float = 10.0,
) -> ApiResponse:
    """GET ``target`` (an absolute API path with optional query) from the service.

    Raises :class:`ApiClientError` for anything that never reached the
    server; HTTP error statuses come back as a normal :class:`ApiResponse`
    so the caller can print the server's own JSON error envelope.
    """
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or not target.startswith("/"):
        raise ApiClientError(
            "target must be an absolute API path such as /api/snapshot",
            "INVALID_TARGET",
        )
    access = route_access(parsed.path)
    if access in _DASHBOARD_TIERS:
        raise ApiClientError(
            f"{parsed.path} is a dashboard-only route: reader routes carry the "
            "marker that switches the service to the attended collection "
            "cadence and writes are protected by the browser's same-origin "
            "checks; open the dashboard instead",
            "DASHBOARD_ONLY",
        )
    try:
        resolved = resolve_config_path(config_path)
        config = load_config(resolved)
    except ConfigError as exc:
        raise ApiClientError(str(exc), exc.code) from exc
    headers = {"User-Agent": f"mocop-cli/{__version__}"}
    if access != _PUBLIC:
        try:
            token = read_access_token(token_file or access_token_path(resolved))
        except LifecycleError as exc:
            raise ApiClientError(
                f"{exc}; a managed service keeps the capability beside its "
                "configuration, a foreground run prints it once (save it and "
                "pass --token-file)",
                "TOKEN_UNAVAILABLE",
            ) from exc
        headers["Authorization"] = f"Bearer {token}"
    url = f"{service_url(config)}{target}"
    try:
        response = urlopen(Request(url, headers=headers), timeout=timeout)
    except HTTPError as error:
        with error:
            return ApiResponse(
                error.code, error.headers.get("Content-Type", ""), error.read()
            )
    except (URLError, OSError) as exc:
        raise ApiClientError(
            f"{url} is not answering ({exc.reason if isinstance(exc, URLError) else exc}); "
            "check `mocop service status`",
            "CONNECTION_FAILED",
            exit_code=1,
        ) from exc
    content_type = response.headers.get("Content-Type", "")
    if content_type.startswith(EVENT_STREAM_RESPONSE_TYPE):
        return ApiResponse(response.status, content_type, lines=_stream_lines(response))
    with response:
        return ApiResponse(response.status, content_type, response.read())


def _stream_lines(response: HTTPResponse) -> Iterator[bytes]:
    with response:
        yield from response


def write_response(response: ApiResponse, stream: BinaryIO) -> None:
    """Copy the body to ``stream``; event streams are forwarded line by line."""
    if response.lines is not None:
        for line in response.lines:
            stream.write(line)
            stream.flush()
        return
    stream.write(response.body)
    if response.body and not response.body.endswith(b"\n"):
        stream.write(b"\n")
    stream.flush()
