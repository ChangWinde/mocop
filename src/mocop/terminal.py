"""Open a local interactive SSH terminal to one monitored host.

The dashboard runs on the operator's own machine, so "open a terminal on this
node" is a local desktop action: write a tiny launcher script that runs
``ssh <alias>`` (showing ``df -h`` first) and hand it to the terminal
application through ``open``. Only macOS is supported; elsewhere the
capability is simply absent from ``/api/meta``.

Ghostty and Terminal.app are launched differently: Terminal executes a
``.command`` document in a fresh window of the running application, while
Ghostty has no CLI on macOS and needs a dedicated instance (``open -n``)
whose command, window-state and quit behaviour are set on its command line.
"""

from __future__ import annotations

import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from .config import is_safe_alias
from .remote_script import TERMINAL_ENVIRONMENT_SCRIPT

_OPEN = "/usr/bin/open"
_LAUNCH_TIMEOUT_SECONDS = 15
_COOLDOWN_SECONDS = 2.0
_DETAIL_LIMIT = 200
# Bound on the `app` body field; identifiers are short lowercase tokens.
TERMINAL_APP_ID_MAX_LENGTH = 32
# Ghostty inserts the command into a `bash -c "exec -l <command>"` wrapper, so
# the script path must not need shell quoting.
_SHELL_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
_DEFAULT_SSH_CONFIG = Path("~/.ssh/config").expanduser()
# The remote shell shows the disk table first, then continues as the user's
# normal login shell so the window stays useful after the quick look.
REMOTE_COMMAND = TERMINAL_ENVIRONMENT_SCRIPT + '\ndf -h; exec "${SHELL:-/bin/sh}" -l'
# HTTP projection of the non-success statuses, mirroring /api/probe: the
# status body is returned as-is plus a stable `code`.
RESPONSE_CODES: dict[str, tuple[HTTPStatus, str]] = {
    "unknown_host": (HTTPStatus.NOT_FOUND, "UNKNOWN_HOST"),
    "unknown_app": (HTTPStatus.BAD_REQUEST, "INVALID_SETTINGS"),
    "rate_limited": (HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED"),
    "failed": (HTTPStatus.INTERNAL_SERVER_ERROR, "TERMINAL_LAUNCH_FAILED"),
}

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class TerminalApp:
    id: str
    label: str
    bundle: Path

    def command(self, script: Path) -> list[str]:
        if self.id == "ghostty":
            return [
                _OPEN,
                "-n",
                "-a",
                str(self.bundle),
                "--args",
                "--window-save-state=never",
                "--quit-after-last-window-closed=true",
                f"--command={script}",
            ]
        return [_OPEN, "-a", str(self.bundle), str(script)]

    def describe(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


# Preference order: the operator's own terminal first, the system one last.
_APP_CANDIDATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ghostty", "Ghostty", ("/Applications/Ghostty.app", "~/Applications/Ghostty.app")),
    (
        "terminal",
        "Terminal.app",
        (
            "/System/Applications/Utilities/Terminal.app",
            "/Applications/Utilities/Terminal.app",
        ),
    ),
)


def installed_apps(candidates=_APP_CANDIDATES) -> tuple[TerminalApp, ...]:
    found = []
    for app_id, label, bundles in candidates:
        for bundle in bundles:
            path = Path(bundle).expanduser()
            if path.is_dir():
                found.append(TerminalApp(app_id, label, path))
                break
    return tuple(found)


def _run_open(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_LAUNCH_TIMEOUT_SECONDS,
    )


def _private_script_dir(candidates: Iterable[Path]) -> Path:
    """First candidate that is (or can become) a shell-safe, owner-only dir."""
    for candidate in candidates:
        if not _SHELL_SAFE_PATH.fullmatch(str(candidate)):
            continue
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        if candidate.is_symlink():
            continue
        info = candidate.stat()
        if info.st_uid != os.getuid():
            continue
        if info.st_mode & 0o077:
            candidate.chmod(0o700)
        return candidate
    raise OSError("no private directory available for terminal launch scripts")


class TerminalLauncher:
    def __init__(
        self,
        known_hosts: Callable[[], Iterable[str]],
        ssh_config: Path,
        *,
        apps: Sequence[TerminalApp],
        local_host: str | None = None,
        script_dirs: Sequence[Path] | None = None,
        runner: Runner = _run_open,
        clock: Callable[[], float] = time.monotonic,
        cooldown_seconds: float = _COOLDOWN_SECONDS,
    ) -> None:
        if not apps:
            raise ValueError("terminal launcher needs at least one application")
        self._known_hosts = known_hosts
        self._ssh_config = ssh_config
        self._apps = tuple(apps)
        self._apps_by_id = {app.id: app for app in self._apps}
        self._local_host = local_host
        self._script_dirs = (
            tuple(script_dirs)
            if script_dirs
            else (
                Path(tempfile.gettempdir()) / "mocop-terminal",
                Path.home() / ".cache" / "mocop" / "terminal",
            )
        )
        self._runner = runner
        self._clock = clock
        self._cooldown = cooldown_seconds
        self._lock = threading.Lock()
        self._last_open_at: dict[str, float] = {}

    @classmethod
    def detect(
        cls,
        known_hosts: Callable[[], Iterable[str]],
        ssh_config: Path,
        *,
        local_host: str | None = None,
    ) -> TerminalLauncher | None:
        """A launcher for this desktop, or None when the platform has none."""
        if sys.platform != "darwin":
            return None
        apps = installed_apps()
        if not apps:
            return None
        return cls(known_hosts, ssh_config, apps=apps, local_host=local_host)

    @property
    def apps(self) -> tuple[dict[str, str], ...]:
        return tuple(app.describe() for app in self._apps)

    def open(self, host: str, app: str | None = None) -> dict[str, object]:
        # None or "" both mean "the preferred application" so the dashboard can
        # always send the field the manifest requires.
        app_id = app or self._apps[0].id
        result: dict[str, object] = {"accepted": False, "host": host, "app": app_id}
        target = self._apps_by_id.get(app_id)
        if target is None:
            return {"status": "unknown_app", **result}
        if not is_safe_alias(host) or host not in set(self._known_hosts()):
            return {"status": "unknown_host", **result}
        with self._lock:
            now = self._clock()
            elapsed = now - self._last_open_at.get(host, -math.inf)
            if elapsed < self._cooldown:
                retry_after = round(self._cooldown - elapsed, 2)
                return {
                    "status": "rate_limited",
                    **result,
                    "retryAfterSeconds": retry_after,
                }
            self._last_open_at[host] = now
        try:
            script = self.write_script(host)
            completed = self._runner(target.command(script))
        except (OSError, subprocess.SubprocessError) as exc:
            return {"status": "failed", **result, "detail": _detail(str(exc))}
        if completed.returncode != 0:
            detail = _detail(
                completed.stderr or f"open exited with {completed.returncode}"
            )
            return {"status": "failed", **result, "detail": detail}
        return {"status": "opened", **result, "accepted": True}

    def script_text(self, host: str) -> str:
        if host == self._local_host:
            session = REMOTE_COMMAND
        else:
            ssh = ["ssh"]
            # Only a non-default file needs -F; passing the default path would
            # also switch off /etc/ssh/ssh_config, unlike a plain `ssh <alias>`.
            if self._ssh_config != _DEFAULT_SSH_CONFIG:
                ssh += ["-F", str(self._ssh_config)]
            ssh += ["-t", "--", host, REMOTE_COMMAND]
            session = "exec " + shlex.join(ssh)
        return (
            "#!/bin/sh\n"
            f"# Generated by Mocop for {host}; safe to delete.\n"
            f"printf '\\033]0;%s\\007' {shlex.quote(host)}\n"
            f"{session}\n"
        )

    def write_script(self, host: str) -> Path:
        directory = _private_script_dir(self._script_dirs)
        path = directory / f"{host}.command"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(self.script_text(host))
        path.chmod(0o700)
        return path


def _detail(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:_DETAIL_LIMIT]
