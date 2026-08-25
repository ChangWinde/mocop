"""Bounded, read-only topology inference from effective OpenSSH options."""

from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol

from .config import (
    TOPOLOGY_MAX_LINKS,
    ConnectionTopologyConfig,
    MonitorConfig,
    TopologyLinkConfig,
    is_safe_alias,
)
from .probe import (
    _ActiveProcessRegistry,
    _ProcessCancelled,
    _ProcessOutputLimitExceeded,
    _run_bounded_process,
)

_SSH_G_MAX_OUTPUT_BYTES = 262_144
_SSH_ROUTE_KEYS = frozenset({"proxycommand", "proxyjump"})
_NONE_OPTIONS = frozenset({"", "none"})
_TRAILING_NUMERIC_ALIAS = re.compile(r"^(.+?)[-_.]?\d+$")


@dataclass(frozen=True, slots=True)
class SshRoute:
    """One sanitized effective route; raw commands and addresses are discarded."""

    kind: str
    hops: tuple[str, ...] = ()
    opaque: bool = False


@dataclass(frozen=True, slots=True)
class SshRouteResolution:
    known_aliases: tuple[str, ...]
    routes: tuple[tuple[str, SshRoute], ...]
    failures: tuple[str, ...]
    warnings: tuple[str, ...]

    def route_map(self) -> dict[str, SshRoute]:
        return dict(self.routes)


@dataclass(frozen=True, slots=True)
class SshTopologyProjection:
    topology: ConnectionTopologyConfig | None
    infrastructure_hosts: tuple[str, ...]
    host_groups: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]


class SshRouteResolver(Protocol):
    def resolve(
        self,
        alias: str,
        config: MonitorConfig,
        known_aliases: tuple[str, ...],
        timeout_seconds: float,
    ) -> SshRoute | None: ...


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    return environment


def _known_alias_index(aliases: tuple[str, ...]) -> dict[str, str]:
    buckets: dict[str, list[str]] = {}
    for alias in aliases:
        buckets.setdefault(alias.casefold(), []).append(alias)
    return {
        normalized: values[0]
        for normalized, values in buckets.items()
        if len(values) == 1
    }


def _alias_from_destination(value: str, known_aliases: dict[str, str]) -> str | None:
    candidate = value.strip()
    if not candidate or candidate.startswith("-"):
        return None
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[1]
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing < 0:
            return None
        candidate = candidate[1:closing]
    elif candidate.count(":") == 1:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            candidate = host
    return known_aliases.get(candidate.casefold())


def _synthetic_proxy_alias(target: str, index: int) -> str:
    suffix = f"-{index}" if index else ""
    prefix = "proxy-"
    available = 253 - len(prefix) - len(suffix)
    return f"{prefix}{target[:available]}{suffix}"


def _alias_group_candidate(alias: str) -> str | None:
    separator = max(alias.rfind("-"), alias.rfind("_"), alias.rfind("."))
    if separator > 0:
        return alias[:separator]
    numeric = _TRAILING_NUMERIC_ALIAS.fullmatch(alias)
    if numeric is not None and numeric.group(1):
        return numeric.group(1)
    return None


def _proxy_jump_route(
    target: str, value: str, known_aliases: dict[str, str]
) -> SshRoute:
    hops: list[str] = []
    opaque = False
    for index, raw_hop in enumerate(value.split(",")):
        alias = _alias_from_destination(raw_hop, known_aliases)
        if alias is None:
            alias = _synthetic_proxy_alias(target, index)
            opaque = True
        if alias not in hops and alias != target:
            hops.append(alias)
    return SshRoute("proxyjump", tuple(hops), opaque)


def _proxy_command_route(
    target: str, value: str, known_aliases: dict[str, str]
) -> SshRoute:
    try:
        tokens = shlex.split(value, comments=False, posix=True)
    except ValueError:
        tokens = []
    hops: list[str] = []
    for token in tokens:
        alias = _alias_from_destination(token, known_aliases)
        if alias is not None and alias != target and alias not in hops:
            hops.append(alias)
    if hops:
        return SshRoute("proxycommand", tuple(hops))
    return SshRoute("proxycommand", (_synthetic_proxy_alias(target, 0),), opaque=True)


class OpenSshRouteResolver:
    """Resolve proxy-only OpenSSH options without initiating a connection."""

    def __init__(self) -> None:
        self._processes = _ActiveProcessRegistry()

    def cancel(self) -> None:
        self._processes.cancel()

    def resolve(
        self,
        alias: str,
        config: MonitorConfig,
        known_aliases: tuple[str, ...],
        timeout_seconds: float,
    ) -> SshRoute | None:
        try:
            completed = _run_bounded_process(
                ["ssh", "-G", "-F", str(config.ssh_config), "--", alias],
                input_text="",
                timeout_seconds=timeout_seconds,
                max_output_bytes=_SSH_G_MAX_OUTPUT_BYTES,
                environment=_environment(),
                process_registry=self._processes,
            )
        except (
            OSError,
            subprocess.TimeoutExpired,
            _ProcessOutputLimitExceeded,
            _ProcessCancelled,
        ):
            return None
        if completed.returncode != 0:
            return None
        options: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition(" ")
            normalized = key.strip().lower()
            if separator and normalized in _SSH_ROUTE_KEYS:
                options[normalized] = value.strip()
        aliases = _known_alias_index(known_aliases)
        proxy_jump = options.get("proxyjump", "").strip()
        if proxy_jump.lower() not in _NONE_OPTIONS:
            return _proxy_jump_route(alias, proxy_jump, aliases)
        proxy_command = options.get("proxycommand", "").strip()
        if proxy_command.lower() not in _NONE_OPTIONS:
            return _proxy_command_route(alias, proxy_command, aliases)
        return SshRoute("direct")


class SshTopologyPlanner:
    """Resolve routes and project them into a deterministic display tree."""

    def __init__(self, resolver: SshRouteResolver | None = None) -> None:
        self._resolver = resolver or OpenSshRouteResolver()

    def cancel(self) -> None:
        cancel = getattr(self._resolver, "cancel", None)
        if callable(cancel):
            cancel()

    def resolve(
        self,
        aliases: tuple[str, ...],
        targets: tuple[str, ...],
        config: MonitorConfig,
    ) -> SshRouteResolution:
        known = tuple(sorted(dict.fromkeys(aliases)))
        known_set = set(known)
        pending = {
            alias
            for alias in targets
            if alias in known_set and alias != config.local_host
        }
        routes: dict[str, SshRoute] = {}
        failures: set[str] = set()
        warnings: list[str] = []
        while pending:
            batch = tuple(sorted(pending))
            pending.clear()
            workers = max(1, min(config.max_workers, len(batch)))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="ssh-topology"
            ) as pool:
                futures = {
                    pool.submit(
                        self._resolver.resolve,
                        alias,
                        config,
                        known,
                        config.ssh_discovery.resolve_timeout_seconds,
                    ): alias
                    for alias in batch
                }
                for future in as_completed(futures):
                    alias = futures[future]
                    route = future.result()
                    if route is None:
                        failures.add(alias)
                        warnings.append(f"{alias}: SSH route could not be resolved")
                        continue
                    routes[alias] = route
                    if route.opaque:
                        warnings.append(f"{alias}: proxy route contains an opaque hop")
                    for hop in route.hops:
                        if (
                            hop in known_set
                            and hop not in routes
                            and hop not in failures
                            and hop != config.local_host
                        ):
                            pending.add(hop)
        return SshRouteResolution(
            known_aliases=known,
            routes=tuple(sorted(routes.items())),
            failures=tuple(sorted(failures)),
            warnings=tuple(sorted(set(warnings))),
        )

    @staticmethod
    def project(
        root: str,
        hosts: tuple[str, ...],
        resolution: SshRouteResolution,
    ) -> SshTopologyProjection:
        routes = resolution.route_map()
        known_aliases = set(resolution.known_aliases)
        warnings = list(resolution.warnings)
        chain_cache: dict[str, tuple[str, ...]] = {}

        def expanded_hops(alias: str) -> tuple[str, ...]:
            if alias in chain_cache:
                return chain_cache[alias]
            # Walk the first-hop chain iteratively: an operator config may
            # nest ProxyJump hundreds of levels deep, and a RecursionError
            # would escape the collector's (OSError, ValueError) boundary
            # and stop all monitoring instead of degrading one route.
            stack: list[str] = []
            seen: set[str] = set()
            prefix: tuple[str, ...] = ()
            cursor = alias
            while True:
                route = routes.get(cursor)
                if route is None or not route.hops:
                    chain_cache[cursor] = ()
                    break
                stack.append(cursor)
                seen.add(cursor)
                first = route.hops[0]
                if first not in routes:
                    break
                if first in chain_cache:
                    prefix = chain_cache[first]
                    break
                if first in seen:
                    warnings.append(f"{first}: proxy route cycle ignored")
                    break
                cursor = first
            # Unwind deepest-first: every level combines its own hops behind
            # the child chain while excluding itself and the root, exactly
            # like the recursive formulation this replaces.
            for node in reversed(stack):
                combined: list[str] = []
                for hop in (*prefix, *routes[node].hops):
                    if hop not in combined and hop not in {root, node}:
                        combined.append(hop)
                prefix = tuple(combined)
                chain_cache[node] = prefix
            return chain_cache[alias]

        parent_by_node: dict[str, str] = {}
        links: dict[tuple[str, str], TopologyLinkConfig] = {}
        host_groups: dict[str, str] = {}
        infrastructure: set[str] = set()
        direct_group_candidates: dict[str, str] = {}
        for host in sorted(hosts):
            if host == root:
                continue
            hops = expanded_hops(host)
            infrastructure.update(hops)
            known_hops = [hop for hop in hops if hop in known_aliases]
            if known_hops:
                host_groups[host] = known_hops[-1][:48]
            else:
                candidate = _alias_group_candidate(host)
                if candidate is not None:
                    direct_group_candidates[host] = candidate
            path = [root, *hops, host]
            for source, target in zip(path, path[1:], strict=False):
                if source == target:
                    continue
                previous_parent = parent_by_node.get(target)
                if previous_parent is not None and previous_parent != source:
                    warnings.append(
                        f"{target}: ambiguous proxy parent {source} ignored"
                    )
                    continue
                parent_by_node[target] = source
                route = routes.get(target)
                label = None
                if route is not None and route.kind == "proxycommand":
                    label = "ProxyCommand"
                elif route is not None and route.opaque:
                    label = "opaque ProxyJump"
                links[(source, target)] = TopologyLinkConfig(
                    source=source,
                    target=target,
                    transport="ssh",
                    label=label,
                )
        candidate_counts = Counter(direct_group_candidates.values())
        for host, candidate in direct_group_candidates.items():
            if candidate_counts[candidate] >= 2:
                host_groups[host] = candidate[:48]
        ordered_links = tuple(
            links[key] for key in sorted(links, key=lambda item: (item[0], item[1]))
        )
        if len(ordered_links) > TOPOLOGY_MAX_LINKS:
            children: dict[str, list[TopologyLinkConfig]] = {}
            for link in ordered_links:
                children.setdefault(link.source, []).append(link)
            retained: list[TopologyLinkConfig] = []
            pending = [root]
            while pending and len(retained) < TOPOLOGY_MAX_LINKS:
                source = pending.pop(0)
                for link in children.get(source, []):
                    if len(retained) >= TOPOLOGY_MAX_LINKS:
                        break
                    retained.append(link)
                    pending.append(link.target)
            ordered_links = tuple(retained)
            warnings.append(f"topology truncated to {TOPOLOGY_MAX_LINKS} display links")
        topology = (
            ConnectionTopologyConfig(root=root, links=ordered_links)
            if ordered_links
            else None
        )
        return SshTopologyProjection(
            topology=topology,
            infrastructure_hosts=tuple(sorted(infrastructure)),
            host_groups=tuple(sorted(host_groups.items())),
            warnings=tuple(sorted(set(warnings))),
        )


def default_topology_root(
    config: MonitorConfig, reserved_aliases: tuple[str, ...] = ()
) -> str:
    if config.local_host is not None:
        return config.local_host
    hostname = socket.gethostname().strip()
    reserved = set(reserved_aliases)
    if is_safe_alias(hostname) and hostname not in reserved:
        return hostname
    root = "mocop-local"
    suffix = 2
    while root in reserved:
        root = f"mocop-local-{suffix}"
        suffix += 1
    return root
