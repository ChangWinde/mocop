from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from .discovery_policy import (
    SshDiscoveryConfig,
)
from .maintenance import MaintenanceWindowConfig
from .updates import UpdatesConfig

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

    code = "INVALID_CONFIG"


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
    # Confirmation in samples alone scales with the poll interval: two samples
    # are ten seconds at a five-second cadence, so a VRAM spike of that length
    # opens an incident that resolves twenty seconds later. The floor makes a
    # resource condition sustained by the clock as well.
    resource_open_seconds: float = 60
    # Idle VRAM is a claim about a workload, not a spike: checkpoint and
    # evaluation pauses idle a GPU for a minute or two, so the idle-memory
    # condition has its own, longer floor.
    gpu_idle_memory_seconds: float = 300


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
    ssh_discovery: SshDiscoveryConfig = field(default_factory=SshDiscoveryConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_host_override_index",
            MappingProxyType(dict(self.host_overrides)),
        )

    def host_override(self, host: str) -> HostOverrideConfig | None:
        return self._host_override_index.get(host)

    def host_display_names(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (alias, override.display_name)
            for alias, override in self.host_overrides
            if override.display_name is not None
        )


DISPLAY_NAME_MAX_LENGTH = 64


def is_safe_alias(value: str) -> bool:
    return bool(_SAFE_ALIAS.fullmatch(value))


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
