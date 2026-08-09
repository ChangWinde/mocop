from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import BUNDLED_CONFIG_PATH, ConfigError, is_safe_alias, load_config

SERVICE_NAME = "mocop.service"
CommandRunner = Callable[[tuple[str, ...]], int]


class LifecycleError(RuntimeError):
    """Raised when local setup or service management cannot complete safely."""


def user_config_path(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    xdg_root = values.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg_root).expanduser() if xdg_root else Path.home() / ".config"
    return (root / "mocop" / "config.json").resolve()


def user_unit_path(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    xdg_root = values.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg_root).expanduser() if xdg_root else Path.home() / ".config"
    return (root / "systemd" / "user" / SERVICE_NAME).resolve()


def initialize_config(path: Path, hosts: Sequence[str]) -> Path:
    """Create a private config once; never replace an existing operator config."""
    target = path.expanduser().resolve()
    normalized_hosts = tuple(dict.fromkeys(host.strip() for host in hosts))
    invalid = [host for host in normalized_hosts if not is_safe_alias(host)]
    if invalid:
        raise LifecycleError(f"invalid SSH host alias: {', '.join(invalid)}")

    try:
        data = json.loads(BUNDLED_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(
            "the bundled configuration template is unavailable"
        ) from exc
    data["hosts"] = list(normalized_hosts)
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise LifecycleError(f"configuration already exists: {target}") from exc
    except OSError as exc:
        raise LifecycleError(f"cannot create configuration: {target}") from exc

    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise LifecycleError(f"cannot write configuration: {target}") from exc
    return target


def _systemd_quote(value: Path) -> str:
    text = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise LifecycleError("service paths cannot contain control characters")
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _absolute_without_resolving_symlinks(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def render_user_unit(python_executable: Path, config_path: Path) -> str:
    # A virtual environment is identified by the interpreter path used to launch
    # it. Resolving that symlink would silently escape the environment.
    executable = _systemd_quote(_absolute_without_resolving_symlinks(python_executable))
    config = _systemd_quote(config_path.expanduser().resolve())
    return f"""[Unit]
Description=Mocop AI-native GPU cluster monitor
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart={executable} -m mocop --config={config}
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UMask=0077

[Install]
WantedBy=default.target
"""


def _run_systemctl(arguments: tuple[str, ...]) -> int:
    try:
        completed = subprocess.run(arguments, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(f"cannot run {' '.join(arguments[:2])}") from exc
    return completed.returncode


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise LifecycleError(f"cannot write service unit: {path}") from exc


class UserServiceManager:
    """Manage only mocop's user-level systemd unit through fixed commands."""

    def __init__(
        self,
        *,
        config_path: Path,
        unit_path: Path,
        python_executable: Path,
        run: CommandRunner = _run_systemctl,
    ) -> None:
        self.config_path = config_path.expanduser().resolve()
        self.unit_path = unit_path.expanduser().resolve()
        self.python_executable = _absolute_without_resolving_symlinks(python_executable)
        self._run = run

    def _checked(self, *arguments: str) -> None:
        command = ("systemctl", "--user", *arguments)
        if self._run(command) != 0:
            raise LifecycleError(f"command failed: {' '.join(command)}")

    def install(self) -> None:
        try:
            load_config(self.config_path)
        except ConfigError as exc:
            raise LifecycleError(
                f"configuration is not ready: {self.config_path}: {exc}"
            ) from exc

        unit = render_user_unit(self.python_executable, self.config_path)
        _atomic_write(self.unit_path, unit, 0o644)
        self._checked("daemon-reload")
        self._checked("enable", SERVICE_NAME)
        self._checked("restart", SERVICE_NAME)

    def status(self) -> int:
        return self._run(("systemctl", "--user", "status", "--no-pager", SERVICE_NAME))

    def uninstall(self) -> None:
        self._checked("disable", "--now", SERVICE_NAME)
        try:
            self.unit_path.unlink(missing_ok=True)
        except OSError as exc:
            raise LifecycleError(
                f"cannot remove service unit: {self.unit_path}"
            ) from exc
        self._checked("daemon-reload")
