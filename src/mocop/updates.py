"""Release checking and wheel-only self-update (ADR-0026).

The repository is hardcoded so no configuration value can redirect the
supply chain. The browser can only trigger the fixed apply action; the
target version is always the latest official release, chosen server-side.
Installation uses wheels exclusively, so no downloaded code executes during
the install itself, and a verified interpreter probe gates the supervised
restart.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import __version__


class UpdateStatusSource(Protocol):
    """The update-manager surface the web layer projects and triggers."""

    def status(self) -> dict[str, object]: ...

    def apply(self) -> tuple[bool, str]: ...


_REPOSITORY = "ChangWinde/mocop"
_RELEASE_URL = f"https://api.github.com/repos/{_REPOSITORY}/releases/latest"
_USER_AGENT = f"mocop/{__version__} (self-update)"
_TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_MAX_METADATA_BYTES = 256 * 1024
_MAX_SUMS_BYTES = 64 * 1024
_MAX_WHEEL_BYTES = 64 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 20
_INSTALL_TIMEOUT_SECONDS = 300
_UPDATE_KEYS = {"mode", "check_interval_seconds"}
_UPDATE_MODES = frozenset({"off", "check", "self-update"})


class UpdatesPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UpdatesConfig:
    """Release-currency policy; the shipped default performs no requests."""

    mode: str = "off"
    check_interval_seconds: int = 21_600


def parse_updates_config(raw: object) -> UpdatesConfig:
    if not isinstance(raw, dict) or set(raw) - _UPDATE_KEYS:
        raise UpdatesPolicyError(
            "updates must contain only mode and check_interval_seconds"
        )
    mode = raw.get("mode", "off")
    if mode not in _UPDATE_MODES:
        raise UpdatesPolicyError("updates.mode must be off, check, or self-update")
    interval = raw.get("check_interval_seconds", 21_600)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 3_600 <= interval <= 86_400
    ):
        raise UpdatesPolicyError(
            "updates.check_interval_seconds must be between 3600 and 86400"
        )
    return UpdatesConfig(mode=mode, check_interval_seconds=interval)


def parse_release_tag(tag: object) -> tuple[int, int, int] | None:
    """Strictly parse a release tag; anything else is not an update source."""
    if not isinstance(tag, str):
        return None
    match = _TAG_PATTERN.fullmatch(tag.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _fetch_bounded(url: str, limit: int) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("update sources must use https")
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("update source response exceeds its size bound")
    return payload


def _utc_now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class UpdateManager:
    """Poll the official releases and apply one verified wheel on request.

    Thread model: one daemon poll thread (mode != off), one single-flight
    apply thread, and a lock guarding the small status record that the web
    layer projects.
    """

    def __init__(
        self,
        config: UpdatesConfig,
        *,
        current_version: str = __version__,
        restart: Callable[[], None] | None = None,
        fetch: Callable[[str, int], bytes] = _fetch_bounded,
        run_process: Callable[..., subprocess.CompletedProcess] | None = None,
        clock: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._config = config
        self._current = parse_release_tag(f"v{current_version}")
        self._current_version = current_version
        self._restart = restart
        self._fetch = fetch
        self._run = run_process or self._run_bounded
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._apply_thread: threading.Thread | None = None
        self._latest: tuple[int, int, int] | None = None
        self._assets: dict[str, str] = {}
        self._checked_at: str | None = None
        self._state = "idle"
        self._detail: str | None = None

    @staticmethod
    def _run_bounded(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def start(self) -> None:
        if self._config.mode == "off" or self._poll_thread is not None:
            return
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="mocop-update-check", daemon=True
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, object]:
        with self._lock:
            latest = self._latest
            return {
                "mode": self._config.mode,
                "currentVersion": self._current_version,
                "latestVersion": ".".join(map(str, latest)) if latest else None,
                "updateAvailable": self._update_available_locked(),
                "checkedAt": self._checked_at,
                "state": self._state,
                "detail": self._detail,
            }

    def _update_available_locked(self) -> bool:
        return (
            self._latest is not None
            and self._current is not None
            and self._latest > self._current
        )

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self.check_now()
            self._stop.wait(self._config.check_interval_seconds)

    def check_now(self) -> None:
        """One bounded release poll; failures leave the last good answer."""
        try:
            release = json.loads(self._fetch(_RELEASE_URL, _MAX_METADATA_BYTES))
            latest = parse_release_tag(
                release.get("tag_name") if isinstance(release, dict) else None
            )
            if latest is None:
                raise ValueError("latest release does not carry a v<semver> tag")
            assets: dict[str, str] = {}
            for asset in release.get("assets", []) or []:
                name = asset.get("name") if isinstance(asset, dict) else None
                url = (
                    asset.get("browser_download_url")
                    if isinstance(asset, dict)
                    else None
                )
                if (
                    isinstance(name, str)
                    and isinstance(url, str)
                    and url.startswith("https://")
                ):
                    assets[name] = url
            with self._lock:
                self._latest = latest
                self._assets = assets
                self._checked_at = self._clock()
                if self._state == "failed" and self._update_available_locked():
                    # A newer release supersedes a failed attempt at an older one.
                    self._state = "idle"
                    self._detail = None
        except Exception as error:  # noqa: BLE001 - poller must never die
            with self._lock:
                self._checked_at = self._clock()
                if self._state == "idle":
                    self._detail = f"release check failed: {error}"

    def apply(self) -> tuple[bool, str]:
        """Start the single-flight update worker; returns (accepted, message)."""
        if self._config.mode != "self-update":
            return False, "self-update is not enabled"
        if self._restart is None:
            return False, "self-update requires the managed service"
        with self._lock:
            if self._state == "updating":
                return False, "an update is already in progress"
            if not self._update_available_locked():
                return False, "no newer release is available"
            self._state = "updating"
            self._detail = "downloading"
        self._apply_thread = threading.Thread(
            target=self._apply_worker, name="mocop-update-apply", daemon=True
        )
        self._apply_thread.start()
        return True, "update started"

    def _fail(self, message: str) -> None:
        with self._lock:
            self._state = "failed"
            self._detail = (
                f"{message}; recover manually with: uv tool install --force "
                f"--from git+https://github.com/{_REPOSITORY}.git@<tag> mocop"
            )

    def _apply_worker(self) -> None:
        try:
            with self._lock:
                latest = self._latest
                assets = dict(self._assets)
            if latest is None or self._current is None or latest <= self._current:
                self._fail("release state changed before download")
                return
            version = ".".join(map(str, latest))
            wheel_name = f"mocop-{version}-py3-none-any.whl"
            wheel_url = assets.get(wheel_name)
            sums_url = assets.get("SHA256SUMS")
            if wheel_url is None or sums_url is None:
                self._fail("the latest release does not ship a verifiable wheel")
                return

            sums = self._fetch(sums_url, _MAX_SUMS_BYTES).decode("utf-8", "strict")
            expected = None
            for line in sums.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == wheel_name:
                    expected = parts[0].lower()
            if expected is None or not re.fullmatch(r"[0-9a-f]{64}", expected):
                self._fail("SHA256SUMS does not name the release wheel")
                return

            wheel_bytes = self._fetch(wheel_url, _MAX_WHEEL_BYTES)
            digest = hashlib.sha256(wheel_bytes).hexdigest()
            if digest != expected:
                self._fail("wheel digest does not match SHA256SUMS")
                return

            with self._lock:
                self._detail = "installing"
            with tempfile.TemporaryDirectory(prefix="mocop-update-") as directory:
                wheel_path = Path(directory) / wheel_name
                wheel_path.write_bytes(wheel_bytes)
                if not self._install(wheel_path):
                    return
                if not self._verify_installed(version):
                    self._fail("installed environment does not report the new version")
                    return

            with self._lock:
                self._state = "restarting"
                self._detail = f"restarting into {version}"
            restart = self._restart
            assert restart is not None
            restart()
        except Exception as error:  # noqa: BLE001 - worker reports, never raises
            self._fail(f"update failed: {error}")

    def _install(self, wheel_path: Path) -> bool:
        pip_probe = self._run(
            [sys.executable, "-m", "pip", "--version"], _FETCH_TIMEOUT_SECONDS
        )
        if pip_probe.returncode == 0:
            result = self._run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--force-reinstall",
                    str(wheel_path),
                ],
                _INSTALL_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                self._fail(f"pip install failed: {result.stderr.strip()[-300:]}")
                return False
            return True
        uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
        if not Path(uv).exists():
            self._fail("neither pip nor uv is available to install the wheel")
            return False
        result = self._run(
            ["uv", "tool", "install", "--force", "--from", str(wheel_path), "mocop"]
            if shutil.which("uv")
            else [uv, "tool", "install", "--force", "--from", str(wheel_path), "mocop"],
            _INSTALL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            self._fail(f"uv tool install failed: {result.stderr.strip()[-300:]}")
            return False
        return True

    def _verify_installed(self, version: str) -> bool:
        probe = self._run(
            [
                sys.executable,
                "-c",
                "import importlib.metadata as m; print(m.version('mocop'))",
            ],
            _FETCH_TIMEOUT_SECONDS,
        )
        return probe.returncode == 0 and probe.stdout.strip() == version
