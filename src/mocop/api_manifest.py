"""Machine-readable HTTP API contract: routes, tiers, query/body schemas.

``GET /api/meta`` serializes this manifest, every GET handler parses its query
through it, every POST handler validates its body through it, and the JSON
404/405 fallbacks consult it, so a route or parameter change must land here to
stay visible to agents. The error-code catalog lives here too, so an agent can
call any route without opening ``docs/API.md``. Repository tests compare the
manifest with that reference and with live routing behaviour.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs

from . import __version__
from .config import (
    HOST_GROUP_MAX_LENGTH,
    INCIDENT_ACTION_KEY_MAX_LENGTH,
    INCIDENT_ACTION_REASON_MAX_LENGTH,
    MAINTENANCE_REASON_MAX_LENGTH,
    is_safe_alias,
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


class QueryError(ValueError):
    """A rejected query string: the stable code and, when one parameter is at
    fault, its name."""

    def __init__(self, message: str, code: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class QueryParameter:
    """One accepted query parameter.

    ``alias`` values must be safe SSH aliases, ``identity`` values are bounded
    printable GPU identities, and ``text`` values are bounded printable free
    text; all three raise the route's shape code when malformed. ``integer``
    values raise their own ``invalid_code`` when they are not integers inside
    ``[minimum, maximum]``.
    """

    kind: str
    required: bool = False
    minimum: int | None = None
    maximum: int | None = None
    default: int | str | None = None
    invalid_code: str | None = None

    def describe(self) -> dict[str, object]:
        described: dict[str, object] = {"type": self.kind, "required": self.required}
        if self.minimum is not None:
            described["minimum"] = self.minimum
        if self.maximum is not None:
            described["maximum"] = self.maximum
        if self.default is not None:
            described["default"] = self.default
        return described


@dataclass(frozen=True, slots=True)
class QuerySchema:
    parameters: dict[str, QueryParameter]
    shape_code: str
    shape_message: str

    def describe(self) -> dict[str, object]:
        return {
            name: parameter.describe() for name, parameter in self.parameters.items()
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


def _valid_text(value: str) -> bool:
    return 1 <= len(value) <= 128 and not any(
        ord(character) < 32 for character in value
    )


def parse_query(schema: QuerySchema, query: str) -> dict[str, object]:
    """Validate a raw query string against ``schema``.

    Unknown names, then every cardinality and string-shape problem (route
    shape code), then integer parsing and bounds (per-parameter code): the
    same precedence the routes have always documented. Absent optional
    integers take their default; absent optional strings are ``None``.
    """
    raw = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(raw) - set(schema.parameters))
    if unknown:
        raise QueryError(
            "unknown query parameter", "UNKNOWN_QUERY_PARAMETER", unknown[0]
        )
    supplied: dict[str, str] = {}
    for name, parameter in schema.parameters.items():
        shape_error = QueryError(schema.shape_message, schema.shape_code, name)
        given = raw.get(name, [])
        if len(given) > 1 or (parameter.required and not given):
            raise shape_error
        if not given:
            continue
        if parameter.kind == "alias" and not is_safe_alias(given[0]):
            raise shape_error
        if parameter.kind in {"identity", "text"} and not _valid_text(given[0]):
            raise shape_error
        supplied[name] = given[0]
    values: dict[str, object] = {}
    for name, parameter in schema.parameters.items():
        if name not in supplied:
            values[name] = parameter.default
            continue
        if parameter.kind != "integer":
            values[name] = supplied[name]
            continue
        assert parameter.invalid_code is not None
        assert parameter.minimum is not None and parameter.maximum is not None
        try:
            number = int(supplied[name])
        except ValueError:
            number = parameter.minimum - 1
        if not parameter.minimum <= number <= parameter.maximum:
            raise QueryError(
                f"{name} must be between {parameter.minimum} and {parameter.maximum}",
                parameter.invalid_code,
                name,
            )
        values[name] = number
    return values


# Dashboard writes accept these exact duration values (seconds); 0 clears.
DASHBOARD_DURATIONS: frozenset[int] = frozenset({0, 3_600, 14_400, 86_400, 604_800})
_DURATION_VALUES = tuple(sorted(DASHBOARD_DURATIONS))
_ENUM_ACTIONS = ("acknowledged", "silenced", "clear")


class BodyError(ValueError):
    """A rejected write body: the stable code and, when one field is at fault,
    its name."""

    def __init__(self, message: str, code: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class BodyField:
    """One accepted JSON field on a write route.

    ``alias`` and ``enum`` are strings restricted to the safe alias grammar or
    ``values``; ``integer`` and ``number`` are non-boolean JSON numbers inside
    ``values`` or ``[minimum, maximum]``; ``text`` is a string whose stripped
    length may not exceed ``maximum`` (the route enforces its own character
    grammar); ``timestamp`` is a string, or ``null`` when ``nullable``.
    """

    kind: str
    required: bool = True
    nullable: bool = False
    values: tuple[object, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    notes: str | None = None

    def describe(self) -> dict[str, object]:
        described: dict[str, object] = {"type": self.kind, "required": self.required}
        if self.nullable:
            described["nullable"] = True
        if self.values is not None:
            described["values"] = list(self.values)
        if self.minimum is not None:
            described["minimum"] = self.minimum
        if self.maximum is not None:
            described["maximum"] = self.maximum
        if self.notes is not None:
            described["notes"] = self.notes
        return described

    def well_typed(self, value: object) -> bool:
        if value is None:
            return self.nullable
        if self.kind in {"alias", "enum", "text", "timestamp"}:
            return isinstance(value, str)
        if isinstance(value, bool):
            return False
        if self.kind == "integer":
            return isinstance(value, int)
        return isinstance(value, int | float)

    def accepts(self, value: object) -> bool:
        """Value-level check for a well-typed value."""
        if value is None:
            return True
        if self.kind == "alias":
            return is_safe_alias(value)
        if self.kind == "text":
            return self.maximum is None or len(value.strip()) <= self.maximum
        if self.values is not None:
            return value in self.values
        if self.minimum is None:
            return True
        try:
            numeric = float(value)
        except OverflowError:
            # JSON integers have unbounded precision; huge ones are invalid.
            return False
        assert self.maximum is not None
        return math.isfinite(numeric) and self.minimum <= value <= self.maximum


@dataclass(frozen=True, slots=True)
class BodySchema:
    """The JSON object a write route accepts.

    ``empty`` is the exact ``{}`` body used by restart, update, and the
    notification test. ``exact_keys`` is False only for collector settings,
    which take a non-empty subset of the published fields.
    """

    fields: dict[str, BodyField]
    exact_keys: bool = True
    empty: bool = False

    def describe(self) -> dict[str, object]:
        if self.empty:
            return {"type": "object", "empty": True}
        return {
            "type": "object",
            "exactKeys": self.exact_keys,
            "fields": {name: field.describe() for name, field in self.fields.items()},
        }

    def accepts_keys(self, keys: Iterable[str]) -> bool:
        supplied = set(keys)
        if self.empty:
            return not supplied
        if self.exact_keys:
            return supplied == set(self.fields)
        return bool(supplied) and supplied <= set(self.fields)


def validate_body(schema: BodySchema, payload: object) -> dict[str, object]:
    """Check a parsed JSON body against ``schema`` and return it as a dict.

    Anything but an object, a key set the schema does not accept, or a field
    of the wrong JSON type is ``INVALID_SCHEMA``; a well-typed value outside
    the published alias grammar, ``values``, bounds, or text length is
    ``INVALID_SETTINGS``. Cross-field rules stay with the route handler.
    """
    if not isinstance(payload, dict) or not schema.accepts_keys(payload):
        raise BodyError(
            "request body does not match the route schema", "INVALID_SCHEMA"
        )
    for name, value in payload.items():
        field = schema.fields[name]
        if not field.well_typed(value):
            raise BodyError(f"{name} has the wrong type", "INVALID_SCHEMA", name)
        if not field.accepts(value):
            raise BodyError(
                f"{name} is not an accepted value", "INVALID_SETTINGS", name
            )
    return payload


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
