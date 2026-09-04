from __future__ import annotations

import errno
import http.client
import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import (
    BUNDLED_CONFIG_PATH,
    ConfigError,
    MonitorConfig,
    is_safe_alias,
    load_config,
    load_private_config,
)
from .privatefiles import (
    PRIVATE_FILE_MODE,
    acquire_private_lock,
    is_private_regular_file,
    release_private_lock,
)

SERVICE_NAME = "mocop.service"
ACCESS_TOKEN_NAME = "access-token"
CommandRunner = Callable[[tuple[str, ...]], int]
_MAX_UNIT_BYTES = 1_048_576
_UnitBackup = tuple[str, bytes | str | None, int]


class LifecycleError(RuntimeError):
    """Raised when local setup or service management cannot complete safely."""


def user_config_path(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    xdg_root = values.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg_root).expanduser() if xdg_root else Path.home() / ".config"
    return _absolute_without_resolving_symlinks(root / "mocop" / "config.json")


def user_unit_path(environ: dict[str, str] | None = None) -> Path:
    # The user manager is long lived and does not inherit one-off shell XDG
    # overrides. Keep one canonical source/lock path per UID, then enable its
    # absolute path so managers with a persistent custom XDG create a link.
    del environ
    root = Path.home() / ".config"
    return _absolute_without_resolving_symlinks(
        root / "systemd" / "user" / SERVICE_NAME
    )


def access_token_path(config_path: Path) -> Path:
    return _absolute_without_resolving_symlinks(config_path).with_name(
        ACCESS_TOKEN_NAME
    )


def ensure_access_token(config_path: Path) -> Path:
    """Create or validate the private per-install dashboard credential."""
    path = access_token_path(config_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    token = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    except FileExistsError:
        read_access_token(path)
        return path
    except OSError as exc:
        raise LifecycleError("cannot create dashboard access token") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise LifecycleError("cannot write dashboard access token") from exc
    return path


def read_access_token(path: Path) -> str:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not is_private_regular_file(metadata) or not 32 <= metadata.st_size <= 256:
            raise LifecycleError("dashboard access token must be a private file")
        with os.fdopen(descriptor, "r", encoding="ascii") as stream:
            descriptor = -1
            token = stream.read(257).strip()
    except LifecycleError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LifecycleError(
                "dashboard access token must be a private file"
            ) from exc
        raise LifecycleError("cannot read dashboard access token") from exc
    except UnicodeError as exc:
        raise LifecycleError("cannot read dashboard access token") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not 32 <= len(token) <= 192 or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in token
    ):
        raise LifecycleError("dashboard access token is invalid")
    return token


def initialize_config(
    path: Path,
    hosts: Sequence[str],
    *,
    local_host: str | None = None,
    display_name: str | None = None,
    ssh_config: str | None = None,
    auto_discover: bool = False,
) -> Path:
    """Create a private config once; never replace an existing operator config."""
    target = _absolute_without_resolving_symlinks(path)
    normalized_local = local_host.strip() if local_host is not None else None
    candidates = (*((normalized_local,) if normalized_local else ()), *hosts)
    normalized_hosts = tuple(dict.fromkeys(host.strip() for host in candidates))
    invalid = [host for host in normalized_hosts if not is_safe_alias(host)]
    if invalid:
        raise LifecycleError(f"invalid SSH host alias: {', '.join(invalid)}")
    if local_host is not None and normalized_local is None:
        raise LifecycleError("local host alias cannot be empty")
    if display_name is not None and normalized_local is None:
        raise LifecycleError("display name requires a local host")

    try:
        data = json.loads(BUNDLED_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(
            "the bundled configuration template is unavailable"
        ) from exc
    data["hosts"] = list(normalized_hosts)
    data["local_host"] = normalized_local
    data["auto_discover"] = auto_discover
    if ssh_config is not None:
        data["ssh_config"] = ssh_config
    if display_name is not None:
        data["host_overrides"] = {normalized_local: {"display_name": display_name}}
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
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
    try:
        load_config(target)
    except ConfigError as exc:
        target.unlink(missing_ok=True)
        raise LifecycleError(f"cannot create valid configuration: {exc}") from exc
    return target


def _systemd_quote(value: Path) -> str:
    text = str(value)
    _validate_service_path(text)
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _systemd_optional_environment_file(value: Path) -> str:
    """Render an optional EnvironmentFile= path that systemd actually reads.

    ``EnvironmentFile=`` takes the whole remaining line as one path after the
    optional ``-`` prefix; systemd does not word-split or unquote it. Quoting
    (or C-style ``\\xNN`` escaping) makes the value start with ``"`` (or ``\\``),
    which fails the absolute-path check, so systemd ignores the whole line and
    the ``-`` prefix suppresses the error — webhook credentials then never
    load. A bare absolute path with spaces is valid; only ``%`` needs escaping
    against specifier expansion. Control characters are already rejected by
    ``_validate_service_path``.
    """
    text = str(value)
    _validate_service_path(text)
    return f"-{text.replace('%', '%%')}"


def _validate_service_path(text: str) -> None:
    if any(
        ord(character) < 32
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in text
    ):
        raise LifecycleError("service paths must be valid UTF-8 without controls")


def _absolute_without_resolving_symlinks(path: Path) -> Path:
    try:
        return Path(os.path.abspath(path.expanduser()))
    except (RuntimeError, UnicodeError, OSError) as exc:
        raise LifecycleError("service path is invalid") from exc


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LifecycleError("service unit directory cannot be inspected") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise LifecycleError(
            "service unit directory must be owner-controlled and not writable by others"
        )


def render_user_unit(
    python_executable: Path,
    config_path: Path,
    token_path: Path | None = None,
) -> str:
    # A virtual environment is identified by the interpreter path used to launch
    # it. Resolving that symlink would silently escape the environment.
    executable = _systemd_quote(_absolute_without_resolving_symlinks(python_executable))
    resolved_config = _absolute_without_resolving_symlinks(config_path)
    config = _systemd_quote(resolved_config)
    token = _systemd_quote(token_path or access_token_path(resolved_config))
    environment_file = _systemd_optional_environment_file(
        resolved_config.with_name("environment")
    )
    return f"""[Unit]
Description=Mocop AI-native GPU cluster monitor
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart={executable} -m mocop --managed-service --access-token-file={token} --config={config}
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
EnvironmentFile={environment_file}
NoNewPrivileges=true
StateDirectory=mocop
StateDirectoryMode=0700
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
    _atomic_write_bytes(path, content.encode("utf-8"), mode)


def _atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_temporary)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
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
        self.config_path = _absolute_without_resolving_symlinks(config_path)
        # Resolving the final component would turn an attacker-controlled or
        # stale symlink into authority to overwrite/delete its target.
        self.unit_path = _absolute_without_resolving_symlinks(unit_path)
        self.python_executable = _absolute_without_resolving_symlinks(python_executable)
        self.access_token_path = access_token_path(self.config_path)
        self._run = run
        self._install_backup: _UnitBackup | None = None
        self._previous_enabled = False
        self._previous_active = False
        self._unit_mutated = False
        self._lifecycle_lock_fd: int | None = None

    def _acquire_lifecycle_lock(self) -> None:
        if self._lifecycle_lock_fd is not None:
            return
        self.unit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_private_directory(self.unit_path.parent)
        lock_path = self.unit_path.with_name(f".{SERVICE_NAME}.lock")
        try:
            self._lifecycle_lock_fd = acquire_private_lock(lock_path)
        except OSError as exc:
            raise LifecycleError("cannot lock service lifecycle operations") from exc

    def _release_lifecycle_lock(self) -> None:
        descriptor = self._lifecycle_lock_fd
        self._lifecycle_lock_fd = None
        if descriptor is not None:
            release_private_lock(descriptor)

    def _checked(self, *arguments: str) -> None:
        command = ("systemctl", "--user", *arguments)
        if self._run(command) != 0:
            raise LifecycleError(f"command failed: {' '.join(command)}")

    def install(self) -> MonitorConfig:
        self._acquire_lifecycle_lock()
        try:
            try:
                config = load_private_config(self.config_path)
            except ConfigError as exc:
                raise LifecycleError(
                    f"configuration is not ready: {self.config_path}: {exc}"
                ) from exc
            ensure_access_token(self.config_path)

            environment_path = self.config_path.with_name("environment")
            if environment_path.exists() or environment_path.is_symlink():
                try:
                    metadata = environment_path.lstat()
                except OSError as exc:
                    raise LifecycleError(
                        f"cannot inspect service environment: {environment_path}"
                    ) from exc
                # lstat metadata already reports a symlink as non-regular.
                if not is_private_regular_file(metadata):
                    raise LifecycleError(
                        "service environment must be a private regular file owned "
                        "by the current user"
                    )

            unit = render_user_unit(
                self.python_executable, self.config_path, self.access_token_path
            )
            self._install_backup = self._capture_unit()
            self._previous_enabled = (
                self._run(
                    ("systemctl", "--user", "is-enabled", "--quiet", SERVICE_NAME)
                )
                == 0
            )
            self._previous_active = (
                self._run(("systemctl", "--user", "is-active", "--quiet", SERVICE_NAME))
                == 0
            )
            self._unit_mutated = True
            _atomic_write(self.unit_path, unit, 0o644)
            self._checked("daemon-reload")
            # An absolute path makes systemd link the exact lexical unit into
            # the running user manager even when this shell's XDG environment
            # differs from the manager's startup environment.
            self._checked("enable", str(self.unit_path))
            self._checked("restart", SERVICE_NAME)
            return config
        except BaseException as install_error:
            try:
                self.rollback_install()
            except LifecycleError as rollback_error:
                raise LifecycleError(
                    f"installation failed and rollback is incomplete: {rollback_error}"
                ) from install_error
            raise

    def commit_install(self) -> None:
        """Forget rollback state after the caller verifies service health."""
        self._install_backup = None
        self._unit_mutated = False
        self._release_lifecycle_lock()

    def rollback_install(self) -> None:
        """Restore unit bytes and prior systemd state, or report incompleteness."""
        backup = self._install_backup
        if backup is None:
            self._release_lifecycle_lock()
            return
        if not self._unit_mutated:
            self._install_backup = None
            self._release_lifecycle_lock()
            return
        self._acquire_lifecycle_lock()
        kind, payload, mode = backup
        try:
            if kind == "missing":
                self.unit_path.unlink(missing_ok=True)
            elif kind == "regular":
                assert isinstance(payload, bytes)
                _atomic_write_bytes(self.unit_path, payload, mode)
            else:
                assert kind == "symlink" and isinstance(payload, str)
                temporary = self.unit_path.with_name(
                    f".{self.unit_path.name}.{secrets.token_hex(8)}.rollback"
                )
                temporary.symlink_to(payload)
                os.replace(temporary, self.unit_path)
        except OSError as exc:
            self._release_lifecycle_lock()
            raise LifecycleError("service unit rollback failed") from exc
        commands = (
            ("systemctl", "--user", "daemon-reload"),
            (
                "systemctl",
                "--user",
                "enable" if self._previous_enabled else "disable",
                str(self.unit_path) if self._previous_enabled else SERVICE_NAME,
            ),
            (
                "systemctl",
                "--user",
                "restart" if self._previous_active else "stop",
                SERVICE_NAME,
            ),
        )
        failed: list[str] = []
        try:
            for command in commands:
                try:
                    if self._run(command) != 0:
                        failed.append(command[2])
                except Exception:
                    failed.append(command[2])
        finally:
            self._release_lifecycle_lock()
        if failed:
            raise LifecycleError(
                "systemd rollback commands failed: " + ", ".join(failed)
            )
        self._install_backup = None
        self._unit_mutated = False

    def _capture_unit(self) -> _UnitBackup:
        try:
            metadata = self.unit_path.lstat()
        except FileNotFoundError:
            return ("missing", None, 0)
        except OSError as exc:
            raise LifecycleError(
                f"cannot inspect service unit: {self.unit_path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            try:
                return ("symlink", os.readlink(self.unit_path), 0)
            except OSError as exc:
                raise LifecycleError(
                    f"cannot inspect service unit: {self.unit_path}"
                ) from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_UNIT_BYTES:
            raise LifecycleError("existing service unit is not a bounded regular file")
        try:
            return (
                "regular",
                self.unit_path.read_bytes(),
                stat.S_IMODE(metadata.st_mode),
            )
        except OSError as exc:
            raise LifecycleError(f"cannot read service unit: {self.unit_path}") from exc

    def status(self) -> int:
        result = self._run(
            ("systemctl", "--user", "status", "--no-pager", SERVICE_NAME)
        )
        return 0 if result == 0 else 1

    def wait_until_active(
        self,
        *,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Poll the unit's active state through the injected command runner.

        systemd reports a Type=simple unit active the moment it forks, so
        each check is preceded by one poll interval: an immediately crashing
        service is then seen in its failed or restart-pending state instead
        of being reported as a false success.
        """
        deadline = clock() + timeout_seconds
        while True:
            sleep(poll_interval_seconds)
            command = ("systemctl", "--user", "is-active", "--quiet", SERVICE_NAME)
            if self._run(command) == 0:
                return True
            if clock() >= deadline:
                return False

    def wait_until_healthy(
        self,
        host: str,
        port: int,
        *,
        timeout_seconds: float = 8.0,
        poll_interval_seconds: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Verify that the newly installed authenticated Mocop API is live.

        The capability is never sent here. ``wait_until_active`` already
        confirmed this exact user unit is running, and its generated
        ExecStart binds the config and sibling token this installer wrote, so
        the running service's token matches by construction. Transmitting it
        would instead expose it to whatever process holds the loopback port
        if our own unit crash-loops (a Type=simple unit reports active on
        fork, before a bind failure). Confirming liveness with an
        unauthenticated request that must be rejected proves the listener is a
        Mocop instance enforcing authentication and also catches a service
        that started without a readable token (which would answer 200).
        """
        connect_host = (
            "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
        )
        deadline = clock() + max(0.0, timeout_seconds)
        while True:
            connection: http.client.HTTPConnection | None = None
            try:
                connection = http.client.HTTPConnection(
                    connect_host,
                    port,
                    timeout=min(1.0, max(0.1, deadline - clock())),
                )
                connection.request(
                    "GET",
                    "/api/meta",
                )
                response = connection.getresponse()
                payload = response.read(4097)
                if response.status == 200 and len(payload) <= 4096:
                    meta = json.loads(payload)
                    if isinstance(meta, dict) and meta.get("apiVersion") == "2":
                        connection.request("GET", "/api/snapshot")
                        protected = connection.getresponse()
                        protected.read()
                        if protected.status == 403:
                            return True
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                http.client.HTTPException,
            ):
                # BadStatusLine and friends are transient just like a refused
                # connection: a service that answers garbage while starting up
                # must be polled again, not crash the installer mid-rollback.
                pass
            finally:
                if connection is not None:
                    connection.close()
            if clock() >= deadline:
                return False
            sleep(min(poll_interval_seconds, max(0.0, deadline - clock())))

    def uninstall(self) -> None:
        try:
            self._acquire_lifecycle_lock()
            unit_exists = self.unit_path.exists() or self.unit_path.is_symlink()
            result = self._run(
                ("systemctl", "--user", "disable", "--now", SERVICE_NAME)
            )
            if result != 0 and unit_exists:
                raise LifecycleError(
                    "command failed: systemctl --user disable --now mocop.service"
                )
            try:
                self.unit_path.unlink(missing_ok=True)
            except OSError as exc:
                raise LifecycleError(
                    f"cannot remove service unit: {self.unit_path}"
                ) from exc
            self._checked("daemon-reload")
        finally:
            self._release_lifecycle_lock()
