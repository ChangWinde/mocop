from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import (
    ConfigError,
    MonitorConfig,
    is_safe_alias,
    load_private_config,
    load_private_config_document,
)
from .lifecycle import LifecycleError
from .privatefiles import PRIVATE_FILE_MODE


@dataclass(frozen=True, slots=True)
class MigrationResult:
    source: Path
    target: Path
    old_local_host: str | None
    new_local_host: str | None
    auto_discover: bool
    dropped_fields: tuple[str, ...]


def _absolute(path: Path) -> Path:
    try:
        return Path(os.path.abspath(path.expanduser()))
    except (RuntimeError, UnicodeError, OSError) as exc:
        raise LifecycleError("migration path is invalid") from exc


def _private_target_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise LifecycleError("migration target directory cannot be prepared") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise LifecycleError(
            "migration target directory must be owner-controlled and not writable "
            "by others"
        )


def _source_data(path: Path) -> tuple[dict[str, object], MonitorConfig]:
    try:
        raw, config = load_private_config_document(path)
    except (ConfigError, OSError, UnicodeError) as exc:
        raise LifecycleError(
            "source configuration must be a private valid config file"
        ) from exc
    return raw, config


def _pop_alias(
    data: dict[str, object], field: str, alias: str, dropped: list[str]
) -> object | None:
    mapping = data.get(field)
    if not isinstance(mapping, dict) or alias not in mapping:
        return None
    value = mapping.pop(alias)
    dropped.append(f"{field}.{alias}")
    return value


def _replace_topology_alias(
    topology: dict[str, object], old_alias: str, new_alias: str
) -> None:
    if topology.get("root") == old_alias:
        topology["root"] = new_alias
    links = topology.get("links")
    if not isinstance(links, list):
        return
    for link in links:
        if not isinstance(link, dict):
            continue
        if link.get("source") == old_alias:
            link["source"] = new_alias
        if link.get("target") == old_alias:
            link["target"] = new_alias


def _topology_contains(topology: object, alias: str) -> bool:
    if not isinstance(topology, dict):
        return False
    if topology.get("root") == alias:
        return True
    links = topology.get("links")
    return isinstance(links, list) and any(
        isinstance(link, dict)
        and (link.get("source") == alias or link.get("target") == alias)
        for link in links
    )


def _write_private_target(path: Path, data: dict[str, object]) -> None:
    if os.path.lexists(path):
        raise LifecycleError(f"migration target already exists: {path}")
    token_path = path.with_name("access-token")
    if os.path.lexists(token_path):
        raise LifecycleError(
            "migration target contains access-token; use a clean directory so the "
            "new installation receives a fresh capability"
        )
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise LifecycleError(f"migration target already exists: {path}") from exc
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise LifecycleError("cannot write migrated configuration") from exc
    try:
        load_private_config(path)
    except (ConfigError, OSError, UnicodeError) as exc:
        path.unlink(missing_ok=True)
        raise LifecycleError(f"migrated configuration is invalid: {exc}") from exc


def migrate_config(
    source: Path,
    target: Path,
    *,
    current_hostname: str,
    local_host: str | None = None,
    drop_local_host: bool = False,
    display_name: str | None = None,
    ssh_config: str = "~/.ssh/config",
    auto_discover: bool | None = None,
) -> MigrationResult:
    """Generate a current private config without mutating the source installation."""
    source_path = _absolute(source)
    target_path = _absolute(target)
    if drop_local_host and local_host is not None:
        raise LifecycleError(
            "--local-host and --drop-local-host are mutually exclusive"
        )
    if auto_discover is not None and not isinstance(auto_discover, bool):
        raise LifecycleError("auto-discover migration policy must be boolean")

    raw, source_config = _source_data(source_path)
    old_local_host = source_config.local_host
    if drop_local_host:
        new_local_host = None
    elif local_host is not None:
        new_local_host = local_host.strip()
    elif old_local_host is not None:
        new_local_host = current_hostname.strip()
    else:
        new_local_host = None
    if new_local_host is not None and not is_safe_alias(new_local_host):
        raise LifecycleError(f"invalid local host alias: {new_local_host}")
    if display_name is not None and new_local_host is None:
        raise LifecycleError("display name requires a migrated local host")

    data = json.loads(json.dumps(raw, ensure_ascii=False))
    hosts = list(source_config.hosts)
    excludes = set(source_config.exclude_hosts)
    if (
        new_local_host is not None
        and new_local_host != old_local_host
        and new_local_host in hosts
    ):
        raise LifecycleError(
            f"new local host alias already identifies another target: {new_local_host}"
        )
    if new_local_host is not None and new_local_host in excludes:
        raise LifecycleError(f"new local host alias is excluded: {new_local_host}")
    if (
        old_local_host is not None
        and new_local_host is not None
        and new_local_host != old_local_host
        and _topology_contains(raw.get("topology"), new_local_host)
    ):
        # Only a rename can fold two topology nodes into one and self-link.
        # Guard exactly that: when the source has no local_host there is no
        # rename, so deploying onto a machine that is already a topology node
        # (a jump host, for example) stays a valid migration.
        raise LifecycleError(
            "new local host alias already appears in the configured topology: "
            f"{new_local_host}"
        )

    dropped: list[str] = []
    if old_local_host is not None:
        replacement = [
            new_local_host if host == old_local_host else host
            for host in hosts
            if host != old_local_host or new_local_host is not None
        ]
        hosts = list(dict.fromkeys(replacement))
        groups = data.get("host_groups")
        old_group = (
            groups.pop(old_local_host, None) if isinstance(groups, dict) else None
        )
        _pop_alias(data, "expected_gpu_counts", old_local_host, dropped)
        _pop_alias(data, "host_overrides", old_local_host, dropped)
        _pop_alias(data, "maintenance_windows", old_local_host, dropped)
        incident_overrides = data.get("incident_overrides")
        if isinstance(incident_overrides, dict):
            host_overrides = incident_overrides.get("hosts")
            if isinstance(host_overrides, dict) and old_local_host in host_overrides:
                host_overrides.pop(old_local_host)
                dropped.append(f"incident_overrides.hosts.{old_local_host}")
        actions = data.get("incident_actions")
        if isinstance(actions, list):
            retained = [
                action
                for action in actions
                if not isinstance(action, dict) or action.get("host") != old_local_host
            ]
            if len(retained) != len(actions):
                data["incident_actions"] = retained
                dropped.append(f"incident_actions.{old_local_host}")
        topology = data.get("topology")
        if new_local_host is not None and isinstance(topology, dict):
            _replace_topology_alias(topology, old_local_host, new_local_host)
        elif _topology_contains(topology, old_local_host):
            data.pop("topology", None)
            dropped.append("topology")
        if old_group is not None and new_local_host is not None:
            groups = data.setdefault("host_groups", {})
            if isinstance(groups, dict):
                groups.setdefault(new_local_host, old_group)
        elif old_group is not None:
            dropped.append(f"host_groups.{old_local_host}")
    elif new_local_host is not None:
        hosts.append(new_local_host)

    data["hosts"] = hosts
    data["local_host"] = new_local_host
    data["ssh_config"] = ssh_config
    discovery = source_config.ssh_discovery
    data["ssh_discovery"] = {
        "mode": "topology",
        "refresh_seconds": discovery.refresh_seconds,
        "resolve_timeout_seconds": discovery.resolve_timeout_seconds,
    }
    if auto_discover is not None:
        data["auto_discover"] = auto_discover
    if display_name is not None:
        overrides = data.setdefault("host_overrides", {})
        if isinstance(overrides, dict):
            overrides[new_local_host] = {"display_name": display_name}

    incident_overrides = data.get("incident_overrides")
    groups = data.get("host_groups")
    if isinstance(incident_overrides, dict) and isinstance(groups, dict):
        group_overrides = incident_overrides.get("groups")
        if isinstance(group_overrides, dict):
            configured_groups = set(groups.values())
            for group in tuple(group_overrides):
                if group not in configured_groups:
                    group_overrides.pop(group)
                    dropped.append(f"incident_overrides.groups.{group}")

    _private_target_directory(target_path.parent)
    _write_private_target(target_path, data)
    return MigrationResult(
        source=source_path,
        target=target_path,
        old_local_host=old_local_host,
        new_local_host=new_local_host,
        auto_discover=bool(data["auto_discover"]),
        dropped_fields=tuple(dropped),
    )
