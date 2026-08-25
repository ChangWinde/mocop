from __future__ import annotations

import os
import re
import shlex
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from glob import iglob
from pathlib import Path
from typing import Protocol, runtime_checkable

from .config import (
    CONFIG_MAX_HOST_ALIASES,
    ConnectionTopologyConfig,
    MonitorConfig,
    is_safe_alias,
)
from .ssh_topology import SshTopologyPlanner, default_topology_root

_MAX_CONFIG_FILES = 128
_MAX_CONFIG_BYTES = 1_048_576


class HostSource(Protocol):
    def aliases(self, config: MonitorConfig) -> tuple[str, ...]: ...

    def hosts(self, config: MonitorConfig) -> tuple[str, ...]: ...


@runtime_checkable
class CancellableHostSource(Protocol):
    def cancel(self) -> None: ...


@dataclass(frozen=True, slots=True)
class HostDiscoverySnapshot:
    aliases: tuple[str, ...]
    eligible_aliases: tuple[str, ...]
    hosts: tuple[str, ...]
    infrastructure_hosts: tuple[str, ...]
    host_groups: tuple[tuple[str, str], ...]
    topology: ConnectionTopologyConfig | None
    warnings: tuple[str, ...]
    mode: str


@runtime_checkable
class DiscoveryHostSource(Protocol):
    def discovery(self, config: MonitorConfig) -> HostDiscoverySnapshot: ...


def resolve_host_discovery(
    source: HostSource, config: MonitorConfig
) -> HostDiscoverySnapshot:
    if isinstance(source, DiscoveryHostSource):
        return source.discovery(config)
    return HostDiscoverySnapshot(
        aliases=(),
        eligible_aliases=(),
        hosts=source.hosts(config),
        infrastructure_hosts=(),
        host_groups=config.host_groups,
        topology=config.topology,
        warnings=(),
        mode="aliases",
    )


class OpenSshConfigHostSource:
    """Enumerate literal Host aliases from OpenSSH config and its Include files."""

    def __init__(
        self,
        planner: SshTopologyPlanner | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._planner = planner or SshTopologyPlanner()
        self._monotonic = monotonic or time.monotonic
        self._cache_lock = threading.Lock()
        self._cache_key: tuple[object, ...] | None = None
        self._cache_until = 0.0
        self._cache: HostDiscoverySnapshot | None = None

    def cancel(self) -> None:
        self._planner.cancel()

    def aliases(self, config: MonitorConfig) -> tuple[str, ...]:
        return self._safe_aliases(config)[0]

    def _safe_aliases(
        self, config: MonitorConfig
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Split discovered aliases into safe candidates and skip warnings.

        One unrelated exotic Host entry (an IPv6 literal, a token carrying
        ':' or '%') must not veto monitoring of explicitly configured hosts:
        an unsafe alias could never become a probe target anyway, so it
        leaves the candidate set with a visible warning instead of failing
        the whole discovery (ADR-0022). Explicit configuration keeps its own
        strict validation in the config loader and downstream guards.
        """
        aliases = self._read_aliases(config.ssh_config)
        skipped = tuple(
            f"{alias}: unsafe ssh alias ignored"
            for alias in sorted(alias for alias in aliases if not is_safe_alias(alias))
        )
        safe = {alias for alias in aliases if is_safe_alias(alias)}
        if len(safe) > CONFIG_MAX_HOST_ALIASES:
            raise ValueError(
                "SSH config contains more than "
                f"{CONFIG_MAX_HOST_ALIASES} literal host aliases"
            )
        return tuple(sorted(safe)), skipped

    def hosts(self, config: MonitorConfig) -> tuple[str, ...]:
        return self.discovery(config).hosts

    @staticmethod
    def _alias_only_hosts(
        config: MonitorConfig, aliases: tuple[str, ...]
    ) -> tuple[str, ...]:
        discovered = set(config.hosts)
        if config.auto_discover:
            discovered.update(
                alias for alias in aliases if not is_code_host_alias(alias)
            )

        invalid = sorted(host for host in discovered if not is_safe_alias(host))
        if invalid:
            raise ValueError(
                "host aliases must contain only letters, numbers, dots, underscores, "
                f"and hyphens: {', '.join(invalid)}"
            )
        return tuple(sorted(discovered - config.exclude_hosts))

    @staticmethod
    def _cache_identity(
        config: MonitorConfig, aliases: tuple[str, ...]
    ) -> tuple[object, ...]:
        return (
            config.ssh_config,
            aliases,
            config.auto_discover,
            config.hosts,
            config.exclude_hosts,
            config.local_host,
            config.host_groups,
            config.topology,
            config.max_workers,
            config.ssh_discovery,
        )

    def discovery(self, config: MonitorConfig) -> HostDiscoverySnapshot:
        aliases, alias_warnings = self._safe_aliases(config)
        eligible = tuple(
            alias
            for alias in aliases
            if alias not in config.exclude_hosts and not is_code_host_alias(alias)
        )
        if config.ssh_discovery.mode == "aliases":
            return HostDiscoverySnapshot(
                aliases=aliases,
                eligible_aliases=eligible,
                hosts=self._alias_only_hosts(config, aliases),
                infrastructure_hosts=(),
                host_groups=config.host_groups,
                topology=config.topology,
                warnings=alias_warnings,
                mode="aliases",
            )

        now = self._monotonic()
        cache_key = self._cache_identity(config, aliases) + (alias_warnings,)
        with self._cache_lock:
            if (
                self._cache is not None
                and self._cache_key == cache_key
                and now < self._cache_until
            ):
                return self._cache
            targets = tuple(
                sorted(set(eligible) | (set(config.hosts) - config.exclude_hosts))
            )
            resolution = self._planner.resolve(aliases, targets, config)
            route_map = resolution.route_map()
            infrastructure = {hop for route in route_map.values() for hop in route.hops}
            failed = set(resolution.failures)
            auto_hosts = set(eligible) - infrastructure - failed
            explicit_hosts = set(config.hosts) - config.exclude_hosts
            active = explicit_hosts | (auto_hosts if config.auto_discover else set())
            hosts = tuple(sorted(active))
            projection = self._planner.project(
                default_topology_root(config, aliases), hosts, resolution
            )
            inferred_groups = dict(projection.host_groups)
            inferred_groups.update(config.host_groups)
            snapshot = HostDiscoverySnapshot(
                aliases=aliases,
                eligible_aliases=tuple(
                    alias
                    for alias in eligible
                    if alias not in infrastructure and alias not in failed
                ),
                hosts=hosts,
                infrastructure_hosts=tuple(sorted(infrastructure)),
                host_groups=tuple(sorted(inferred_groups.items())),
                topology=config.topology or projection.topology,
                warnings=alias_warnings + projection.warnings,
                mode="topology",
            )
            self._cache_key = cache_key
            self._cache_until = now + config.ssh_discovery.refresh_seconds
            self._cache = snapshot
            return snapshot

    def _read_aliases(self, root: Path) -> set[str]:
        aliases: set[str] = set()
        visited: set[Path] = set()
        try:
            root_path = root.expanduser().resolve()
        except (RuntimeError, UnicodeError, OSError) as exc:
            raise ValueError("SSH config path is invalid") from exc
        pending = [root_path]
        total_bytes = 0

        while pending:
            path = pending.pop()
            if path in visited:
                continue
            if len(visited) >= _MAX_CONFIG_FILES:
                raise ValueError("SSH config includes too many files")
            visited.add(path)

            try:
                content = self._read_regular_file(path, _MAX_CONFIG_BYTES - total_bytes)
                total_bytes += len(content)
                lines = content.decode("utf-8", errors="replace").splitlines()
            except FileNotFoundError:
                if path == root_path:
                    return aliases
                continue
            except OSError as exc:
                raise ValueError(f"cannot read SSH config: {path}") from exc

            include_allowed = True
            for line in lines:
                try:
                    tokens = self._tokenize_option(line)
                except ValueError:
                    continue
                if not tokens:
                    continue
                keyword = tokens[0].lower()
                if keyword == "host":
                    for token in tokens[1:]:
                        if not token.startswith("!") and not any(
                            marker in token for marker in "*?"
                        ):
                            aliases.add(token)
                    # Includes in a specific Host block are conditional on the
                    # queried destination. Enumerating their declarations as
                    # global aliases would authorize names OpenSSH never uses.
                    include_allowed = tokens[1:] == ["*"]
                elif keyword == "match":
                    include_allowed = [token.lower() for token in tokens[1:]] == ["all"]
                elif keyword == "include":
                    if not include_allowed:
                        continue
                    for pattern in tokens[1:]:
                        expanded = self._expand_include(pattern, Path.home() / ".ssh")
                        if (
                            len(visited) + len(pending) + len(expanded)
                            > _MAX_CONFIG_FILES
                        ):
                            raise ValueError("SSH config includes too many files")
                        pending.extend(reversed(expanded))
        return aliases

    @staticmethod
    def _read_regular_file(path: Path, remaining_bytes: int) -> bytes:
        """Read one stable regular file under the aggregate byte budget."""
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"SSH config is not a regular file: {path}")
            if remaining_bytes < 0 or metadata.st_size > remaining_bytes:
                raise ValueError(f"SSH config is too large: {path}")
            chunks: list[bytes] = []
            consumed = 0
            while True:
                chunk = os.read(descriptor, min(65_536, remaining_bytes - consumed + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                consumed += len(chunk)
                if consumed > remaining_bytes:
                    raise ValueError(f"SSH config is too large: {path}")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _tokenize_option(line: str) -> list[str]:
        """Tokenize one ssh_config option using OpenSSH's option grammar."""
        quote: str | None = None
        escaped = False
        end = len(line)
        for index, character in enumerate(line):
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote != "'":
                escaped = True
                continue
            if quote is not None:
                if character == quote:
                    quote = None
                continue
            if character in {"'", '"'}:
                quote = character
                continue
            if character == "#" and (index == 0 or line[index - 1].isspace()):
                end = index
                break
        content = line[:end].strip()
        if not content:
            return []
        match = re.match(r"([^\s=]+)(.*)", content, flags=re.DOTALL)
        if match is None:
            return []
        keyword, arguments = match.groups()
        arguments = arguments.lstrip()
        if arguments.startswith("="):
            arguments = arguments[1:].lstrip()
        if not arguments:
            return [keyword]
        return [keyword, *shlex.split(arguments, comments=False, posix=True)]

    @staticmethod
    def _expand_include(pattern: str, ssh_directory: Path) -> list[Path]:
        try:
            candidate = Path(pattern).expanduser()
            if not candidate.is_absolute():
                candidate = ssh_directory / candidate
            matches: list[Path] = []
            for match in iglob(str(candidate)):
                matches.append(Path(match).resolve())
                if len(matches) > _MAX_CONFIG_FILES:
                    raise ValueError("SSH config includes too many files")
            return sorted(matches)
        except (RuntimeError, UnicodeError, OSError) as exc:
            raise ValueError("SSH Include path is invalid") from exc


_CODE_HOST_ALIAS = re.compile(
    r"(?:^|[._-])git(?:(?:hub|lab))?(?:$|[._-])",
    re.IGNORECASE,
)


def is_code_host_alias(alias: str) -> bool:
    """Return whether an alias visibly names Git or hosted Git infrastructure."""
    return bool(_CODE_HOST_ALIAS.search(alias))
