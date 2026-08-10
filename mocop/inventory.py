from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .config import (
    BUNDLED_CONFIG_PATH,
    ConfigError,
    MonitorConfig,
    is_safe_alias,
    is_valid_host_group,
    is_valid_maintenance_reason,
    load_config,
)
from .discovery import HostSource, is_code_host_alias
from .models import utc_after

_MAX_CONFIG_BYTES = 1_048_576
DASHBOARD_MAINTENANCE_DURATIONS = frozenset({0, 3_600, 14_400, 86_400, 604_800})


class InventoryError(RuntimeError):
    """Raised when the constrained dashboard inventory cannot be read or changed."""


class InventoryRequestError(InventoryError):
    """Raised when a requested inventory transition is not currently valid."""


class DashboardConfigController(Protocol):
    def snapshot(self) -> dict[str, object]: ...

    def change(self, action: str, host: str) -> dict[str, object]: ...

    def update_collector_settings(
        self, settings: dict[str, object]
    ) -> dict[str, object]: ...

    def update_maintenance(
        self, host: str, duration_seconds: int, reason: str
    ) -> dict[str, object]: ...

    def update_host_group(self, host: str, group: str) -> dict[str, object]: ...


class ConfigInventory:
    """Manage the dashboard's bounded projection of the operator JSON config."""

    def __init__(
        self,
        config_path: Path,
        host_source: HostSource,
        on_config_changed: Callable[[MonitorConfig], None],
    ) -> None:
        self._config_path = config_path.expanduser().resolve()
        self._host_source = host_source
        self._on_config_changed = on_config_changed
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            config = self._load()
            return self._snapshot(config)

    def change(self, action: str, host: str) -> dict[str, object]:
        if action not in {"add", "remove"}:
            raise InventoryRequestError("inventory action must be add or remove")
        if not isinstance(host, str) or not is_safe_alias(host):
            raise InventoryRequestError("host must be a safe OpenSSH alias")

        with self._lock:
            self._require_writable()
            config = self._load()
            data = self._read_object()
            configured = list(config.hosts)

            if action == "add":
                eligible = set(self._eligible_aliases(config))
                if host in configured:
                    raise InventoryRequestError("host is already configured")
                if host not in eligible:
                    raise InventoryRequestError(
                        "host is not an eligible discovered alias"
                    )
                configured.append(host)
            else:
                try:
                    active = set(self._host_source.hosts(config))
                except (OSError, ValueError) as exc:
                    raise InventoryError(
                        "active inventory could not be resolved"
                    ) from exc
                if host not in active:
                    raise InventoryRequestError("host is not in the active inventory")
                if host in configured:
                    configured.remove(host)
                if config.auto_discover:
                    data["exclude_hosts"] = list(
                        dict.fromkeys([*data["exclude_hosts"], host])
                    )
                if data.get("local_host") == host:
                    data["local_host"] = None
                for field in (
                    "expected_gpu_counts",
                    "host_overrides",
                    "maintenance_windows",
                    "host_groups",
                ):
                    metadata = data.get(field)
                    if isinstance(metadata, dict):
                        metadata.pop(host, None)

            data["hosts"] = configured
            updated = self._commit(data)
            return self._snapshot(updated)

    def update_host_group(self, host: str, group: str) -> dict[str, object]:
        """Persist or clear one explicitly configured host's shared group."""
        if not isinstance(host, str) or not is_safe_alias(host):
            raise InventoryRequestError("host must be a safe OpenSSH alias")
        if not is_valid_host_group(group, required=False):
            raise InventoryRequestError("host group is invalid")
        assert isinstance(group, str)
        normalized_group = group.strip()

        with self._lock:
            self._require_writable()
            config = self._load()
            if host not in config.hosts or host in config.exclude_hosts:
                raise InventoryRequestError("group host is not explicitly configured")
            current_groups = dict(config.host_groups)
            if current_groups.get(host, "") == normalized_group:
                return self._snapshot(config)
            data = self._read_object()
            raw_groups = data.get("host_groups", {})
            if not isinstance(raw_groups, dict):
                raise InventoryError("host group configuration is invalid")
            groups = dict(raw_groups)
            if normalized_group:
                groups[host] = normalized_group
            else:
                groups.pop(host, None)
            data["host_groups"] = groups
            updated = self._commit(data)
            return self._snapshot(updated)

    def update_maintenance(
        self, host: str, duration_seconds: int, reason: str
    ) -> dict[str, object]:
        """Persist or clear one explicitly configured maintenance window."""
        if not isinstance(host, str) or not is_safe_alias(host):
            raise InventoryRequestError("host must be a safe OpenSSH alias")
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or duration_seconds not in DASHBOARD_MAINTENANCE_DURATIONS
        ):
            raise InventoryRequestError("maintenance duration is not allowed")
        if not is_valid_maintenance_reason(reason, required=duration_seconds != 0):
            raise InventoryRequestError("maintenance reason is invalid")
        assert isinstance(reason, str)
        normalized_reason = reason.strip()

        with self._lock:
            self._require_writable()
            config = self._load()
            if host not in config.hosts or host in config.exclude_hosts:
                raise InventoryRequestError(
                    "maintenance host is not explicitly configured"
                )
            data = self._read_object()
            raw_windows = data.get("maintenance_windows", {})
            if not isinstance(raw_windows, dict):
                raise InventoryError("maintenance configuration is invalid")
            windows = dict(raw_windows)
            if duration_seconds == 0:
                if host not in windows:
                    return self._snapshot(config)
                windows.pop(host, None)
            else:
                windows[host] = {
                    "until": utc_after(duration_seconds),
                    "reason": normalized_reason,
                }
            data["maintenance_windows"] = windows
            updated = self._commit(data)
            return self._snapshot(updated)

    def update_collector_settings(
        self, settings: dict[str, object]
    ) -> dict[str, object]:
        """Persist the dashboard's narrow collection-policy projection."""
        fields = {
            "pollIntervalSeconds": "poll_interval_seconds",
            "probeTimeoutSeconds": "probe_timeout_seconds",
            "maxWorkers": "max_workers",
        }
        if not settings or set(settings) - fields.keys():
            raise InventoryRequestError("invalid collector settings schema")

        with self._lock:
            self._require_writable()
            config = self._load()
            normalized: dict[str, float | int] = {}
            for key, value in settings.items():
                if key == "maxWorkers":
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise InventoryRequestError("maxWorkers must be an integer")
                    if not 1 <= value <= 64:
                        raise InventoryRequestError("maxWorkers is outside safe bounds")
                    normalized[key] = value
                    continue
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise InventoryRequestError(f"{key} must be a finite number")
                number = float(value)
                if not math.isfinite(number):
                    raise InventoryRequestError(f"{key} must be a finite number")
                if key == "pollIntervalSeconds" and not 2 <= number <= 60:
                    raise InventoryRequestError(
                        "pollIntervalSeconds is outside dashboard bounds"
                    )
                if key == "probeTimeoutSeconds" and not 2 <= number <= 300:
                    raise InventoryRequestError(
                        "probeTimeoutSeconds is outside safe bounds"
                    )
                if (
                    key == "probeTimeoutSeconds"
                    and number <= config.connect_timeout_seconds
                ):
                    raise InventoryRequestError(
                        "probeTimeoutSeconds must exceed the SSH connect timeout"
                    )
                normalized[key] = number

            current = self._collector_settings(config)
            if all(current[key] == value for key, value in normalized.items()):
                return current

            data = self._read_object()
            for key, value in normalized.items():
                data[fields[key]] = value
            updated = self._commit(data)
            return self._collector_settings(updated)

    def _load(self) -> MonitorConfig:
        try:
            return load_config(self._config_path)
        except ConfigError as exc:
            raise InventoryError(
                "cluster configuration is unavailable or invalid"
            ) from exc

    def _scan(self, config: MonitorConfig) -> tuple[str, ...]:
        try:
            return self._host_source.aliases(config)
        except (OSError, ValueError) as exc:
            raise InventoryError("OpenSSH aliases could not be scanned") from exc

    def _eligible_aliases(self, config: MonitorConfig) -> tuple[str, ...]:
        return tuple(
            alias
            for alias in self._scan(config)
            if alias not in config.exclude_hosts and not is_code_host_alias(alias)
        )

    def _snapshot(self, config: MonitorConfig) -> dict[str, object]:
        scanned = self._scan(config)
        eligible = tuple(
            alias
            for alias in scanned
            if alias not in config.exclude_hosts and not is_code_host_alias(alias)
        )
        try:
            active = self._host_source.hosts(config)
        except (OSError, ValueError) as exc:
            raise InventoryError("active inventory could not be resolved") from exc
        active_set = set(active)
        return {
            "configuredHosts": list(config.hosts),
            "activeHosts": list(active),
            "availableHosts": [alias for alias in eligible if alias not in active_set],
            "localHost": config.local_host,
            "autoDiscover": config.auto_discover,
            "ignoredCodeHostCount": sum(is_code_host_alias(alias) for alias in scanned),
            "excludedHostCount": sum(
                alias in config.exclude_hosts and not is_code_host_alias(alias)
                for alias in scanned
            ),
            "collectorSettings": self._collector_settings(config),
            "maintenanceWindows": {
                alias: window.to_dict()
                for alias, window in config.maintenance_windows
                if window.is_active()
            },
            "hostGroups": dict(config.host_groups),
            "writable": self._is_writable_target(),
        }

    @staticmethod
    def _collector_settings(config: MonitorConfig) -> dict[str, object]:
        return {
            "pollIntervalSeconds": config.poll_interval_seconds,
            "probeTimeoutSeconds": config.probe_timeout_seconds,
            "maxWorkers": config.max_workers,
        }

    def _read_object(self) -> dict[str, object]:
        try:
            if self._config_path.stat().st_size > _MAX_CONFIG_BYTES:
                raise InventoryError("cluster configuration is too large")
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
        except InventoryError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InventoryError("cluster configuration could not be read") from exc
        if not isinstance(data, dict):
            raise InventoryError("cluster configuration must be a JSON object")
        return data

    def _is_writable_target(self) -> bool:
        if self._config_path == BUNDLED_CONFIG_PATH.resolve():
            return False
        try:
            metadata = self._config_path.stat()
        except OSError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and os.access(self._config_path, os.W_OK)
            and os.access(self._config_path.parent, os.W_OK)
        )

    def _require_writable(self) -> None:
        if not self._is_writable_target():
            raise InventoryError("cluster configuration is not dashboard-writable")

    def _commit(self, data: dict[str, object]) -> MonitorConfig:
        updated = self._atomic_replace(data)
        try:
            self._on_config_changed(updated)
        except Exception as exc:
            raise InventoryError(
                "configuration was saved but runtime synchronization failed"
            ) from exc
        return updated

    def _atomic_replace(self, data: dict[str, object]) -> MonitorConfig:
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self._config_path.name}.",
                suffix=".tmp",
                dir=self._config_path.parent,
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            updated = load_config(temporary_path)
            os.replace(temporary_path, self._config_path)
            temporary_path = None
            self._config_path.chmod(0o600)
            directory_descriptor = os.open(self._config_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return updated
        except (OSError, ConfigError) as exc:
            raise InventoryError("cluster configuration could not be updated") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
