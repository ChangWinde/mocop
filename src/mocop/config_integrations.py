"""Section parsers for the integrations a configuration may declare.

Topology links, SQLite history, workload identity, and webhook endpoints are
each parsed here from their JSON section into the frozen configuration type,
with the same strict-schema stance as the rest of the loader: unknown keys,
wrong types, and out-of-range values are configuration errors, never
defaults. ``config_loader`` orchestrates; this module owns these sections.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .config import (
    TOPOLOGY_LABEL_MAX_LENGTH,
    TOPOLOGY_MAX_LINKS,
    TOPOLOGY_TRANSPORTS,
    ConfigError,
    ConnectionTopologyConfig,
    PersistenceConfig,
    TopologyLinkConfig,
    WebhookConfig,
    WorkloadConfig,
    is_safe_alias,
)

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


def connection_topology(raw: object) -> ConnectionTopologyConfig:
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


def persistence_config(data: dict[str, Any]) -> PersistenceConfig:
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


def workload_config(data: dict[str, Any]) -> WorkloadConfig:
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


def webhook_configs(data: dict[str, Any]) -> tuple[WebhookConfig, ...]:
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
