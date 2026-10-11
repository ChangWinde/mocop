"""Local opt-in policy; validation never reads credentials or the network."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config_errors import ConfigError


class TrainingPolicyError(ConfigError):
    pass


@dataclass(frozen=True, slots=True)
class TrainingJobsConfig:
    enabled: bool = False
    origin: str | None = None
    token_file: Path | None = None
    poll_interval_seconds: int = 30
    allow_http: bool = False


def parse_training_config(
    raw: object, base: Path, authentication: str = "bearer"
) -> TrainingJobsConfig:
    keys = {"enabled", "origin", "token_file", "poll_interval_seconds", "allow_http"}
    if not isinstance(raw, dict) or set(raw) - keys:
        raise TrainingPolicyError("training_jobs has an invalid schema")
    enabled, allow_http = raw.get("enabled", False), raw.get("allow_http", False)
    if not isinstance(enabled, bool) or not isinstance(allow_http, bool):
        raise TrainingPolicyError(
            "training_jobs.enabled and allow_http must be booleans"
        )
    interval = raw.get("poll_interval_seconds", 30)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 15 <= interval <= 300
    ):
        raise TrainingPolicyError(
            "training_jobs.poll_interval_seconds must be between 15 and 300"
        )
    origin, token_file = raw.get("origin"), raw.get("token_file")
    if origin is not None:
        if not isinstance(origin, str) or any(
            c.isspace() or ord(c) < 32 for c in origin
        ):
            raise TrainingPolicyError("training_jobs.origin must be an HTTP(S) origin")
        try:
            url = urlsplit(origin)
            port = url.port
            valid = (
                url.scheme in ("https", "http")
                and url.hostname
                and not url.username
                and not url.password
                and url.path in ("", "/")
                and not url.query
                and not url.fragment
                and (port is None or 1 <= port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid or (url.scheme == "http" and not allow_http):
            raise TrainingPolicyError(
                "training_jobs.origin requires HTTPS unless allow_http is true"
            )
        origin = origin.rstrip("/")
    if token_file is not None:
        if (
            not isinstance(token_file, str)
            or not token_file
            or any(ord(c) < 32 for c in token_file)
        ):
            raise TrainingPolicyError(
                "training_jobs.token_file must be a filesystem path"
            )
        try:
            token_file = Path(token_file).expanduser()
            if not token_file.is_absolute():
                token_file = base / token_file
        except (ValueError, RuntimeError):
            raise TrainingPolicyError(
                "training_jobs.token_file must be a filesystem path"
            ) from None
    if enabled and (origin is None or token_file is None):
        raise TrainingPolicyError(
            "enabled training_jobs requires origin and token_file"
        )
    if enabled and authentication != "bearer":
        raise TrainingPolicyError("training_jobs requires bearer authentication")
    return TrainingJobsConfig(enabled, origin, token_file, interval, allow_http)
