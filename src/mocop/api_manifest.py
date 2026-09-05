"""Machine-readable HTTP API contract: routes, tiers, query schemas, body caps.

``GET /api/meta`` serializes this manifest, every GET handler parses its query
through it, and the JSON 404/405 fallbacks consult it, so a route or parameter
change must land here to stay visible to agents. Repository tests compare the
manifest with ``docs/API.md`` and with live routing behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs

from . import __version__
from .config import is_safe_alias
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
    """A rejected query string carrying the stable machine-readable code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


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
        for name in ("minimum", "maximum", "default"):
            value = getattr(self, name)
            if value is not None:
                described[name] = value
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
    if set(raw) - set(schema.parameters):
        raise QueryError("unknown query parameter", "UNKNOWN_QUERY_PARAMETER")
    shape_error = QueryError(schema.shape_message, schema.shape_code)
    supplied: dict[str, str] = {}
    for name, parameter in schema.parameters.items():
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
            )
        values[name] = number
    return values


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
        entry["responseType"] = RESPONSE_TYPES.get(path, "application/json")
        endpoints.append(entry)
    return endpoints
