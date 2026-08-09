from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONFIG_ENV_VAR = "MOCOP_CONFIG"
LOCAL_CONFIG_PATH = Path("config/mocop.json")
USER_CONFIG_RELATIVE_PATH = Path("mocop/config.json")
BUNDLED_CONFIG_PATH = Path(__file__).with_name("default_config.json")
_SAFE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")


class ConfigError(ValueError):
    """Raised when monitor configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ThresholdConfig:
    cpu_warning_pct: float = 85
    memory_warning_pct: float = 90
    swap_warning_pct: float = 50
    disk_warning_pct: float = 85
    gpu_temperature_warning_c: float = 80
    gpu_busy_pct: float = 10

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    ssh_config: Path
    auto_discover: bool
    hosts: tuple[str, ...]
    exclude_hosts: frozenset[str]
    poll_interval_seconds: float
    probe_timeout_seconds: float
    connect_timeout_seconds: int
    max_workers: int
    listen_host: str
    listen_port: int
    max_output_bytes: int = 2_097_152
    history_points: int = 720
    incident_history_points: int = 500
    collection_stale_cycles: int = 3
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)


_REQUIRED_KEYS = {
    "ssh_config",
    "auto_discover",
    "hosts",
    "exclude_hosts",
    "poll_interval_seconds",
    "probe_timeout_seconds",
    "connect_timeout_seconds",
    "max_workers",
    "listen_host",
    "listen_port",
}
_OPTIONAL_KEYS = {
    "history_points",
    "incident_history_points",
    "collection_stale_cycles",
    "max_output_bytes",
    "thresholds",
}
_THRESHOLD_KEYS = {
    "cpu_warning_pct",
    "memory_warning_pct",
    "swap_warning_pct",
    "disk_warning_pct",
    "gpu_temperature_warning_c",
    "gpu_busy_pct",
}


def _bounded_number(
    data: dict[str, Any], key: str, minimum: float, maximum: float
) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{key} must be a number")
    if not minimum <= float(value) <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return float(value)


def _bounded_integer(data: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _string_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigError(f"{key} must be a list of non-empty strings")
    return tuple(dict.fromkeys(item.strip() for item in value))


def is_safe_alias(value: str) -> bool:
    return bool(_SAFE_ALIAS.fullmatch(value))


def resolve_config_path(
    explicit: Path | str | None = None,
    *,
    environ: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve configuration without depending on the source checkout layout."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    values = os.environ if environ is None else environ
    configured = values.get(CONFIG_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    xdg_root = values.get("XDG_CONFIG_HOME", "").strip()
    user_root = Path(xdg_root).expanduser() if xdg_root else Path.home() / ".config"
    user_config = (user_root / USER_CONFIG_RELATIVE_PATH).resolve()
    if user_config.is_file():
        return user_config

    project_config = ((cwd or Path.cwd()) / LOCAL_CONFIG_PATH).resolve()
    if project_config.is_file():
        return project_config
    return BUNDLED_CONFIG_PATH.resolve()


def load_config(path: Path | str | None = None) -> MonitorConfig:
    config_path = resolve_config_path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config: {config_path}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise ConfigError("config root must be a JSON object")
    missing = sorted(_REQUIRED_KEYS - data.keys())
    unknown = sorted(data.keys() - _REQUIRED_KEYS - _OPTIONAL_KEYS)
    if missing:
        raise ConfigError(f"missing config keys: {', '.join(missing)}")
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")

    if not isinstance(data["ssh_config"], str) or not data["ssh_config"].strip():
        raise ConfigError("ssh_config must be a non-empty path")
    if not isinstance(data["auto_discover"], bool):
        raise ConfigError("auto_discover must be true or false")
    if not isinstance(data["listen_host"], str) or not data["listen_host"].strip():
        raise ConfigError("listen_host must be a non-empty string")

    hosts = _string_list(data, "hosts")
    excludes = frozenset(_string_list(data, "exclude_hosts"))
    invalid_aliases = sorted(
        alias for alias in (*hosts, *excludes) if not is_safe_alias(alias)
    )
    if invalid_aliases:
        raise ConfigError(
            "host aliases must contain only letters, numbers, dots, underscores, "
            f"and hyphens: {', '.join(invalid_aliases)}"
        )
    poll_interval = _bounded_number(data, "poll_interval_seconds", 1, 3600)
    probe_timeout = _bounded_number(data, "probe_timeout_seconds", 2, 300)
    connect_timeout = _bounded_integer(data, "connect_timeout_seconds", 1, 120)
    max_output_bytes = _bounded_integer(
        {"max_output_bytes": data.get("max_output_bytes", 2_097_152)},
        "max_output_bytes",
        65_536,
        16_777_216,
    )
    max_workers = _bounded_integer(data, "max_workers", 1, 64)
    listen_port = _bounded_integer(data, "listen_port", 1, 65535)
    history_value = data.get("history_points", 720)
    if isinstance(history_value, bool) or not isinstance(history_value, int):
        raise ConfigError("history_points must be an integer")
    if not 12 <= history_value <= 8640:
        raise ConfigError("history_points must be between 12 and 8640")
    incident_history_value = data.get("incident_history_points", 500)
    if isinstance(incident_history_value, bool) or not isinstance(
        incident_history_value, int
    ):
        raise ConfigError("incident_history_points must be an integer")
    if not 20 <= incident_history_value <= 5000:
        raise ConfigError("incident_history_points must be between 20 and 5000")
    collection_stale_cycles = data.get("collection_stale_cycles", 3)
    if (
        isinstance(collection_stale_cycles, bool)
        or not isinstance(collection_stale_cycles, int)
        or not 2 <= collection_stale_cycles <= 12
    ):
        raise ConfigError("collection_stale_cycles must be between 2 and 12")

    if probe_timeout <= connect_timeout:
        raise ConfigError(
            "probe_timeout_seconds must be greater than connect_timeout_seconds"
        )

    threshold_data = data.get("thresholds", {})
    if not isinstance(threshold_data, dict):
        raise ConfigError("thresholds must be a JSON object")
    unknown_thresholds = sorted(threshold_data.keys() - _THRESHOLD_KEYS)
    if unknown_thresholds:
        raise ConfigError(f"unknown threshold keys: {', '.join(unknown_thresholds)}")
    defaults = ThresholdConfig()

    def threshold(name: str, maximum: float = 100) -> float:
        value = threshold_data.get(name, getattr(defaults, name))
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"thresholds.{name} must be a number")
        if not 0 <= float(value) <= maximum:
            raise ConfigError(f"thresholds.{name} must be between 0 and {maximum}")
        return float(value)

    thresholds = ThresholdConfig(
        cpu_warning_pct=threshold("cpu_warning_pct"),
        memory_warning_pct=threshold("memory_warning_pct"),
        swap_warning_pct=threshold("swap_warning_pct"),
        disk_warning_pct=threshold("disk_warning_pct"),
        gpu_temperature_warning_c=threshold("gpu_temperature_warning_c", 150),
        gpu_busy_pct=threshold("gpu_busy_pct"),
    )

    ssh_config = Path(data["ssh_config"]).expanduser()
    if not ssh_config.is_absolute():
        ssh_config = config_path.parent / ssh_config

    return MonitorConfig(
        ssh_config=ssh_config.resolve(),
        auto_discover=data["auto_discover"],
        hosts=hosts,
        exclude_hosts=excludes,
        poll_interval_seconds=poll_interval,
        probe_timeout_seconds=probe_timeout,
        connect_timeout_seconds=connect_timeout,
        max_output_bytes=max_output_bytes,
        max_workers=max_workers,
        listen_host=data["listen_host"].strip(),
        listen_port=listen_port,
        history_points=history_value,
        incident_history_points=incident_history_value,
        collection_stale_cycles=collection_stale_cycles,
        thresholds=thresholds,
    )
