"""Static dashboard asset routing and conditional-delivery validators.

The web handler stays in charge of writing responses; this module owns the
fixed route table, asset loading, and the strong-ETag revalidation rules so
they remain independently testable and keep the HTTP module focused on the
API surface.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

STATIC_ROOT = Path(__file__).with_name("static")

_JAVASCRIPT = "text/javascript; charset=utf-8"
STATIC_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/format.js": ("format.js", _JAVASCRIPT),
    "/csv-export.js": ("csv-export.js", _JAVASCRIPT),
    "/process-search.js": ("process-search.js", _JAVASCRIPT),
    "/gpu-tasks.js": ("gpu-tasks.js", _JAVASCRIPT),
    "/capacity-match.js": ("capacity-match.js", _JAVASCRIPT),
    "/capacity-watch.js": ("capacity-watch.js", _JAVASCRIPT),
    "/update-pill.js": ("update-pill.js", _JAVASCRIPT),
    "/dashboard-auth.js": ("dashboard-auth.js", _JAVASCRIPT),
    "/app.js": ("app.js", _JAVASCRIPT),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def load_asset(filename: str) -> bytes | None:
    """Read one routed asset; None signals an unreadable installation."""
    try:
        return (STATIC_ROOT / filename).read_bytes()
    except OSError:
        return None


def strong_etag(payload: bytes) -> str:
    """Strong content validator: revalidation stays correct across restarts
    and deployments because it depends only on the bytes served."""
    return f'"{hashlib.sha256(payload).hexdigest()}"'


def client_cache_is_current(header: str | None, etag: str) -> bool:
    """Evaluate If-None-Match with the weak comparison a W/ prefix implies."""
    if header is None:
        return False
    if header.strip() == "*":
        return True
    candidates = {value.strip().removeprefix("W/") for value in header.split(",")}
    return etag in candidates
