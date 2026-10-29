"""Additive training, personal-storage and desktop terminal API contracts."""

from .api_schema import BodyField, BodySchema, QueryParameter, QuerySchema
from .terminal import TERMINAL_APP_ID_MAX_LENGTH

OBSERVABILITY_ROUTES = (
    ("GET", "/api/user-memory", "authenticated"),
    ("GET", "/api/directory-disk", "authenticated"),
    ("GET", "/api/storage-analysis", "authenticated"),
    ("GET", "/api/training-jobs", "authenticated"),
    ("GET", "/api/training-job-detail", "authenticated"),
    ("POST", "/api/terminal", "writer"),
)

OBSERVABILITY_QUERIES = {
    "/api/training-job-detail": QuerySchema(
        {"id": QueryParameter("text", required=True)},
        shape_message="one job id is required",
        shape_code="INVALID_QUERY",
    ),
}

OBSERVABILITY_WRITES = {
    "/api/terminal": BodySchema(
        {
            "host": BodyField("alias"),
            "app": BodyField(
                "text",
                maximum=TERMINAL_APP_ID_MAX_LENGTH,
                notes="one id from capabilities.terminalApps; empty string "
                "selects the first listed application",
            ),
        }
    ),
}

OBSERVABILITY_ERRORS = (
    ("TERMINAL_LAUNCH_FAILED", 500),
    ("TRAINING_INVALID_ID", 400),
    ("TRAINING_AUTH_REQUIRED", 401),
    ("TRAINING_ACCESS_DENIED", 403),
    ("TRAINING_NOT_FOUND", 404),
    ("TRAINING_RATE_LIMITED", 429),
    ("TRAINING_READ_FAILED", 503),
)
