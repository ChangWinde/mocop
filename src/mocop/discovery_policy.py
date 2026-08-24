"""Dependency-free parsing for the SSH inventory discovery policy."""

from __future__ import annotations

import math
from dataclasses import dataclass

_SSH_DISCOVERY_KEYS = {"mode", "refresh_seconds", "resolve_timeout_seconds"}
_SSH_DISCOVERY_MODES = frozenset({"aliases", "topology"})


class SshDiscoveryPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SshDiscoveryConfig:
    """Policy for turning effective OpenSSH routes into inventory metadata."""

    mode: str = "aliases"
    refresh_seconds: int = 300
    resolve_timeout_seconds: float = 3


def parse_ssh_discovery_config(raw: object) -> SshDiscoveryConfig:
    if not isinstance(raw, dict) or set(raw) - _SSH_DISCOVERY_KEYS:
        raise SshDiscoveryPolicyError(
            "ssh_discovery must contain only mode, refresh_seconds, and "
            "resolve_timeout_seconds"
        )
    mode = raw.get("mode", "aliases")
    if mode not in _SSH_DISCOVERY_MODES:
        raise SshDiscoveryPolicyError("ssh_discovery.mode must be aliases or topology")
    refresh = raw.get("refresh_seconds", 300)
    if (
        isinstance(refresh, bool)
        or not isinstance(refresh, int)
        or not 30 <= refresh <= 3600
    ):
        raise SshDiscoveryPolicyError(
            "ssh_discovery.refresh_seconds must be between 30 and 3600"
        )
    timeout = raw.get("resolve_timeout_seconds", 3)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or not 1 <= timeout <= 30
    ):
        raise SshDiscoveryPolicyError(
            "ssh_discovery.resolve_timeout_seconds must be between 1 and 30"
        )
    return SshDiscoveryConfig(
        mode=mode,
        refresh_seconds=refresh,
        resolve_timeout_seconds=float(timeout),
    )
