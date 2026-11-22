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
    "/incident-text.js": ("incident-text.js", _JAVASCRIPT),
    "/diagnosis-text.js": ("diagnosis-text.js", _JAVASCRIPT),
    "/api-contracts.js": ("api-contracts.js", _JAVASCRIPT),
    "/keyed-loader.js": ("keyed-loader.js", _JAVASCRIPT),
    "/event-stream.js": ("event-stream.js", _JAVASCRIPT),
    "/csv-export.js": ("csv-export.js", _JAVASCRIPT),
    "/process-search.js": ("process-search.js", _JAVASCRIPT),
    "/process-inventory.js": ("process-inventory.js", _JAVASCRIPT),
    "/gpu-tasks.js": ("gpu-tasks.js", _JAVASCRIPT),
    "/capacity-match.js": ("capacity-match.js", _JAVASCRIPT),
    "/capacity-watch.js": ("capacity-watch.js", _JAVASCRIPT),
    "/update-pill.js": ("update-pill.js", _JAVASCRIPT),
    "/dashboard-auth.js": ("dashboard-auth.js", _JAVASCRIPT),
    "/background-asset.js": ("background-asset.js", _JAVASCRIPT),
    "/attention-groups.js": ("attention-groups.js", _JAVASCRIPT),
    "/attention.js": ("attention.js", _JAVASCRIPT),
    "/owner-usage.js": ("owner-usage.js", _JAVASCRIPT),
    "/preferences.js": ("preferences.js", _JAVASCRIPT),
    "/fleet-state.js": ("fleet-state.js", _JAVASCRIPT),
    "/use-check.js": ("use-check.js", _JAVASCRIPT),
    "/gpu-jobs.js": ("gpu-jobs.js", _JAVASCRIPT),
    "/alert-noise.js": ("alert-noise.js", _JAVASCRIPT),
    "/self-check.js": ("self-check.js", _JAVASCRIPT),
    "/command-palette.js": ("command-palette.js", _JAVASCRIPT),
    "/palette-view.js": ("palette-view.js", _JAVASCRIPT),
    "/trend-charts.js": ("trend-charts.js", _JAVASCRIPT),
    "/usage-view.js": ("usage-view.js", _JAVASCRIPT),
    "/jobs-view.js": ("jobs-view.js", _JAVASCRIPT),
    "/alerts-view.js": ("alerts-view.js", _JAVASCRIPT),
    "/topology-view.js": ("topology-view.js", _JAVASCRIPT),
    "/inventory-forms.js": ("inventory-forms.js", _JAVASCRIPT),
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
