"""Machine-readable HTTP API contract: routes, tiers, query/body schemas.

``GET /api/meta`` serializes this manifest, every GET handler parses its query
through it, every POST handler validates its body through it, and the JSON
404/405 fallbacks consult it, so a route or parameter change must land here to
stay visible to agents. The error-code catalog lives here too, so an agent can
call any route without opening ``docs/API.md``. Repository tests compare the
manifest with that reference and with live routing behaviour.
"""

from __future__ import annotations

from . import __version__
from .api_schema import BodyField, BodySchema, QueryParameter, QuerySchema
from .config import (
    HOST_GROUP_MAX_LENGTH,
    INCIDENT_ACTION_KEY_MAX_LENGTH,
    INCIDENT_ACTION_REASON_MAX_LENGTH,
    MAINTENANCE_REASON_MAX_LENGTH,
)
from .metrics import OPENMETRICS_CONTENT_TYPE

API_VERSION = "2"
API_SCHEMA_VERSION = 1
DOCUMENTATION_URL = (
    f"https://github.com/ChangWinde/mocop/blob/v{__version__}/docs/API.md"
)
EVENT_STREAM_RESPONSE_TYPE = "text/event-stream"

# Access levels: public = unauthenticated discovery/health, authenticated =
# Bearer read, reader = Bearer plus dashboard marker, writer = Bearer
# same-origin write.
API_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/api/snapshot", "authenticated"),
    ("GET", "/api/events", "authenticated"),
    ("GET", "/api/history", "authenticated"),
    ("GET", "/api/usage", "authenticated"),
    ("GET", "/api/capacity", "authenticated"),
    ("GET", "/api/incidents", "authenticated"),
    ("GET", "/api/meta", "public"),
    ("GET", "/healthz", "public"),
    ("GET", "/readyz", "public"),
    ("GET", "/metrics", "authenticated"),
    ("GET", "/api/gpu-history", "reader"),
    ("GET", "/api/diagnostics", "reader"),
    ("GET", "/api/inventory", "reader"),
    ("GET", "/api/topology", "reader"),
    ("GET", "/api/update", "reader"),
    ("POST", "/api/update/apply", "writer"),
    ("POST", "/api/settings/collector", "writer"),
    ("POST", "/api/settings/hosts", "writer"),
    ("POST", "/api/settings/maintenance", "writer"),
    ("POST", "/api/settings/host-group", "writer"),
    ("POST", "/api/settings/incident-action", "writer"),
    ("POST", "/api/probe", "writer"),
    ("POST", "/api/notifications/test", "writer"),
    ("POST", "/api/service/restart", "writer"),
)

ROUTE_METHODS: dict[str, frozenset[str]] = {
    path: frozenset(
        route_method for route_method, route_path, _ in API_ROUTES if route_path == path
    )
    for _, path, _ in API_ROUTES
}

# Every write takes one JSON object; the cap is the whole request body.
WRITE_BODY_LIMITS: dict[str, int] = {
    "/api/settings/collector": 512,
    "/api/settings/hosts": 512,
    "/api/settings/maintenance": 512,
    "/api/settings/host-group": 512,
    "/api/service/restart": 32,
    "/api/update/apply": 32,
    "/api/settings/incident-action": 1024,
    "/api/probe": 512,
    "/api/notifications/test": 32,
}

RESPONSE_TYPES: dict[str, str] = {
    "/api/events": EVENT_STREAM_RESPONSE_TYPE,
    "/metrics": OPENMETRICS_CONTENT_TYPE,
}


def _integer(
    minimum: int, maximum: int, default: int, invalid_code: str
) -> QueryParameter:
    return QueryParameter(
        "integer",
        minimum=minimum,
        maximum=maximum,
        default=default,
        invalid_code=invalid_code,
    )


QUERY_SCHEMAS: dict[str, QuerySchema] = {
    "/api/history": QuerySchema(
        {
            "host": QueryParameter("alias", required=True),
            "limit": _integer(2, 300, 120, "INVALID_LIMIT"),
        },
        "INVALID_QUERY",
        "invalid host or limit",
    ),
    "/api/usage": QuerySchema(
        {
            "hours": _integer(1, 720, 24, "INVALID_HOURS"),
            "limit": _integer(1, 500, 50, "INVALID_LIMIT"),
        },
        "INVALID_QUERY",
        "invalid hours or limit",
    ),
    "/api/gpu-history": QuerySchema(
        {
            "host": QueryParameter("alias", required=True),
            "gpu": QueryParameter("identity", required=True),
            "limit": _integer(2, 300, 120, "INVALID_LIMIT"),
        },
        "INVALID_QUERY",
        "invalid host, GPU, or limit",
    ),
    "/api/diagnostics": QuerySchema(
        {"host": QueryParameter("alias")},
        "INVALID_HOST",
        "invalid host",
    ),
    "/api/incidents": QuerySchema(
        {"limit": _integer(1, 200, 50, "INVALID_LIMIT")},
        "INVALID_LIMIT",
        "invalid limit",
    ),
    "/api/capacity": QuerySchema(
        {
            "gpus": _integer(1, 256, 1, "INVALID_CAPACITY_REQUEST"),
            "min_vram_gib": _integer(0, 512, 0, "INVALID_CAPACITY_REQUEST"),
            "model": QueryParameter("text", default="any"),
        },
        "INVALID_CAPACITY_REQUEST",
        "invalid gpus, min_vram_gib, or model",
    ),
}


# Dashboard writes accept these exact duration values (seconds); 0 clears.
DASHBOARD_DURATIONS: frozenset[int] = frozenset({0, 3_600, 14_400, 86_400, 604_800})
_DURATION_VALUES = tuple(sorted(DASHBOARD_DURATIONS))
_ENUM_ACTIONS = ("acknowledged", "silenced", "clear")


WRITE_SCHEMAS: dict[str, BodySchema] = {
    "/api/settings/collector": BodySchema(
        {
            "pollIntervalSeconds": BodyField(
                "number", required=False, minimum=2, maximum=60
            ),
            "probeTimeoutSeconds": BodyField(
                "number",
                required=False,
                minimum=2,
                maximum=300,
                notes="must exceed the configured SSH connect timeout",
            ),
            "maxWorkers": BodyField("integer", required=False, minimum=1, maximum=64),
        },
        exact_keys=False,
    ),
    "/api/settings/hosts": BodySchema(
        {
            "action": BodyField("enum", values=("add", "remove")),
            "host": BodyField("alias"),
        }
    ),
    "/api/settings/maintenance": BodySchema(
        {
            "host": BodyField("alias"),
            "durationSeconds": BodyField("integer", values=_DURATION_VALUES),
            "reason": BodyField(
                "text",
                maximum=MAINTENANCE_REASON_MAX_LENGTH,
                notes="required unless durationSeconds is 0",
            ),
        }
    ),
    "/api/settings/host-group": BodySchema(
        {
            "host": BodyField("alias"),
            "group": BodyField(
                "text",
                maximum=HOST_GROUP_MAX_LENGTH,
                notes="empty string clears the group",
            ),
        }
    ),
    "/api/settings/incident-action": BodySchema(
        {
            "host": BodyField("alias"),
            "conditionKey": BodyField("text", maximum=INCIDENT_ACTION_KEY_MAX_LENGTH),
            "incidentStartedAt": BodyField(
                "timestamp",
                nullable=True,
                notes="the condition's firstObservedAt; null if and only if "
                "action is clear",
            ),
            "action": BodyField("enum", values=_ENUM_ACTIONS),
            "durationSeconds": BodyField(
                "integer",
                values=_DURATION_VALUES,
                notes="0 if and only if action is clear",
            ),
            "reason": BodyField("text", maximum=INCIDENT_ACTION_REASON_MAX_LENGTH),
        }
    ),
    "/api/probe": BodySchema({"host": BodyField("alias")}),
    "/api/service/restart": BodySchema({}, empty=True),
    "/api/update/apply": BodySchema({}, empty=True),
    "/api/notifications/test": BodySchema({}, empty=True),
}

# Stable HTTP error codes and their status. /api/meta publishes this list;
# docs/API.md and the handler string literals must stay in lockstep.
ERROR_CODES: tuple[tuple[str, int], ...] = (
    ("INVALID_REQUEST_AUTHORITY", 400),
    ("INVALID_REQUEST_FRAMING", 400),
    ("INVALID_REQUEST_TARGET", 400),
    ("REQUEST_BODY_NOT_ALLOWED", 400),
    ("QUERY_NOT_ALLOWED", 400),
    ("UNKNOWN_QUERY_PARAMETER", 400),
    ("INVALID_QUERY", 400),
    ("INVALID_LIMIT", 400),
    ("INVALID_HOURS", 400),
    ("INVALID_CAPACITY_REQUEST", 400),
    ("INVALID_HOST", 400),
    ("INVALID_JSON", 400),
    ("INVALID_SCHEMA", 400),
    ("INVALID_SETTINGS", 400),
    ("UNTRUSTED_ORIGIN", 403),
    ("AUTHENTICATION_REQUIRED", 403),
    ("NOT_FOUND", 404),
    ("UNKNOWN_HOST", 404),
    ("UNKNOWN_GPU", 404),
    ("METHOD_NOT_ALLOWED", 405),
    ("PROBE_IN_PROGRESS", 409),
    ("UPDATE_NOT_APPLICABLE", 409),
    ("INVENTORY_CHANGED", 409),
    ("INCIDENT_NOT_ACTIVE", 409),
    ("PAYLOAD_TOO_LARGE", 413),
    ("UNSUPPORTED_MEDIA_TYPE", 415),
    ("RATE_LIMITED", 429),
    ("SERVICE_UNAVAILABLE", 503),
    ("METRICS_LIMIT_EXCEEDED", 503),
    ("NOTIFICATIONS_DISABLED", 503),
    ("CONNECTION_LIMIT", 503),
)

FIELD_CONVENTIONS = {
    "envelope": "camelCase",
    "telemetry": "snake_case",
    "incidentActionWrite": "camelCase",
    "incidentActionStored": "snake_case",
}

WRITE_REQUIREMENTS = {
    "contentType": "application/json",
    "authorization": "Bearer",
    "sameOrigin": True,
    "dashboardMarker": "X-Monitor-Request: dashboard",
}


def describe_error_codes() -> list[dict[str, object]]:
    return [{"code": code, "status": status} for code, status in ERROR_CODES]


def describe_endpoints() -> list[dict[str, object]]:
    """The endpoint manifest ``/api/meta`` publishes."""
    endpoints: list[dict[str, object]] = []
    for method, path, access in API_ROUTES:
        entry: dict[str, object] = {"method": method, "path": path, "access": access}
        if method == "GET":
            schema = QUERY_SCHEMAS.get(path)
            entry["query"] = schema.describe() if schema is not None else {}
        else:
            entry["bodyLimitBytes"] = WRITE_BODY_LIMITS[path]
            entry["body"] = WRITE_SCHEMAS[path].describe()
        entry["responseType"] = RESPONSE_TYPES.get(path, "application/json")
        endpoints.append(entry)
    return endpoints
