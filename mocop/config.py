from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

CONFIG_ENV_VAR = "MOCOP_CONFIG"
LOCAL_CONFIG_PATH = Path("config/mocop.json")
USER_CONFIG_RELATIVE_PATH = Path("mocop/config.json")
BUNDLED_CONFIG_PATH = Path(__file__).with_name("default_config.json")
_SAFE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
MAINTENANCE_REASON_MAX_LENGTH = 120
INCIDENT_ACTION_REASON_MAX_LENGTH = 120
INCIDENT_ACTION_KEY_MAX_LENGTH = 512
INCIDENT_ACTION_MAX_ENTRIES = 512
HOST_GROUP_MAX_LENGTH = 48
TOPOLOGY_LABEL_MAX_LENGTH = 64
TOPOLOGY_MAX_LINKS = 512
TOPOLOGY_TRANSPORTS = frozenset({"ssh", "frp-stcp", "frp-xtcp", "vpn"})
TRUSTED_WEB_HOSTS_MAX_ENTRIES = 32
CONFIG_MAX_BYTES = 1_048_576
CONFIG_MAX_HOST_ALIASES = 1_024


class ConfigError(ValueError):
    """Raised when monitor configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ThresholdConfig:
    cpu_warning_pct: float = 85
    memory_warning_pct: float = 90
    swap_warning_pct: float = 50
    disk_warning_pct: float = 85
    # Absolute headroom below which an already-alerting filesystem is critical,
    # however large it is; a small partition under the percentage threshold is
    # unaffected.
    disk_min_free_gib: float = 5
    # Pressure stall warnings fire on the 60-second "some" average: the share
    # of the last minute during which at least one task was stalled on the
    # resource. Twice the warning value escalates to critical.
    psi_memory_some_pct: float = 20
    psi_io_some_pct: float = 30
    gpu_temperature_warning_c: float = 80
    gpu_busy_pct: float = 10
    gpu_memory_warning_pct: float = 90
    gpu_idle_memory_pct: float = 20

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class IncidentConfig:
    resource_open_cycles: int = 2
    recovery_cycles: int = 2
    gpu_idle_memory_cycles: int = 12


@dataclass(frozen=True, slots=True)
class IncidentActionConfig:
    host: str
    condition_key: str
    action: str
    until: datetime
    reason: str = ""
    incident_started_at: str | None = None

    def is_active(self, at: datetime | None = None) -> bool:
        return self.until > (at or datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, str | None]:
        return {
            "host": self.host,
            "condition_key": self.condition_key,
            "action": self.action,
            "until": self.until.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "reason": self.reason,
            "incident_started_at": self.incident_started_at,
        }


@dataclass(frozen=True, slots=True)
class IncidentScopeOverrideConfig:
    thresholds: tuple[tuple[str, float], ...] = ()
    exclude_disk_mounts: frozenset[str] = frozenset()

    def threshold(self, name: str) -> float | None:
        return next((value for key, value in self.thresholds if key == name), None)


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    enabled: bool = False
    retention_hours: int = 168
    max_bytes: int = 134_217_728


@dataclass(frozen=True, slots=True)
class WorkloadConfig:
    mode: str = "disabled"


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    name: str
    url_env: str
    secret_env: str | None = None
    events: tuple[str, ...] = ("opened", "resolved", "escalated", "deescalated")
    timeout_seconds: float = 5
    max_attempts: int = 3
    retry_base_seconds: float = 1
    min_interval_seconds: float = 1
    allow_private_networks: bool = False


@dataclass(frozen=True, slots=True)
class HostOverrideConfig:
    poll_interval_seconds: float | None = None
    probe_timeout_seconds: float | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceWindowConfig:
    """One-shot (absolute `until`) or weekly recurring silence window.

    Recurring windows are defined in UTC: `weekday` follows Python's Monday=0
    convention, `start_minutes` counts from UTC midnight, and the duration is
    bounded below one week so instances can never overlap themselves.
    """

    reason: str
    until: datetime | None = None
    weekday: int | None = None
    start_minutes: int | None = None
    duration_minutes: int | None = None

    @property
    def recurring(self) -> bool:
        return self.weekday is not None

    def _instance_end(self, now: datetime) -> datetime:
        """Return the end of the active instance, or of the next one."""
        assert self.weekday is not None
        assert self.start_minutes is not None
        assert self.duration_minutes is not None
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days_back = (now.weekday() - self.weekday) % 7
        start = (
            midnight - timedelta(days=days_back) + timedelta(minutes=self.start_minutes)
        )
        if start > now:
            start -= timedelta(days=7)
        end = start + timedelta(minutes=self.duration_minutes)
        if end <= now:
            end = start + timedelta(days=7, minutes=self.duration_minutes)
        return end

    def is_active(self, at: datetime | None = None) -> bool:
        now = at or datetime.now(timezone.utc)
        if not self.recurring:
            assert self.until is not None
            return self.until > now
        assert self.duration_minutes is not None
        end = self._instance_end(now)
        return end - timedelta(minutes=self.duration_minutes) <= now < end

    def to_dict(self, at: datetime | None = None) -> dict[str, object]:
        if not self.recurring:
            assert self.until is not None
            return {
                "until": self.until.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
                "reason": self.reason,
            }
        now = at or datetime.now(timezone.utc)
        end = self._instance_end(now)
        return {
            "until": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "reason": self.reason,
            "recurring": True,
        }


@dataclass(frozen=True, slots=True)
class TopologyLinkConfig:
    source: str
    target: str
    transport: str
    label: str | None = None

    def to_dict(self) -> dict[str, str]:
        value = {
            "source": self.source,
            "target": self.target,
            "transport": self.transport,
        }
        if self.label is not None:
            value["label"] = self.label
        return value


@dataclass(frozen=True, slots=True)
class ConnectionTopologyConfig:
    root: str
    links: tuple[TopologyLinkConfig, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "links": [link.to_dict() for link in self.links],
        }


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
    trusted_web_hosts: tuple[str, ...] = ()
    gpu_process_poll_interval_seconds: float = 15
    retry_jitter_pct: float = 15
    manual_probe_cooldown_seconds: float = 5
    local_host: str | None = None
    max_output_bytes: int = 2_097_152
    history_points: int = 720
    incident_history_points: int = 500
    collection_stale_cycles: int = 3
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    expected_gpu_counts: tuple[tuple[str, int], ...] = ()
    incidents: IncidentConfig = field(default_factory=IncidentConfig)
    incident_actions: tuple[IncidentActionConfig, ...] = ()
    host_incident_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...] = ()
    group_incident_overrides: tuple[tuple[str, IncidentScopeOverrideConfig], ...] = ()
    host_overrides: tuple[tuple[str, HostOverrideConfig], ...] = ()
    maintenance_windows: tuple[tuple[str, MaintenanceWindowConfig], ...] = ()
    host_groups: tuple[tuple[str, str], ...] = ()
    topology: ConnectionTopologyConfig | None = None
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    workloads: WorkloadConfig = field(default_factory=WorkloadConfig)
    webhooks: tuple[WebhookConfig, ...] = ()
    _host_override_index: Mapping[str, HostOverrideConfig] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _maintenance_window_index: Mapping[str, MaintenanceWindowConfig] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _host_group_index: Mapping[str, str] = field(
        init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_host_override_index",
            MappingProxyType(dict(self.host_overrides)),
        )
        object.__setattr__(
            self,
            "_maintenance_window_index",
            MappingProxyType(dict(self.maintenance_windows)),
        )
        object.__setattr__(
            self,
            "_host_group_index",
            MappingProxyType(dict(self.host_groups)),
        )

    def host_override(self, host: str) -> HostOverrideConfig | None:
        return self._host_override_index.get(host)

    def host_display_names(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (alias, override.display_name)
            for alias, override in self.host_overrides
            if override.display_name is not None
        )

    def maintenance_window(self, host: str) -> MaintenanceWindowConfig | None:
        return self._maintenance_window_index.get(host)

    def host_group(self, host: str) -> str | None:
        return self._host_group_index.get(host)


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
    "local_host",
    "trusted_web_hosts",
    "history_points",
    "incident_history_points",
    "collection_stale_cycles",
    "gpu_process_poll_interval_seconds",
    "retry_jitter_pct",
    "manual_probe_cooldown_seconds",
    "max_output_bytes",
    "thresholds",
    "expected_gpu_counts",
    "incidents",
    "host_overrides",
    "maintenance_windows",
    "host_groups",
    "topology",
    "persistence",
    "workloads",
    "webhooks",
    "incident_actions",
    "incident_overrides",
}
_THRESHOLD_KEYS = {
    "cpu_warning_pct",
    "memory_warning_pct",
    "swap_warning_pct",
    "disk_warning_pct",
    "disk_min_free_gib",
    "psi_memory_some_pct",
    "psi_io_some_pct",
    "gpu_temperature_warning_c",
    "gpu_busy_pct",
    "gpu_memory_warning_pct",
    "gpu_idle_memory_pct",
}
_INCIDENT_KEYS = {
    "resource_open_cycles",
    "recovery_cycles",
    "gpu_idle_memory_cycles",
}
_HOST_OVERRIDE_KEYS = {"poll_interval_seconds", "probe_timeout_seconds", "display_name"}
_MAINTENANCE_WINDOW_KEYS = {"until", "reason", "recurrence"}
_MAINTENANCE_RECURRENCE_KEYS = {"weekday", "start", "duration_minutes"}
_RECURRENCE_START = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DISPLAY_NAME_MAX_LENGTH = 64
_INCIDENT_ACTION_KEYS = {"host", "condition_key", "action", "until", "reason"}
_INCIDENT_ACTION_V2_KEYS = _INCIDENT_ACTION_KEYS | {"incident_started_at"}
_INCIDENT_ACTIONS = frozenset({"acknowledged", "silenced"})
_INCIDENT_OVERRIDE_SCOPES = {"hosts", "groups"}
_INCIDENT_SCOPE_OVERRIDE_KEYS = {"thresholds", "exclude_disk_mounts"}
_TOPOLOGY_KEYS = {"root", "links"}
_TOPOLOGY_LINK_REQUIRED_KEYS = {"source", "target", "transport"}
_TOPOLOGY_LINK_KEYS = _TOPOLOGY_LINK_REQUIRED_KEYS | {"label"}
_PERSISTENCE_KEYS = {"enabled", "retention_hours", "max_bytes"}
_WORKLOAD_KEYS = {"mode"}
_WORKLOAD_MODES = frozenset({"disabled", "identity", "auto"})
_WEBHOOK_KEYS = {
    "name",
    "url_env",
    "secret_env",
    "events",
    "timeout_seconds",
    "max_attempts",
    "retry_base_seconds",
    "min_interval_seconds",
    "allow_private_networks",
}
_WEBHOOK_EVENT_STATES = frozenset({"opened", "resolved", "escalated", "deescalated"})
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_WEBHOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _bounded_number(
    data: dict[str, Any], key: str, minimum: float, maximum: float
) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{key} must be a number")
    # Compare before converting: float() overflows on huge JSON integers.
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return float(value)


def _bounded_integer(data: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _optional_bounded_number(
    data: dict[str, Any],
    key: str,
    label: str,
    minimum: float,
    maximum: float,
) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{label} must be a number")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{label} must be between {minimum} and {maximum}")
    return float(value)


def _string_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigError(f"{key} must be a list of non-empty strings")
    return tuple(dict.fromkeys(item.strip() for item in value))


def is_safe_alias(value: str) -> bool:
    return bool(_SAFE_ALIAS.fullmatch(value))


def normalize_web_hostname(value: object, *, allow_port: bool = False) -> str | None:
    """Return the canonical lowercase hostname for a Host-style value.

    Accepts DNS names, IPv4 literals, and IPv6 literals (bare or bracketed).
    A port suffix is accepted only when ``allow_port`` is true; schemes,
    credentials, paths, and queries are always rejected. Returns ``None``
    when the value cannot be interpreted as a plain hostname.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 260
        or any(
            unicodedata.category(character).startswith("C") for character in candidate
        )
    ):
        return None
    if candidate.count(":") >= 2 and not candidate.startswith("["):
        candidate = f"[{candidate}]"  # bare IPv6 literal
    try:
        parsed = urlsplit(f"//{candidate}")
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and not allow_port)
    ):
        return None
    if "[" in parsed.netloc:
        # Bracketed hosts must be IP literals even on Pythons whose urlsplit
        # does not validate them.
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return None
    else:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if len(hostname) > 253 or not hostname.isascii():
                return None
            labels = hostname.removesuffix(".").split(".")
            if any(
                not label
                or len(label) > 63
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?", label)
                for label in labels
            ):
                return None
    return hostname


def _has_disallowed_text_characters(value: str) -> bool:
    """Reject control/format characters plus line and paragraph separators."""
    for character in value:
        category = unicodedata.category(character)
        if category.startswith("C") or category in ("Zl", "Zp"):
            return True
    return False


def is_valid_maintenance_reason(value: object, *, required: bool) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return (
        (bool(normalized) or not required)
        and len(normalized) <= MAINTENANCE_REASON_MAX_LENGTH
        and not _has_disallowed_text_characters(value)
    )


def is_valid_incident_action_reason(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return len(value.strip()) <= INCIDENT_ACTION_REASON_MAX_LENGTH and not any(
        unicodedata.category(character).startswith("C") for character in value
    )


def is_valid_incident_condition_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= INCIDENT_ACTION_KEY_MAX_LENGTH
        and not any(
            unicodedata.category(character).startswith("C") for character in value
        )
    )


def _utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise ConfigError(f"{label} must be a UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ConfigError(f"{label} must be a valid UTC timestamp") from exc


def _incident_scope_override(raw: object, label: str) -> IncidentScopeOverrideConfig:
    if not isinstance(raw, dict) or not set(raw) <= _INCIDENT_SCOPE_OVERRIDE_KEYS:
        raise ConfigError(f"{label} has an invalid schema")
    if not raw:
        raise ConfigError(f"{label} must not be empty")
    raw_thresholds = raw.get("thresholds", {})
    if not isinstance(raw_thresholds, dict):
        raise ConfigError(f"{label}.thresholds must be a JSON object")
    unknown = sorted(raw_thresholds.keys() - _THRESHOLD_KEYS)
    if unknown:
        raise ConfigError(f"unknown {label}.thresholds keys: {', '.join(unknown)}")
    thresholds: list[tuple[str, float]] = []
    for name, value in raw_thresholds.items():
        maximum = 150 if name == "gpu_temperature_warning_c" else 100
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not 0 <= value <= maximum
        ):
            raise ConfigError(
                f"{label}.thresholds.{name} must be between 0 and {maximum}"
            )
        thresholds.append((name, float(value)))
    raw_mounts = raw.get("exclude_disk_mounts", [])
    if (
        not isinstance(raw_mounts, list)
        or len(raw_mounts) > 128
        or not all(
            isinstance(item, str)
            and item.startswith("/")
            and 0 < len(item) <= 512
            and not any(
                unicodedata.category(character).startswith("C") for character in item
            )
            for item in raw_mounts
        )
    ):
        raise ConfigError(
            f"{label}.exclude_disk_mounts must contain at most 128 absolute paths"
        )
    if not thresholds and not raw_mounts:
        raise ConfigError(f"{label} must configure a threshold or disk exclusion")
    return IncidentScopeOverrideConfig(
        thresholds=tuple(sorted(thresholds)),
        exclude_disk_mounts=frozenset(raw_mounts),
    )


def is_valid_host_group(value: object, *, required: bool) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return (
        (bool(normalized) or not required)
        and len(normalized) <= HOST_GROUP_MAX_LENGTH
        and not any(
            unicodedata.category(character).startswith("C") for character in value
        )
    )


def _connection_topology(raw: object) -> ConnectionTopologyConfig:
    if not isinstance(raw, dict) or set(raw) != _TOPOLOGY_KEYS:
        raise ConfigError("topology must contain exactly root and links")
    root_value = raw.get("root")
    if not isinstance(root_value, str) or not is_safe_alias(root_value):
        raise ConfigError("topology.root must be a safe host alias")
    root = root_value.strip()

    raw_links = raw.get("links")
    if not isinstance(raw_links, list):
        raise ConfigError("topology.links must be a list")
    if len(raw_links) > TOPOLOGY_MAX_LINKS:
        raise ConfigError(
            f"topology.links must contain at most {TOPOLOGY_MAX_LINKS} links"
        )

    links: list[TopologyLinkConfig] = []
    targets: set[str] = set()
    children: dict[str, list[str]] = {}
    for index, item in enumerate(raw_links):
        label = f"topology.links[{index}]"
        if not isinstance(item, dict) or not (
            _TOPOLOGY_LINK_REQUIRED_KEYS <= set(item) <= _TOPOLOGY_LINK_KEYS
        ):
            raise ConfigError(f"{label} has an invalid schema")
        source = item.get("source")
        target = item.get("target")
        transport = item.get("transport")
        if (
            not isinstance(source, str)
            or not is_safe_alias(source)
            or not isinstance(target, str)
            or not is_safe_alias(target)
        ):
            raise ConfigError(f"{label} endpoints must be safe host aliases")
        if source == target:
            raise ConfigError(f"{label} cannot link a host to itself")
        if target == root:
            raise ConfigError("topology.root cannot have an incoming link")
        if target in targets:
            raise ConfigError(f"topology target {target} has more than one parent")
        if not isinstance(transport, str) or transport not in TOPOLOGY_TRANSPORTS:
            raise ConfigError(f"{label}.transport is not supported")

        label_value = item.get("label")
        if label_value is None:
            normalized_label = None
        elif (
            not isinstance(label_value, str)
            or not label_value.strip()
            or len(label_value.strip()) > TOPOLOGY_LABEL_MAX_LENGTH
            or any(
                unicodedata.category(character).startswith("C")
                for character in label_value
            )
        ):
            raise ConfigError(
                f"{label}.label must contain at most "
                f"{TOPOLOGY_LABEL_MAX_LENGTH} visible characters"
            )
        else:
            normalized_label = label_value.strip()

        targets.add(target)
        children.setdefault(source, []).append(target)
        links.append(
            TopologyLinkConfig(
                source=source,
                target=target,
                transport=transport,
                label=normalized_label,
            )
        )

    reachable = {root}
    pending = [root]
    while pending:
        source = pending.pop()
        for target in children.get(source, ()):
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    endpoints = {endpoint for link in links for endpoint in (link.source, link.target)}
    if not endpoints <= reachable:
        raise ConfigError("topology links must form one tree reachable from root")
    return ConnectionTopologyConfig(root=root, links=tuple(links))


def _persistence_config(data: dict[str, Any]) -> PersistenceConfig:
    raw = data.get("persistence", {})
    if not isinstance(raw, dict):
        raise ConfigError("persistence must be a JSON object")
    unknown = sorted(raw.keys() - _PERSISTENCE_KEYS)
    if unknown:
        raise ConfigError(f"unknown persistence keys: {', '.join(unknown)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("persistence.enabled must be true or false")

    def integer(key: str, default: int, minimum: int, maximum: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"persistence.{key} must be an integer")
        if not minimum <= value <= maximum:
            raise ConfigError(
                f"persistence.{key} must be between {minimum} and {maximum}"
            )
        return value

    return PersistenceConfig(
        enabled=enabled,
        retention_hours=integer("retention_hours", 168, 1, 8760),
        max_bytes=integer("max_bytes", 134_217_728, 8_388_608, 1_073_741_824),
    )


def _workload_config(data: dict[str, Any]) -> WorkloadConfig:
    raw = data.get("workloads", {"mode": "disabled"})
    if not isinstance(raw, dict):
        raise ConfigError("workloads must be a JSON object")
    unknown = sorted(raw.keys() - _WORKLOAD_KEYS)
    if unknown:
        raise ConfigError(f"unknown workloads keys: {', '.join(unknown)}")
    mode = raw.get("mode")
    if mode not in _WORKLOAD_MODES:
        raise ConfigError("workloads.mode must be disabled, identity, or auto")
    return WorkloadConfig(mode=mode)


def _webhook_configs(data: dict[str, Any]) -> tuple[WebhookConfig, ...]:
    raw_items = data.get("webhooks", [])
    if not isinstance(raw_items, list) or len(raw_items) > 16:
        raise ConfigError("webhooks must be a list with at most 16 entries")
    webhooks: list[WebhookConfig] = []
    names: set[str] = set()

    def number(
        raw: dict[str, Any],
        label: str,
        key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{label}.{key} must be a number")
        if not minimum <= value <= maximum:
            raise ConfigError(f"{label}.{key} must be between {minimum} and {maximum}")
        return float(value)

    for index, raw in enumerate(raw_items):
        label = f"webhooks[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{label} must be a JSON object")
        unknown = sorted(raw.keys() - _WEBHOOK_KEYS)
        if unknown:
            raise ConfigError(f"unknown {label} keys: {', '.join(unknown)}")
        name = raw.get("name")
        url_env = raw.get("url_env")
        secret_env = raw.get("secret_env")
        if not isinstance(name, str) or not _WEBHOOK_NAME.fullmatch(name):
            raise ConfigError(f"{label}.name must be a safe identifier")
        if name in names:
            raise ConfigError(f"{label}.name must be unique")
        names.add(name)
        if not isinstance(url_env, str) or not _ENVIRONMENT_NAME.fullmatch(url_env):
            raise ConfigError(f"{label}.url_env must be an environment variable name")
        if secret_env is not None and (
            not isinstance(secret_env, str)
            or not _ENVIRONMENT_NAME.fullmatch(secret_env)
        ):
            raise ConfigError(
                f"{label}.secret_env must be null or an environment variable name"
            )
        events = raw.get("events", ["opened", "resolved", "escalated", "deescalated"])
        if (
            not isinstance(events, list)
            or not events
            or len(events) > len(_WEBHOOK_EVENT_STATES)
            or not all(isinstance(item, str) for item in events)
            or any(item not in _WEBHOOK_EVENT_STATES for item in events)
            or len(set(events)) != len(events)
        ):
            raise ConfigError(f"{label}.events contains invalid or duplicate states")

        max_attempts = raw.get("max_attempts", 3)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 8
        ):
            raise ConfigError(f"{label}.max_attempts must be between 1 and 8")
        allow_private = raw.get("allow_private_networks", False)
        if not isinstance(allow_private, bool):
            raise ConfigError(f"{label}.allow_private_networks must be true or false")
        webhooks.append(
            WebhookConfig(
                name=name,
                url_env=url_env,
                secret_env=secret_env,
                events=tuple(events),
                timeout_seconds=number(raw, label, "timeout_seconds", 5, 0.5, 30),
                max_attempts=max_attempts,
                retry_base_seconds=number(raw, label, "retry_base_seconds", 1, 0.1, 60),
                min_interval_seconds=number(
                    raw, label, "min_interval_seconds", 1, 0, 300
                ),
                allow_private_networks=allow_private,
            )
        )
    return tuple(webhooks)


def resolve_config_path(
    explicit: Path | str | None = None,
    *,
    environ: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve configuration without depending on the source checkout layout."""
    try:
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
    except (RuntimeError, UnicodeError, OSError) as exc:
        raise ConfigError("configuration path is invalid") from exc


class _JsonObjectPairs:
    """Raw key/value pairs of one JSON object, kept so duplicates survive."""

    __slots__ = ("pairs",)

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        self.pairs = pairs


def _resolve_json_objects(node: Any, path: str, source: Path) -> Any:
    """Convert parsed pairs into dicts, rejecting duplicate keys at any depth."""
    if isinstance(node, _JsonObjectPairs):
        result: dict[str, Any] = {}
        for key, value in node.pairs:
            child_path = f"{path}.{key}" if path else key
            if key in result:
                raise ConfigError(f"duplicate JSON key in {source}: {child_path}")
            result[key] = _resolve_json_objects(value, child_path, source)
        return result
    if isinstance(node, list):
        return [
            _resolve_json_objects(item, f"{path}[{index}]", source)
            for index, item in enumerate(node)
        ]
    return node


def _read_config_bytes(descriptor: int, config_path: Path) -> bytes:
    """Read one regular config file without allowing unbounded allocation."""
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigError(f"config file is not a regular file: {config_path}")
        if metadata.st_size > CONFIG_MAX_BYTES:
            raise ConfigError(
                f"config file exceeds the {CONFIG_MAX_BYTES}-byte limit: {config_path}"
            )
        content = bytearray()
        while len(content) <= CONFIG_MAX_BYTES:
            chunk = os.read(
                descriptor, min(65_536, CONFIG_MAX_BYTES + 1 - len(content))
            )
            if not chunk:
                return bytes(content)
            content.extend(chunk)
    except OSError as exc:
        raise ConfigError(f"cannot read config: {config_path}") from exc
    raise ConfigError(
        f"config file exceeds the {CONFIG_MAX_BYTES}-byte limit: {config_path}"
    )


def _read_config_path(config_path: Path) -> bytes:
    try:
        descriptor = os.open(config_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ConfigError(f"cannot read config: {config_path}") from exc
    try:
        return _read_config_bytes(descriptor, config_path)
    finally:
        os.close(descriptor)


def _validate_private_metadata(
    metadata: os.stat_result, path: Path, *, directory: bool
) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode) or metadata.st_uid != os.getuid():
        kind = "directory" if directory else "regular file"
        raise ConfigError(f"managed config {path} must be an owned {kind}")
    forbidden = 0o022 if directory else 0o077
    if metadata.st_mode & forbidden:
        requirement = "not be group/other-writable" if directory else "be private"
        raise ConfigError(f"managed config {path} must {requirement}")


def _read_private_config_path(config_path: Path) -> bytes:
    """Open a managed config through a trusted parent and parse that same file."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    try:
        parent_descriptor = os.open(config_path.parent, directory_flags)
    except OSError as exc:
        raise ConfigError(
            f"cannot open managed config directory: {config_path.parent}"
        ) from exc
    try:
        _validate_private_metadata(
            os.fstat(parent_descriptor), config_path.parent, directory=True
        )
        try:
            descriptor = os.open(
                config_path.name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ConfigError(f"cannot open managed config: {config_path}") from exc
        try:
            _validate_private_metadata(
                os.fstat(descriptor), config_path, directory=False
            )
            return _read_config_bytes(descriptor, config_path)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _parse_config_bytes(content: bytes, config_path: Path) -> MonitorConfig:
    try:
        raw = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"config file {config_path} is not valid UTF-8 "
            f"at byte {exc.start}: {exc.reason}"
        ) from exc

    try:
        parsed = json.loads(raw, object_pairs_hook=_JsonObjectPairs)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc.msg}") from exc
    except ValueError as exc:
        raise ConfigError(f"invalid JSON value in {config_path}") from exc
    except RecursionError as exc:
        raise ConfigError(f"JSON nesting is too deep in {config_path}") from exc
    try:
        data = _resolve_json_objects(parsed, "", config_path)
    except RecursionError as exc:
        raise ConfigError(f"JSON nesting is too deep in {config_path}") from exc

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
    if (
        not isinstance(data["listen_host"], str)
        or normalize_web_hostname(data["listen_host"]) is None
    ):
        raise ConfigError("listen_host must be a valid hostname or IP literal")

    hosts = _string_list(data, "hosts")
    excludes = frozenset(_string_list(data, "exclude_hosts"))
    if len(hosts) > CONFIG_MAX_HOST_ALIASES:
        raise ConfigError(
            f"hosts must contain at most {CONFIG_MAX_HOST_ALIASES} unique aliases"
        )
    if len(excludes) > CONFIG_MAX_HOST_ALIASES:
        raise ConfigError(
            "exclude_hosts must contain at most "
            f"{CONFIG_MAX_HOST_ALIASES} unique aliases"
        )
    invalid_aliases = sorted(
        alias for alias in (*hosts, *excludes) if not is_safe_alias(alias)
    )
    if invalid_aliases:
        raise ConfigError(
            "host aliases must contain only letters, numbers, dots, underscores, "
            f"and hyphens: {', '.join(invalid_aliases)}"
        )
    local_host_value = data.get("local_host")
    if local_host_value is None:
        local_host = None
    elif not isinstance(local_host_value, str) or not is_safe_alias(
        local_host_value.strip()
    ):
        raise ConfigError("local_host must be null or a safe host alias")
    else:
        local_host = local_host_value.strip()
        if local_host not in hosts:
            raise ConfigError("local_host must also appear in the explicit hosts list")
        if local_host in excludes:
            raise ConfigError("local_host cannot be excluded")
    topology = _connection_topology(data["topology"]) if "topology" in data else None
    poll_interval = _bounded_number(data, "poll_interval_seconds", 1, 3600)
    gpu_process_poll_interval = _bounded_number(
        {
            "gpu_process_poll_interval_seconds": data.get(
                "gpu_process_poll_interval_seconds", 15
            )
        },
        "gpu_process_poll_interval_seconds",
        2,
        3600,
    )
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
    raw_trusted_hosts = data.get("trusted_web_hosts", [])
    if (
        not isinstance(raw_trusted_hosts, list)
        or len(raw_trusted_hosts) > TRUSTED_WEB_HOSTS_MAX_ENTRIES
    ):
        raise ConfigError(
            "trusted_web_hosts must be a list with at most "
            f"{TRUSTED_WEB_HOSTS_MAX_ENTRIES} entries"
        )
    trusted_web_hosts: list[str] = []
    for item in raw_trusted_hosts:
        hostname = normalize_web_hostname(item)
        if hostname is None:
            raise ConfigError(
                "trusted_web_hosts entries must be hostnames or IP literals "
                "without scheme, port, credentials, or path"
            )
        trusted_web_hosts.append(hostname)
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
    retry_jitter_pct = _bounded_number(
        {"retry_jitter_pct": data.get("retry_jitter_pct", 15)},
        "retry_jitter_pct",
        0,
        50,
    )
    manual_probe_cooldown_seconds = _bounded_number(
        {"manual_probe_cooldown_seconds": data.get("manual_probe_cooldown_seconds", 5)},
        "manual_probe_cooldown_seconds",
        1,
        300,
    )

    persistence = _persistence_config(data)
    workloads = _workload_config(data)
    webhooks = _webhook_configs(data)

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
        if not 0 <= value <= maximum:
            raise ConfigError(f"thresholds.{name} must be between 0 and {maximum}")
        return float(value)

    thresholds = ThresholdConfig(
        cpu_warning_pct=threshold("cpu_warning_pct"),
        memory_warning_pct=threshold("memory_warning_pct"),
        swap_warning_pct=threshold("swap_warning_pct"),
        disk_warning_pct=threshold("disk_warning_pct"),
        disk_min_free_gib=threshold("disk_min_free_gib", 1_048_576),
        psi_memory_some_pct=threshold("psi_memory_some_pct"),
        psi_io_some_pct=threshold("psi_io_some_pct"),
        gpu_temperature_warning_c=threshold("gpu_temperature_warning_c", 150),
        gpu_busy_pct=threshold("gpu_busy_pct"),
        gpu_memory_warning_pct=threshold("gpu_memory_warning_pct"),
        gpu_idle_memory_pct=threshold("gpu_idle_memory_pct"),
    )

    expected_data = data.get("expected_gpu_counts", {})
    if not isinstance(expected_data, dict):
        raise ConfigError("expected_gpu_counts must be a JSON object")
    expected_gpu_counts: list[tuple[str, int]] = []
    for alias, count in expected_data.items():
        if not isinstance(alias, str) or not is_safe_alias(alias):
            raise ConfigError("expected_gpu_counts keys must be safe host aliases")
        if alias not in hosts:
            raise ConfigError(
                f"expected_gpu_counts.{alias} must reference an explicit host"
            )
        if alias in excludes:
            raise ConfigError(f"expected_gpu_counts.{alias} cannot be excluded")
        if isinstance(count, bool) or not isinstance(count, int):
            raise ConfigError(f"expected_gpu_counts.{alias} must be an integer")
        if not 0 <= count <= 256:
            raise ConfigError(f"expected_gpu_counts.{alias} must be between 0 and 256")
        expected_gpu_counts.append((alias, count))

    host_override_data = data.get("host_overrides", {})
    if not isinstance(host_override_data, dict):
        raise ConfigError("host_overrides must be a JSON object")
    host_overrides: list[tuple[str, HostOverrideConfig]] = []
    for alias, raw_override in host_override_data.items():
        if not isinstance(alias, str) or not is_safe_alias(alias):
            raise ConfigError("host_overrides keys must be safe host aliases")
        if alias not in hosts:
            raise ConfigError(f"host_overrides.{alias} must reference an explicit host")
        if alias in excludes:
            raise ConfigError(f"host_overrides.{alias} cannot be excluded")
        if not isinstance(raw_override, dict):
            raise ConfigError(f"host_overrides.{alias} must be a JSON object")
        unknown_override_keys = sorted(raw_override.keys() - _HOST_OVERRIDE_KEYS)
        if unknown_override_keys:
            raise ConfigError(
                f"unknown host_overrides.{alias} keys: "
                f"{', '.join(unknown_override_keys)}"
            )
        if not raw_override:
            raise ConfigError(f"host_overrides.{alias} must not be empty")

        display_name: str | None = None
        if "display_name" in raw_override:
            raw_display = raw_override["display_name"]
            if (
                not isinstance(raw_display, str)
                or not raw_display.strip()
                or len(raw_display.strip()) > DISPLAY_NAME_MAX_LENGTH
                or _has_disallowed_text_characters(raw_display)
            ):
                raise ConfigError(
                    f"host_overrides.{alias}.display_name must be 1 to "
                    f"{DISPLAY_NAME_MAX_LENGTH} visible characters"
                )
            display_name = raw_display.strip()
        override = HostOverrideConfig(
            poll_interval_seconds=_optional_bounded_number(
                raw_override,
                "poll_interval_seconds",
                f"host_overrides.{alias}.poll_interval_seconds",
                1,
                3600,
            ),
            probe_timeout_seconds=_optional_bounded_number(
                raw_override,
                "probe_timeout_seconds",
                f"host_overrides.{alias}.probe_timeout_seconds",
                2,
                300,
            ),
            display_name=display_name,
        )
        if (
            override.probe_timeout_seconds is not None
            and override.probe_timeout_seconds <= connect_timeout
        ):
            raise ConfigError(
                f"host_overrides.{alias}.probe_timeout_seconds must be greater "
                "than connect_timeout_seconds"
            )
        host_overrides.append((alias, override))

    maintenance_data = data.get("maintenance_windows", {})
    if not isinstance(maintenance_data, dict):
        raise ConfigError("maintenance_windows must be a JSON object")
    maintenance_windows: list[tuple[str, MaintenanceWindowConfig]] = []
    for alias, raw_window in maintenance_data.items():
        if not isinstance(alias, str) or not is_safe_alias(alias):
            raise ConfigError("maintenance_windows keys must be safe host aliases")
        if alias not in hosts:
            raise ConfigError(
                f"maintenance_windows.{alias} must reference an explicit host"
            )
        if alias in excludes:
            raise ConfigError(f"maintenance_windows.{alias} cannot be excluded")
        if not isinstance(raw_window, dict):
            raise ConfigError(f"maintenance_windows.{alias} must be a JSON object")
        unknown_window_keys = sorted(raw_window.keys() - _MAINTENANCE_WINDOW_KEYS)
        if unknown_window_keys:
            raise ConfigError(
                f"unknown maintenance_windows.{alias} keys: "
                f"{', '.join(unknown_window_keys)}"
            )
        reason_value = raw_window.get("reason", "")
        if not is_valid_maintenance_reason(reason_value, required=False):
            raise ConfigError(
                f"maintenance_windows.{alias}.reason must be at most "
                f"{MAINTENANCE_REASON_MAX_LENGTH} visible characters"
            )
        assert isinstance(reason_value, str)
        reason = reason_value.strip()
        has_until = "until" in raw_window
        has_recurrence = "recurrence" in raw_window
        if has_until == has_recurrence:
            raise ConfigError(
                f"maintenance_windows.{alias} must define exactly one of "
                "'until' or 'recurrence'"
            )
        if has_until:
            until = _utc_timestamp(
                raw_window.get("until"), f"maintenance_windows.{alias}.until"
            )
            maintenance_windows.append(
                (alias, MaintenanceWindowConfig(until=until, reason=reason))
            )
            continue
        recurrence = raw_window["recurrence"]
        label = f"maintenance_windows.{alias}.recurrence"
        if not isinstance(recurrence, dict):
            raise ConfigError(f"{label} must be a JSON object")
        unknown_recurrence = sorted(recurrence.keys() - _MAINTENANCE_RECURRENCE_KEYS)
        if unknown_recurrence:
            raise ConfigError(f"unknown {label} keys: {', '.join(unknown_recurrence)}")
        weekday = recurrence.get("weekday")
        if (
            not isinstance(weekday, int)
            or isinstance(weekday, bool)
            or not (0 <= weekday <= 6)
        ):
            raise ConfigError(f"{label}.weekday must be 0 (Monday) to 6 (Sunday)")
        start_value = recurrence.get("start")
        start_match = (
            _RECURRENCE_START.fullmatch(start_value)
            if isinstance(start_value, str)
            else None
        )
        if start_match is None:
            raise ConfigError(f"{label}.start must be 'HH:MM' in UTC")
        duration = recurrence.get("duration_minutes")
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or not (1 <= duration <= 10_079)
        ):
            raise ConfigError(
                f"{label}.duration_minutes must be 1 to 10079 (less than one week)"
            )
        maintenance_windows.append(
            (
                alias,
                MaintenanceWindowConfig(
                    reason=reason,
                    weekday=weekday,
                    start_minutes=(
                        int(start_match.group(1)) * 60 + int(start_match.group(2))
                    ),
                    duration_minutes=duration,
                ),
            )
        )

    host_group_data = data.get("host_groups", {})
    if not isinstance(host_group_data, dict):
        raise ConfigError("host_groups must be a JSON object")
    host_groups: list[tuple[str, str]] = []
    for alias, group_value in host_group_data.items():
        if not isinstance(alias, str) or not is_safe_alias(alias):
            raise ConfigError("host_groups keys must be safe host aliases")
        if alias not in hosts:
            raise ConfigError(f"host_groups.{alias} must reference an explicit host")
        if alias in excludes:
            raise ConfigError(f"host_groups.{alias} cannot be excluded")
        if not is_valid_host_group(group_value, required=True):
            raise ConfigError(
                f"host_groups.{alias} must be at most "
                f"{HOST_GROUP_MAX_LENGTH} visible characters"
            )
        assert isinstance(group_value, str)
        host_groups.append((alias, group_value.strip()))

    raw_incident_overrides = data.get("incident_overrides", {})
    if (
        not isinstance(raw_incident_overrides, dict)
        or not set(raw_incident_overrides) <= _INCIDENT_OVERRIDE_SCOPES
    ):
        raise ConfigError("incident_overrides must contain only hosts and groups")
    host_incident_overrides: list[tuple[str, IncidentScopeOverrideConfig]] = []
    raw_host_overrides = raw_incident_overrides.get("hosts", {})
    if not isinstance(raw_host_overrides, dict) or len(raw_host_overrides) > 256:
        raise ConfigError("incident_overrides.hosts must be a bounded JSON object")
    for alias, raw_override in raw_host_overrides.items():
        if not isinstance(alias, str) or not is_safe_alias(alias):
            raise ConfigError("incident_overrides.hosts keys must be safe aliases")
        if alias not in hosts or alias in excludes:
            raise ConfigError(
                f"incident_overrides.hosts.{alias} must reference an active host"
            )
        host_incident_overrides.append(
            (
                alias,
                _incident_scope_override(
                    raw_override, f"incident_overrides.hosts.{alias}"
                ),
            )
        )
    group_incident_overrides: list[tuple[str, IncidentScopeOverrideConfig]] = []
    raw_group_overrides = raw_incident_overrides.get("groups", {})
    if not isinstance(raw_group_overrides, dict) or len(raw_group_overrides) > 256:
        raise ConfigError("incident_overrides.groups must be a bounded JSON object")
    configured_groups = {group for _, group in host_groups}
    for group, raw_override in raw_group_overrides.items():
        if (
            not is_valid_host_group(group, required=True)
            or group not in configured_groups
        ):
            raise ConfigError(
                f"incident_overrides.groups.{group} must reference a configured group"
            )
        assert isinstance(group, str)
        group_incident_overrides.append(
            (
                group.strip(),
                _incident_scope_override(
                    raw_override, f"incident_overrides.groups.{group}"
                ),
            )
        )

    incident_data = data.get("incidents", {})
    if not isinstance(incident_data, dict):
        raise ConfigError("incidents must be a JSON object")
    unknown_incidents = sorted(incident_data.keys() - _INCIDENT_KEYS)
    if unknown_incidents:
        raise ConfigError(f"unknown incident keys: {', '.join(unknown_incidents)}")
    incident_defaults = IncidentConfig()

    def incident_cycles(name: str) -> int:
        value = incident_data.get(name, getattr(incident_defaults, name))
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"incidents.{name} must be an integer")
        if not 1 <= value <= 60:
            raise ConfigError(f"incidents.{name} must be between 1 and 60")
        return value

    incidents = IncidentConfig(
        resource_open_cycles=incident_cycles("resource_open_cycles"),
        recovery_cycles=incident_cycles("recovery_cycles"),
        gpu_idle_memory_cycles=incident_cycles("gpu_idle_memory_cycles"),
    )

    raw_actions = data.get("incident_actions", [])
    if (
        not isinstance(raw_actions, list)
        or len(raw_actions) > INCIDENT_ACTION_MAX_ENTRIES
    ):
        raise ConfigError(
            f"incident_actions must be a list with at most "
            f"{INCIDENT_ACTION_MAX_ENTRIES} entries"
        )
    incident_actions: list[IncidentActionConfig] = []
    action_keys: set[tuple[str, str]] = set()
    for index, raw_action in enumerate(raw_actions):
        label = f"incident_actions[{index}]"
        if not isinstance(raw_action, dict) or frozenset(raw_action) not in {
            frozenset(_INCIDENT_ACTION_KEYS),
            frozenset(_INCIDENT_ACTION_V2_KEYS),
        }:
            raise ConfigError(f"{label} has an invalid schema")
        host = raw_action.get("host")
        condition_key = raw_action.get("condition_key")
        action = raw_action.get("action")
        reason = raw_action.get("reason")
        incident_started_at = raw_action.get("incident_started_at")
        if not isinstance(host, str) or not is_safe_alias(host):
            raise ConfigError(f"{label}.host must be a safe host alias")
        if host not in hosts or host in excludes:
            raise ConfigError(f"{label}.host must reference an active explicit host")
        if not is_valid_incident_condition_key(condition_key):
            raise ConfigError(f"{label}.condition_key is invalid")
        if action not in _INCIDENT_ACTIONS:
            raise ConfigError(f"{label}.action must be acknowledged or silenced")
        if not is_valid_incident_action_reason(reason):
            raise ConfigError(
                f"{label}.reason must contain at most "
                f"{INCIDENT_ACTION_REASON_MAX_LENGTH} visible characters"
            )
        until = _utc_timestamp(raw_action.get("until"), f"{label}.until")
        if incident_started_at is not None:
            _utc_timestamp(incident_started_at, f"{label}.incident_started_at")
        assert isinstance(condition_key, str)
        assert isinstance(action, str)
        assert isinstance(reason, str)
        assert incident_started_at is None or isinstance(incident_started_at, str)
        key = (host, condition_key)
        if key in action_keys:
            raise ConfigError(f"{label} duplicates an incident action")
        action_keys.add(key)
        incident_actions.append(
            IncidentActionConfig(
                host=host,
                condition_key=condition_key,
                action=action,
                until=until,
                reason=reason.strip(),
                incident_started_at=incident_started_at,
            )
        )

    raw_ssh_config = data["ssh_config"]
    if any(
        unicodedata.category(character) in {"Cc", "Cs"} for character in raw_ssh_config
    ):
        raise ConfigError("ssh_config must not contain control or surrogate characters")
    try:
        ssh_config = Path(raw_ssh_config).expanduser()
        if not ssh_config.is_absolute():
            ssh_config = config_path.parent / ssh_config
        ssh_config = ssh_config.resolve()
    except (RuntimeError, UnicodeError, OSError) as exc:
        raise ConfigError("ssh_config is not a valid filesystem path") from exc

    return MonitorConfig(
        ssh_config=ssh_config,
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
        trusted_web_hosts=tuple(dict.fromkeys(trusted_web_hosts)),
        gpu_process_poll_interval_seconds=gpu_process_poll_interval,
        retry_jitter_pct=retry_jitter_pct,
        manual_probe_cooldown_seconds=manual_probe_cooldown_seconds,
        local_host=local_host,
        history_points=history_value,
        incident_history_points=incident_history_value,
        collection_stale_cycles=collection_stale_cycles,
        thresholds=thresholds,
        expected_gpu_counts=tuple(expected_gpu_counts),
        incidents=incidents,
        incident_actions=tuple(incident_actions),
        host_incident_overrides=tuple(host_incident_overrides),
        group_incident_overrides=tuple(group_incident_overrides),
        host_overrides=tuple(host_overrides),
        maintenance_windows=tuple(maintenance_windows),
        host_groups=tuple(host_groups),
        topology=topology,
        persistence=persistence,
        workloads=workloads,
        webhooks=webhooks,
    )


def load_config(path: Path | str | None = None) -> MonitorConfig:
    """Load a bounded configuration for an operator-controlled foreground run."""
    config_path = resolve_config_path(path)
    return _parse_config_bytes(_read_config_path(config_path), config_path)


def load_private_config(path: Path | str) -> MonitorConfig:
    """Load the private regular file required by a managed user service.

    The parent directory and file are opened without following their final
    symlink components. Validation and parsing use those same descriptors so
    an attacker cannot exchange the checked file before it is read.
    """
    try:
        config_path = Path(os.path.abspath(Path(path).expanduser()))
    except (RuntimeError, UnicodeError, OSError) as exc:
        raise ConfigError("managed config path is invalid") from exc
    return _parse_config_bytes(_read_private_config_path(config_path), config_path)
