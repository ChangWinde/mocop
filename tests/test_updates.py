from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from mocop.updates import (
    UpdateManager,
    UpdatesConfig,
    UpdatesPolicyError,
    parse_release_tag,
    parse_updates_config,
)

_RELEASE_URL = "https://api.github.com/repos/ChangWinde/mocop/releases/latest"


def release_payload(tag: str, wheel: bytes, version: str) -> dict[str, object]:
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": f"mocop-{version}-py3-none-any.whl",
                "browser_download_url": f"https://example.test/{version}.whl",
            },
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://example.test/SHA256SUMS",
            },
        ],
    }


class _Fetcher:
    """Bounded fake for the two update sources."""

    def __init__(self, version: str = "9.9.9", wheel: bytes = b"wheel-bytes") -> None:
        self.version = version
        self.wheel = wheel
        self.calls: list[str] = []

    def __call__(self, url: str, limit: int) -> bytes:
        self.calls.append(url)
        if url == _RELEASE_URL:
            return json.dumps(
                release_payload(f"v{self.version}", self.wheel, self.version)
            ).encode()
        if url.endswith("SHA256SUMS"):
            digest = hashlib.sha256(self.wheel).hexdigest()
            return f"{digest}  mocop-{self.version}-py3-none-any.whl\n".encode()
        if url.endswith(".whl"):
            return self.wheel
        raise AssertionError(f"unexpected fetch: {url}")


class _Runner:
    """Fake toolchain: pip is available (unless told otherwise) and installs
    succeed (unless told otherwise)."""

    def __init__(
        self,
        installed_version: str = "9.9.9",
        *,
        pip: bool = True,
        install_stderr: str | None = None,
    ) -> None:
        self.installed_version = installed_version
        self.pip = pip
        self.install_stderr = install_stderr
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], timeout: float
    ) -> subprocess.CompletedProcess:
        self.commands.append(command)
        if command[:3] == [sys.executable, "-m", "pip"] and command[3] == "--version":
            if self.pip:
                return subprocess.CompletedProcess(command, 0, "pip 25", "")
            return subprocess.CompletedProcess(command, 1, "", "No module named pip")
        if "install" in command:
            if self.install_stderr is not None:
                return subprocess.CompletedProcess(command, 1, "", self.install_stderr)
            return subprocess.CompletedProcess(command, 0, "installed", "")
        if command[-1].startswith("import importlib.metadata"):
            return subprocess.CompletedProcess(
                command, 0, f"{self.installed_version}\n", ""
            )
        raise AssertionError(f"unexpected command: {command}")


class UpdatesPolicyTests(unittest.TestCase):
    def test_defaults_are_off_and_bounded(self) -> None:
        config = parse_updates_config({})
        self.assertEqual(
            config, UpdatesConfig(mode="off", check_interval_seconds=21_600)
        )
        parsed = parse_updates_config(
            {"mode": "self-update", "check_interval_seconds": 3_600}
        )
        self.assertEqual(parsed.mode, "self-update")

    def test_rejects_unknown_keys_modes_and_intervals(self) -> None:
        for raw in (
            "text",
            {"mode": "auto"},
            {"repository": "evil/repo"},
            {"check_interval_seconds": 60},
            {"check_interval_seconds": True},
            {"check_interval_seconds": 100_000},
        ):
            with self.subTest(raw=raw), self.assertRaises(UpdatesPolicyError):
                parse_updates_config(raw)

    def test_release_tags_are_strict_semver(self) -> None:
        self.assertEqual(parse_release_tag("v1.2.3"), (1, 2, 3))
        for tag in ("1.2.3", "v1.2", "v1.2.3-rc1", "v1.2.3.4", None, "latest"):
            with self.subTest(tag=tag):
                self.assertIsNone(parse_release_tag(tag))


class UpdateManagerTests(unittest.TestCase):
    def manager(
        self,
        *,
        mode: str = "self-update",
        fetch: _Fetcher | None = None,
        runner: _Runner | None = None,
        restart=None,
        current: str = "1.0.0",
    ) -> tuple[UpdateManager, _Fetcher, _Runner]:
        fetch = fetch or _Fetcher()
        runner = runner or _Runner()
        manager = UpdateManager(
            UpdatesConfig(mode=mode),
            current_version=current,
            restart=restart,
            fetch=fetch,
            run_process=runner,
            clock=lambda: "2026-08-25T00:00:00Z",
        )
        return manager, fetch, runner

    def wait_for_state(self, manager: UpdateManager, *states: str) -> dict[str, object]:
        for _ in range(200):
            status = manager.status()
            if status["state"] in states:
                return status
            threading.Event().wait(0.01)
        self.fail(f"manager never reached {states}: {manager.status()}")

    def test_check_now_reports_a_newer_release(self) -> None:
        manager, _, _ = self.manager(mode="check")
        manager.check_now()
        status = manager.status()
        self.assertEqual(status["latestVersion"], "9.9.9")
        self.assertTrue(status["updateAvailable"])
        self.assertEqual(status["checkedAt"], "2026-08-25T00:00:00Z")

    def test_current_release_is_not_an_update(self) -> None:
        manager, _, _ = self.manager(mode="check", current="9.9.9")
        manager.check_now()
        self.assertFalse(manager.status()["updateAvailable"])

    def test_check_failure_keeps_last_answer_and_reports_detail(self) -> None:
        calls = {"n": 0}

        def flaky(url: str, limit: int) -> bytes:
            calls["n"] += 1
            if calls["n"] == 1:
                return _Fetcher()(url, limit)
            raise OSError("network unreachable")

        manager, _, _ = self.manager(mode="check", fetch=flaky)  # type: ignore[arg-type]
        manager.check_now()
        manager.check_now()
        status = manager.status()
        self.assertEqual(status["latestVersion"], "9.9.9")
        self.assertIn("release check failed", str(status["detail"]))

    def test_apply_requires_mode_restart_and_availability(self) -> None:
        checkonly, _, _ = self.manager(mode="check", restart=lambda: None)
        self.assertEqual(checkonly.apply(), (False, "self-update is not enabled"))

        unmanaged, _, _ = self.manager(restart=None)
        self.assertFalse(unmanaged.apply()[0])

        stale, _, _ = self.manager(restart=lambda: None, current="9.9.9")
        stale.check_now()
        self.assertEqual(stale.apply(), (False, "no newer release is available"))

    def test_apply_downloads_verifies_installs_and_restarts(self) -> None:
        restarted = threading.Event()
        manager, fetch, runner = self.manager(restart=restarted.set)
        manager.check_now()
        accepted, message = manager.apply()
        self.assertTrue(accepted, message)

        status = self.wait_for_state(manager, "restarting", "failed")
        self.assertEqual(status["state"], "restarting", status["detail"])
        self.assertTrue(restarted.wait(1))
        install = next(c for c in runner.commands if "install" in c)
        self.assertIn("--no-deps", install)
        self.assertIn("--force-reinstall", install)
        self.assertTrue(install[-1].endswith("mocop-9.9.9-py3-none-any.whl"))
        self.assertTrue(any(url.endswith(".whl") for url in fetch.calls))

    def test_apply_rejects_a_tampered_wheel_without_restarting(self) -> None:
        fetch = _Fetcher()
        original = fetch.__call__

        def tampered(url: str, limit: int) -> bytes:
            payload = original(url, limit)
            return b"evil" if url.endswith(".whl") else payload

        restarted = threading.Event()
        manager, _, runner = self.manager(fetch=tampered, restart=restarted.set)  # type: ignore[arg-type]
        manager.check_now()
        self.assertTrue(manager.apply()[0])

        status = self.wait_for_state(manager, "failed", "restarting")
        self.assertEqual(status["state"], "failed")
        self.assertIn("digest does not match", str(status["detail"]))
        self.assertFalse(restarted.is_set())
        self.assertFalse(any("install" in c for c in runner.commands))

    def test_failed_verification_never_restarts(self) -> None:
        restarted = threading.Event()
        manager, _, _ = self.manager(
            runner=_Runner(installed_version="1.0.0"), restart=restarted.set
        )
        manager.check_now()
        self.assertTrue(manager.apply()[0])

        status = self.wait_for_state(manager, "failed", "restarting")
        self.assertEqual(status["state"], "failed")
        self.assertIn("does not report the new version", str(status["detail"]))
        self.assertFalse(restarted.is_set())

    def test_installs_through_uv_when_the_environment_has_no_pip(self) -> None:
        # uv tool environments ship without pip, so this is the path a managed
        # `uv tool install` deployment takes; the command must name the wheel
        # through --from and keep the tool name mocop.
        restarted = threading.Event()
        runner = _Runner(pip=False)
        manager, _, _ = self.manager(runner=runner, restart=restarted.set)
        manager.check_now()
        with patch("mocop.updates._uv_executable", return_value="/opt/uv/bin/uv"):
            self.assertTrue(manager.apply()[0])
            status = self.wait_for_state(manager, "restarting", "failed")
        self.assertEqual(status["state"], "restarting", status["detail"])
        install = next(c for c in runner.commands if "install" in c)
        self.assertEqual(
            install[:5], ["/opt/uv/bin/uv", "tool", "install", "--force", "--from"]
        )
        self.assertTrue(install[5].endswith("mocop-9.9.9-py3-none-any.whl"))
        self.assertEqual(install[6], "mocop")
        self.assertTrue(restarted.wait(1))

    def test_missing_toolchain_and_failed_installs_report_recovery(self) -> None:
        cases = (
            (_Runner(pip=False), None, "neither pip nor uv is available"),
            (
                _Runner(pip=False, install_stderr="uv: boom"),
                "/opt/uv/bin/uv",
                "uv tool install failed: uv: boom",
            ),
            (
                _Runner(install_stderr="pip: boom"),
                None,
                "pip install failed: pip: boom",
            ),
        )
        for runner, uv, expected in cases:
            with self.subTest(expected=expected):
                restarted = threading.Event()
                manager, _, _ = self.manager(runner=runner, restart=restarted.set)
                manager.check_now()
                with patch("mocop.updates._uv_executable", return_value=uv):
                    self.assertTrue(manager.apply()[0])
                    status = self.wait_for_state(manager, "failed", "restarting")
                self.assertEqual(status["state"], "failed")
                self.assertIn(expected, str(status["detail"]))
                self.assertIn(
                    "uv tool install --force --from git+https://github.com/ChangWinde/mocop.git@<tag> mocop",
                    str(status["detail"]),
                )
                self.assertFalse(restarted.is_set())

    def test_apply_refuses_releases_without_a_verifiable_wheel(self) -> None:
        # No SHA256SUMS asset, then a manifest that names another file: neither
        # may install anything or restart.
        def without_sums(url: str, limit: int) -> bytes:
            if url == _RELEASE_URL:
                payload = release_payload("v9.9.9", b"wheel-bytes", "9.9.9")
                payload["assets"] = payload["assets"][:1]
                return json.dumps(payload).encode()
            return _Fetcher()(url, limit)

        def wrong_sums(url: str, limit: int) -> bytes:
            if url.endswith("SHA256SUMS"):
                return b"0" * 64 + b"  mocop-8.8.8-py3-none-any.whl\n"
            return _Fetcher()(url, limit)

        for fetch, expected in (
            (without_sums, "does not ship a verifiable wheel"),
            (wrong_sums, "SHA256SUMS does not name the release wheel"),
        ):
            with self.subTest(expected=expected):
                restarted = threading.Event()
                manager, _, runner = self.manager(fetch=fetch, restart=restarted.set)  # type: ignore[arg-type]
                manager.check_now()
                self.assertTrue(manager.apply()[0])
                status = self.wait_for_state(manager, "failed", "restarting")
                self.assertIn(expected, str(status["detail"]))
                self.assertFalse(any("install" in c for c in runner.commands))
                self.assertFalse(restarted.is_set())

    def test_check_rejects_untagged_releases_and_clears_a_superseded_failure(
        self,
    ) -> None:
        def untagged(url: str, limit: int) -> bytes:
            if url == _RELEASE_URL:
                return json.dumps({"tag_name": "latest", "assets": []}).encode()
            return _Fetcher()(url, limit)

        manager, _, _ = self.manager(mode="check", fetch=untagged)  # type: ignore[arg-type]
        manager.check_now()
        status = manager.status()
        self.assertIsNone(status["latestVersion"])
        self.assertIn("does not carry a v<semver> tag", str(status["detail"]))

        # A failed attempt at one release is forgotten once a newer one shows up.
        fetcher = _Fetcher(version="9.9.9")
        failing, _, _ = self.manager(
            fetch=fetcher,
            runner=_Runner(installed_version="1.0.0"),
            restart=lambda: None,
        )
        failing.check_now()
        self.assertTrue(failing.apply()[0])
        self.assertEqual(self.wait_for_state(failing, "failed")["state"], "failed")
        fetcher.version = "9.9.10"
        failing.check_now()
        status = failing.status()
        self.assertEqual((status["state"], status["detail"]), ("idle", None))
        self.assertEqual(status["latestVersion"], "9.9.10")

    def test_poll_thread_checks_once_at_start_and_stops_on_request(self) -> None:
        manager, fetch, _ = self.manager(mode="check")
        manager.start()
        manager.start()  # idempotent
        for _ in range(200):
            if manager.status()["checkedAt"] is not None:
                break
            threading.Event().wait(0.01)
        self.assertEqual(manager.status()["latestVersion"], "9.9.9")
        manager.stop()
        assert manager._poll_thread is not None
        manager._poll_thread.join(timeout=2)
        self.assertFalse(manager._poll_thread.is_alive())
        self.assertEqual(fetch.calls.count(_RELEASE_URL), 1)

        disabled, _, _ = self.manager(mode="off")
        disabled.start()
        self.assertIsNone(disabled._poll_thread)

    def test_apply_is_single_flight(self) -> None:
        block = threading.Event()

        def slow_fetch(url: str, limit: int) -> bytes:
            if url.endswith(".whl"):
                block.wait(2)
            return _Fetcher()(url, limit)

        manager, _, _ = self.manager(fetch=slow_fetch, restart=lambda: None)  # type: ignore[arg-type]
        manager.check_now()
        self.assertTrue(manager.apply()[0])
        self.assertEqual(manager.apply(), (False, "an update is already in progress"))
        block.set()
        self.wait_for_state(manager, "restarting", "failed")


if __name__ == "__main__":
    unittest.main()
