from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from glob import glob
from pathlib import Path
from typing import Protocol

from .config import MonitorConfig, is_safe_alias

_MAX_CONFIG_FILES = 128
_MAX_CONFIG_BYTES = 1_048_576


class HostSource(Protocol):
    def aliases(self, config: MonitorConfig) -> tuple[str, ...]: ...

    def hosts(self, config: MonitorConfig) -> tuple[str, ...]: ...


HostSourceFactory = Callable[[], HostSource]
_HOST_SOURCES: dict[str, HostSourceFactory] = {}


def register_host_source(name: str) -> Callable[[HostSourceFactory], HostSourceFactory]:
    def decorator(factory: HostSourceFactory) -> HostSourceFactory:
        _HOST_SOURCES[name] = factory
        return factory

    return decorator


def create_host_source(name: str) -> HostSource:
    try:
        return _HOST_SOURCES[name]()
    except KeyError as exc:
        raise KeyError(
            f"unknown host source {name!r}; available: {sorted(_HOST_SOURCES)}"
        ) from exc


@register_host_source("openssh-config")
class OpenSshConfigHostSource:
    """Enumerate literal Host aliases from OpenSSH config and its Include files."""

    def aliases(self, config: MonitorConfig) -> tuple[str, ...]:
        aliases = self._read_aliases(config.ssh_config)
        invalid = sorted(alias for alias in aliases if not is_safe_alias(alias))
        if invalid:
            raise ValueError(
                "host aliases must contain only letters, numbers, dots, underscores, "
                f"and hyphens: {', '.join(invalid)}"
            )
        return tuple(sorted(aliases))

    def hosts(self, config: MonitorConfig) -> tuple[str, ...]:
        discovered = set(config.hosts)
        if config.auto_discover:
            discovered.update(
                alias for alias in self.aliases(config) if not is_code_host_alias(alias)
            )

        invalid = sorted(host for host in discovered if not is_safe_alias(host))
        if invalid:
            raise ValueError(
                "host aliases must contain only letters, numbers, dots, underscores, "
                f"and hyphens: {', '.join(invalid)}"
            )
        return tuple(sorted(discovered - config.exclude_hosts))

    def _read_aliases(self, root: Path) -> set[str]:
        aliases: set[str] = set()
        visited: set[Path] = set()
        pending = [root.expanduser().resolve()]

        while pending:
            path = pending.pop()
            if path in visited:
                continue
            if len(visited) >= _MAX_CONFIG_FILES:
                raise ValueError("SSH config includes too many files")
            visited.add(path)

            try:
                if path.stat().st_size > _MAX_CONFIG_BYTES:
                    raise ValueError(f"SSH config is too large: {path}")
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except FileNotFoundError:
                if path == root:
                    return aliases
                continue
            except OSError as exc:
                raise ValueError(f"cannot read SSH config: {path}") from exc

            for line in lines:
                try:
                    tokens = shlex.split(line, comments=True, posix=True)
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
                elif keyword == "include":
                    for pattern in tokens[1:]:
                        expanded = self._expand_include(pattern, root.parent)
                        pending.extend(reversed(expanded))
        return aliases

    @staticmethod
    def _expand_include(pattern: str, ssh_directory: Path) -> list[Path]:
        candidate = Path(pattern).expanduser()
        if not candidate.is_absolute():
            candidate = ssh_directory / candidate
        return [Path(match).resolve() for match in sorted(glob(str(candidate)))]


_CODE_HOST_ALIAS = re.compile(
    r"(?:^|[._-])git(?:(?:hub|lab))?(?:$|[._-])",
    re.IGNORECASE,
)


def is_code_host_alias(alias: str) -> bool:
    """Return whether an alias visibly names Git or hosted Git infrastructure."""
    return bool(_CODE_HOST_ALIAS.search(alias))
