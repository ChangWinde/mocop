"""How the API manifest is published through ``GET /api/meta``.

``api_manifest`` is the single source of routes, tiers, schemas, and error
codes; this module renders it into the self-describing document agents read,
together with the field conventions and write requirements the document
states.
"""

from __future__ import annotations

from . import __version__
from .api_manifest import (
    API_ROUTES,
    API_SCHEMA_VERSION,
    API_VERSION,
    DOCUMENTATION_URL,
    ERROR_CODES,
    QUERY_SCHEMAS,
    RESPONSE_TYPES,
    WRITE_BODY_LIMITS,
    WRITE_SCHEMAS,
)
from .models import SERVER_MESSAGE_PREFIXES, SERVER_MESSAGES

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


def describe_server_messages() -> dict[str, list[str]]:
    """The stable ``servers[].message`` vocabulary agents may branch on."""
    return {"exact": list(SERVER_MESSAGES), "prefixes": list(SERVER_MESSAGE_PREFIXES)}


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


def describe_meta(
    *,
    restart_supported: bool,
    manual_probe_supported: bool,
    configuration_write_supported: bool,
    update_supported: bool,
) -> dict[str, object]:
    """The complete ``GET /api/meta`` document for one deployment."""
    return {
        "apiVersion": API_VERSION,
        "appVersion": __version__,
        "schemaVersion": API_SCHEMA_VERSION,
        "documentation": DOCUMENTATION_URL,
        "capabilities": {
            "restartSupported": restart_supported,
            "manualProbeSupported": manual_probe_supported,
            "configurationWriteSupported": configuration_write_supported,
            "updateSupported": update_supported,
        },
        "conventions": FIELD_CONVENTIONS,
        "write": WRITE_REQUIREMENTS,
        "errorCodes": describe_error_codes(),
        "serverMessages": describe_server_messages(),
        "endpoints": describe_endpoints(),
    }
