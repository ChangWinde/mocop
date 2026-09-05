from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .config import (
    BUNDLED_CONFIG_PATH,
    ConfigError,
    MonitorConfig,
    is_safe_alias,
    is_valid_host_group,
    is_valid_incident_action_reason,
    is_valid_incident_condition_key,
    is_valid_maintenance_reason,
    load_config,
)
from .discovery import (
    HostDiscoverySnapshot,
    HostSource,
    is_code_host_alias,
    resolve_host_discovery,
)
from .models import utc_after
from .privatefiles import (
    PRIVATE_FILE_MODE,
    acquire_private_lock,
    release_private_lock,
)

_MAX_CONFIG_BYTES = 1_048_576
DASHBOARD_MAINTENANCE_DURATIONS = frozenset({0, 3_600, 14_400, 86_400, 604_800})
DASHBOARD_INCIDENT_ACTION_DURATIONS = frozenset({0, 3_600, 14_400, 86_400, 604_800})


class InventoryError(RuntimeError):
    """Raised when the constrained dashboard inventory cannot be read or changed."""


class InventoryRequestError(InventoryError):
    """Raised when a requested inventory transition is not currently valid."""


class DashboardConfigController(Protocol):
    def snapshot(self) -> dict[str, object]: ...

    def topology(self) -> dict[str, object]: ...

    def writable(self) -> bool: ...

    def change(self, action: str, host: str) -> dict[str, object]: ...

    def update_collector_settings(
        self, settings: dict[str, object]
    ) -> dict[str, object]: ...

    def update_maintenance(
        self, host: str, duration_seconds: int, reason: str
    ) -> dict[str, object]: ...

    def update_host_group(self, host: str, group: str) -> dict[str, object]: ...

    def update_incident_action(
        self,
        host: str,
        condition_key: str,
        action: str,
        duration_seconds: int,
        reason: str,
        incident_started_at: str | None = None,
    ) -> dict[str, object]: ...


class ConfigInventory:
    """Manage the dashboard's bounded projection of the operator JSON config."""

    def __init__(
        self,
        config_path: Path,
        host_source: HostSource,
        on_config_changed: Callable[[MonitorConfig], None],
    ) -> None:
        self._config_path = Path(os.path.abspath(config_path.expanduser()))
        self._host_source = host_source
        self._on_config_changed = on_config_changed
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            config = self._load()
            return self._snapshot(config)

    def topology(self) -> dict[str, object]:
        """Return configured or cached resolved display metadata."""
        with self._lock:
            config = self._load()
            topology = (
                config.topology
                if config.topology is not None
                else (
                    resolve_host_discovery(self._host_source, config).topology
                    if config.ssh_discovery.mode == "topology"
                    else None
                )
            )
            return (
                topology.to_dict()
                if topology is not None
                else {
                    "root": None,
                    "links": [],
                }
            )

    def writable(self) -> bool:
        """Report dashboard writability from file metadata alone (no SSH scan)."""
        return self._is_writable_target()

    def change(self, action: str, host: str) -> dict[str, object]:
        if action not in {"add", "remove"}:
            raise InventoryRequestError("inventory action must be add or remove")
        if not isinstance(host, str) or not is_safe_alias(host):
            raise InventoryRequestError("host must be a safe OpenSSH alias")

        with self._mutation_lock():
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
                actions = data.get("incident_actions")
                if isinstance(actions, list):
                    data["incident_actions"] = [
                        item
                        for item in actions
                        if not isinstance(item, dict) or item.get("host") != host
                    ]
                self._remove_topology_host(data, host)

            data["hosts"] = configured
            self._prune_incident_overrides(data)
            _updated, response = self._commit(data, prepare=self._snapshot)
            assert response is not None
            return response

    @staticmethod
    def _remove_topology_host(data: dict[str, object], host: str) -> None:
        topology = data.get("topology")
        if not isinstance(topology, dict):
            return
        if topology.get("root") == host:
            data.pop("topology", None)
            return
        links = topology.get("links")
        if not isinstance(links, list):
            return
        topology["links"] = [
            link
            for link in links
            if isinstance(link, dict)
            and link.get("source") != host
            and link.get("target") != host
        ]
        root = topology.get("root")
        if not isinstance(root, str):
            data.pop("topology", None)
            return
        reachable = {root}
        pending = list(topology["links"])
        retained: list[dict[str, object]] = []
        while pending:
            progressed = False
            remainder = []
            for link in pending:
                source = link.get("source")
                target = link.get("target")
                if source in reachable and isinstance(target, str):
                    reachable.add(target)
                    retained.append(link)
                    progressed = True
                else:
                    remainder.append(link)
            if not progressed:
                break
            pending = remainder
        topology["links"] = retained

    @staticmethod
    def _prune_incident_overrides(data: dict[str, object]) -> None:
        """Remove scoped overrides whose host or group no longer exists."""
        overrides = data.get("incident_overrides")
        if not isinstance(overrides, dict):
            return
        configured_hosts = {
            host for host in data.get("hosts", ()) if isinstance(host, str)
        }
        excluded_hosts = {
            host for host in data.get("exclude_hosts", ()) if isinstance(host, str)
        }
        active_hosts = configured_hosts - excluded_hosts
        host_overrides = overrides.get("hosts", {})
        group_overrides = overrides.get("groups", {})
        groups = data.get("host_groups", {})
        configured_groups = (
            {
                group
                for host, group in groups.items()
                if host in active_hosts and isinstance(group, str)
            }
            if isinstance(groups, dict)
            else set()
        )
        if isinstance(host_overrides, dict):
            overrides["hosts"] = {
                host: value
                for host, value in host_overrides.items()
                if host in active_hosts
            }
        if isinstance(group_overrides, dict):
            overrides["groups"] = {
                group: value
                for group, value in group_overrides.items()
                if group in configured_groups
            }

    def update_host_group(self, host: str, group: str) -> dict[str, object]:
        """Persist or clear one explicitly configured host's shared group."""
        if not isinstance(host, str) or not is_safe_alias(host):
            raise InventoryRequestError("host must be a safe OpenSSH alias")
        if not is_valid_host_group(group, required=False):
            raise InventoryRequestError("host group is invalid")
        assert isinstance(group, str)
        normalized_group = group.strip()

        with self._mutation_lock():
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
            self._prune_incident_overrides(data)
            _updated, response = self._commit(data, prepare=self._snapshot)
            assert response is not None
            return response

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

        with self._mutation_lock():
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
            _updated, response = self._commit(data, prepare=self._snapshot)
            assert response is not None
            return response

    def update_incident_action(
        self,
        host: str,
        condition_key: str,
        action: str,
        duration_seconds: int,
        reason: str,
        incident_started_at: str | None = None,
    ) -> dict[str, object]:
        """Persist one bounded condition-level acknowledgement or silence."""
        if not isinstance(host, str) or not is_safe_alias(host):
            raise InventoryRequestError("incident host must be a safe alias")
        if not is_valid_incident_condition_key(condition_key):
            raise InventoryRequestError("incident condition key is invalid")
        if action not in {"acknowledged", "silenced", "clear"}:
            raise InventoryRequestError("incident action is invalid")
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or duration_seconds not in DASHBOARD_INCIDENT_ACTION_DURATIONS
            or (action == "clear") != (duration_seconds == 0)
        ):
            raise InventoryRequestError("incident action duration is invalid")
        if not is_valid_incident_action_reason(reason):
            raise InventoryRequestError("incident action reason is invalid")
        if action != "clear" and not isinstance(incident_started_at, str):
            raise InventoryRequestError("incident instance is required")

        with self._mutation_lock():
            self._require_writable()
            config = self._load()
            if host not in config.hosts or host in config.exclude_hosts:
                raise InventoryRequestError(
                    "incident host is not explicitly configured"
                )
            data = self._read_object()
            raw_actions = data.get("incident_actions", [])
            if not isinstance(raw_actions, list):
                raise InventoryError("incident action configuration is invalid")
            now = utc_after(0)
            actions = [
                item
                for item in raw_actions
                if isinstance(item, dict)
                and isinstance(item.get("until"), str)
                and item["until"] > now
                and (item.get("host"), item.get("condition_key"))
                != (host, condition_key)
            ]
            if action != "clear":
                actions.append(
                    {
                        "host": host,
                        "condition_key": condition_key,
                        "action": action,
                        "until": utc_after(duration_seconds),
                        "reason": reason.strip(),
                        "incident_started_at": incident_started_at,
                    }
                )
            data["incident_actions"] = actions
            _updated, response = self._commit(data, prepare=self._snapshot)
            assert response is not None
            return response

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

        with self._mutation_lock():
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
            updated, _response = self._commit(data)
            return self._collector_settings(updated)

    @contextmanager
    def _mutation_lock(self):
        """Serialize config transactions across threads and Mocop processes."""
        with self._lock:
            lock_path = self._config_path.with_name(f".{self._config_path.name}.lock")
            try:
                descriptor = acquire_private_lock(lock_path)
            except OSError as exc:
                raise InventoryError("configuration lock is unavailable") from exc
            try:
                yield
            finally:
                release_private_lock(descriptor)

    def _load(self) -> MonitorConfig:
        try:
            return load_config(self._config_path)
        except ConfigError as exc:
            raise InventoryError(
                "cluster configuration is unavailable or invalid"
            ) from exc

    def _discover(self, config: MonitorConfig) -> HostDiscoverySnapshot:
        try:
            return resolve_host_discovery(self._host_source, config)
        except (OSError, ValueError) as exc:
            raise InventoryError("OpenSSH aliases could not be scanned") from exc

    def _eligible_aliases(self, config: MonitorConfig) -> tuple[str, ...]:
        return self._discover(config).eligible_aliases

    def _snapshot(self, config: MonitorConfig) -> dict[str, object]:
        discovery = self._discover(config)
        scanned = discovery.aliases
        eligible = discovery.eligible_aliases
        active = discovery.hosts
        active_set = set(active)
        # One clock sample keeps the active-window decision and the serialized
        # instance end consistent when a recurring boundary is being crossed.
        now = datetime.now(timezone.utc)
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
            # Every configured window stays visible; "active" tells the
            # dashboard whether it is currently silencing the host.
            "maintenanceWindows": {
                alias: {**window.to_dict(now), "active": window.is_active(now)}
                for alias, window in config.maintenance_windows
            },
            "incidentActions": [
                action.to_dict()
                for action in config.incident_actions
                if action.is_active(now)
            ],
            "hostGroups": {
                host: group
                for host, group in discovery.host_groups
                if host in config.hosts
            },
            "sshDiscoveryMode": discovery.mode,
            "infrastructureHosts": list(discovery.infrastructure_hosts),
            "sshDiscoveryWarnings": list(discovery.warnings),
            "writable": self._is_writable_target(),
        }

    @staticmethod
    def _collector_settings(config: MonitorConfig) -> dict[str, object]:
        # connectTimeoutSeconds is read-only context for the dashboard (the
        # probe timeout must exceed it); the write path keeps rejecting it.
        return {
            "pollIntervalSeconds": config.poll_interval_seconds,
            "probeTimeoutSeconds": config.probe_timeout_seconds,
            "connectTimeoutSeconds": config.connect_timeout_seconds,
            "maxWorkers": config.max_workers,
        }

    def _read_object(self) -> dict[str, object]:
        descriptor = -1
        try:
            descriptor = os.open(self._config_path, os.O_RDONLY | os.O_NOFOLLOW)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise InventoryError("cluster configuration is not a regular file")
            if metadata.st_size > _MAX_CONFIG_BYTES:
                raise InventoryError("cluster configuration is too large")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(_MAX_CONFIG_BYTES + 1)
            if len(content) > _MAX_CONFIG_BYTES:
                raise InventoryError("cluster configuration is too large")
            data = json.loads(content.decode("utf-8"))
        except InventoryError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise InventoryError("cluster configuration could not be read") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(data, dict):
            raise InventoryError("cluster configuration must be a JSON object")
        return data

    def _is_writable_target(self) -> bool:
        if self._config_path == BUNDLED_CONFIG_PATH.resolve():
            return False
        try:
            metadata = self._config_path.lstat()
        except OSError:
            return False
        # lstat metadata already reports a symlink as non-regular.
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and os.access(self._config_path, os.W_OK)
            and os.access(self._config_path.parent, os.W_OK)
        )

    def _require_writable(self) -> None:
        if not self._is_writable_target():
            raise InventoryError("cluster configuration is not dashboard-writable")

    def _commit(
        self,
        data: dict[str, object],
        *,
        prepare: Callable[[MonitorConfig], dict[str, object]] | None = None,
    ) -> tuple[MonitorConfig, dict[str, object] | None]:
        updated, response = self._atomic_replace(data, prepare=prepare)
        try:
            self._on_config_changed(updated)
        except Exception as exc:
            raise InventoryError(
                "configuration was saved but runtime synchronization failed"
            ) from exc
        return updated, response

    def _atomic_replace(
        self,
        data: dict[str, object],
        *,
        prepare: Callable[[MonitorConfig], dict[str, object]] | None = None,
    ) -> tuple[MonitorConfig, dict[str, object] | None]:
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
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            updated = load_config(temporary_path)
            response = prepare(updated) if prepare is not None else None
            # The private mode travels with the inode through the rename.
            os.replace(temporary_path, self._config_path)
            temporary_path = None
            directory_descriptor = os.open(self._config_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return updated, response
        except (OSError, ConfigError) as exc:
            raise InventoryError("cluster configuration could not be updated") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
