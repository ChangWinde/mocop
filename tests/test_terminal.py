from __future__ import annotations

import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mocop.api_manifest import ERROR_CODES
from mocop.terminal import (
    REMOTE_COMMAND,
    RESPONSE_CODES,
    TerminalApp,
    TerminalLauncher,
    installed_apps,
)

GHOSTTY = TerminalApp("ghostty", "Ghostty", Path("/Applications/Ghostty.app"))
TERMINAL = TerminalApp(
    "terminal", "Terminal.app", Path("/System/Applications/Utilities/Terminal.app")
)


class _Runner:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.commands: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.error: BaseException | None = None

    def __call__(self, command):
        self.commands.append(list(command))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            list(command), self.returncode, stdout="", stderr=self.stderr
        )


class TerminalLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mocop-terminal-"))
        self.addCleanup(self._remove_directory)
        self.runner = _Runner()
        self.clock = [100.0]
        self.hosts = {"gpu-01", "gpu-02", "workstation"}
        self.launcher = TerminalLauncher(
            lambda: self.hosts,
            Path("/home/operator/.ssh/config"),
            apps=(GHOSTTY, TERMINAL),
            local_host="workstation",
            script_dirs=(self.directory,),
            runner=self.runner,
            clock=lambda: self.clock[0],
        )

    def _remove_directory(self) -> None:
        for child in self.directory.iterdir():
            child.unlink()
        self.directory.rmdir()

    def test_needs_at_least_one_application(self) -> None:
        with self.assertRaises(ValueError):
            TerminalLauncher(lambda: (), Path("/x"), apps=())

    def test_describes_apps_in_preference_order(self) -> None:
        self.assertEqual(
            self.launcher.apps,
            (
                {"id": "ghostty", "label": "Ghostty"},
                {"id": "terminal", "label": "Terminal.app"},
            ),
        )

    def test_opens_the_preferred_app_with_a_private_launch_script(self) -> None:
        result = self.launcher.open("gpu-01")

        self.assertEqual(
            result,
            {"status": "opened", "accepted": True, "host": "gpu-01", "app": "ghostty"},
        )
        script = self.directory / "gpu-01.command"
        self.assertTrue(script.is_file())
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        (command,) = self.runner.commands
        self.assertEqual(
            command[:4], ["/usr/bin/open", "-n", "-a", str(GHOSTTY.bundle)]
        )
        self.assertIn("--args", command)
        self.assertIn("--window-save-state=never", command)
        self.assertIn("--quit-after-last-window-closed=true", command)
        self.assertEqual(command[-1], f"--command={script}")

        text = script.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("printf '\\033]0;%s\\007' gpu-01\n", text)
        expected = shlex.join(
            [
                "ssh",
                "-F",
                "/home/operator/.ssh/config",
                "-t",
                "--",
                "gpu-01",
                REMOTE_COMMAND,
            ]
        )
        self.assertIn(f"exec {expected}\n", text)
        self.assertIn("df -h", text)

    def test_terminal_app_opens_the_script_as_a_document(self) -> None:
        result = self.launcher.open("gpu-02", "terminal")

        self.assertEqual(result["status"], "opened")
        self.assertEqual(result["app"], "terminal")
        (command,) = self.runner.commands
        self.assertEqual(
            command,
            [
                "/usr/bin/open",
                "-a",
                str(TERMINAL.bundle),
                str(self.directory / "gpu-02.command"),
            ],
        )

    def test_default_ssh_config_is_left_implicit(self) -> None:
        launcher = TerminalLauncher(
            lambda: self.hosts,
            Path("~/.ssh/config").expanduser(),
            apps=(TERMINAL,),
            script_dirs=(self.directory,),
            runner=self.runner,
        )
        text = launcher.script_text("gpu-01")
        self.assertIn("exec ssh -t -- gpu-01 ", text)
        self.assertNotIn(" -F ", text)

    def test_local_host_runs_the_session_without_ssh(self) -> None:
        text = self.launcher.script_text("workstation")
        self.assertNotIn("ssh", text)
        self.assertTrue(text.rstrip("\n").endswith(REMOTE_COMMAND))

    def test_rejects_unknown_hosts_apps_and_unsafe_aliases(self) -> None:
        self.assertEqual(self.launcher.open("gpu-99")["status"], "unknown_host")
        self.assertEqual(self.launcher.open("../etc")["status"], "unknown_host")
        self.assertEqual(self.launcher.open("gpu-01", "iterm")["status"], "unknown_app")
        self.assertEqual(self.runner.commands, [])
        self.assertFalse(list(self.directory.iterdir()))

    def test_empty_app_selects_the_preferred_application(self) -> None:
        # The manifest requires the field, so the dashboard sends "" for the default.
        result = self.launcher.open("gpu-01", "")
        self.assertEqual(result["status"], "opened")
        self.assertEqual(result["app"], "ghostty")

    def test_repeated_clicks_are_rate_limited_per_host(self) -> None:
        self.assertEqual(self.launcher.open("gpu-01")["status"], "opened")
        limited = self.launcher.open("gpu-01")
        self.assertEqual(limited["status"], "rate_limited")
        self.assertGreater(limited["retryAfterSeconds"], 0)
        self.assertEqual(self.launcher.open("gpu-02")["status"], "opened")
        self.clock[0] += 5
        self.assertEqual(self.launcher.open("gpu-01")["status"], "opened")
        self.assertEqual(len(self.runner.commands), 3)

    def test_open_failures_are_reported_without_raising(self) -> None:
        self.runner.returncode = 1
        self.runner.stderr = "Unable to find application named 'Ghostty'\n  extra\n"
        failed = self.launcher.open("gpu-01")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            failed["detail"], "Unable to find application named 'Ghostty' extra"
        )

        self.clock[0] += 5
        self.runner.error = subprocess.TimeoutExpired(["open"], 15)
        timed_out = self.launcher.open("gpu-01")
        self.assertEqual(timed_out["status"], "failed")
        self.assertIn("timed out", timed_out["detail"])

    def test_refuses_shared_or_foreign_script_directories(self) -> None:
        foreign = self.directory / "shared"
        foreign.mkdir(mode=0o777)
        os.chmod(foreign, 0o777)
        launcher = TerminalLauncher(
            lambda: self.hosts,
            Path("/home/operator/.ssh/config"),
            apps=(TERMINAL,),
            script_dirs=(foreign,),
            runner=self.runner,
        )
        # A world-writable directory we own is tightened rather than refused.
        self.assertEqual(launcher.open("gpu-01")["status"], "opened")
        self.assertEqual(stat.S_IMODE(foreign.stat().st_mode), 0o700)
        (foreign / "gpu-01.command").unlink()
        foreign.rmdir()

        unsafe = TerminalLauncher(
            lambda: self.hosts,
            Path("/home/operator/.ssh/config"),
            apps=(TERMINAL,),
            script_dirs=(self.directory / "has space",),
            runner=self.runner,
        )
        result = unsafe.open("gpu-01")
        self.assertEqual(result["status"], "failed")
        self.assertIn("no private directory", result["detail"])

    def test_response_codes_cover_every_non_success_status(self) -> None:
        self.assertEqual(
            set(RESPONSE_CODES),
            {"unknown_host", "unknown_app", "rate_limited", "failed"},
        )
        catalog = dict(ERROR_CODES)
        for status, code in RESPONSE_CODES.values():
            self.assertGreaterEqual(status, 400)
            # Every code the launcher can surface is published by /api/meta.
            self.assertEqual(catalog[code], int(status))


class DetectionTests(unittest.TestCase):
    def test_installed_apps_only_lists_existing_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            present = Path(root) / "Present.app"
            present.mkdir()
            candidates = (
                ("present", "Present", (str(Path(root) / "missing.app"), str(present))),
                ("absent", "Absent", (str(Path(root) / "nope.app"),)),
            )
            apps = installed_apps(candidates)
        self.assertEqual([app.id for app in apps], ["present"])
        self.assertEqual(apps[0].bundle, present)

    def test_detect_returns_none_off_macos_or_without_apps(self) -> None:
        with patch("mocop.terminal.sys.platform", "linux"):
            self.assertIsNone(TerminalLauncher.detect(lambda: (), Path("/x")))
        with (
            patch("mocop.terminal.sys.platform", "darwin"),
            patch("mocop.terminal.installed_apps", return_value=()),
        ):
            self.assertIsNone(TerminalLauncher.detect(lambda: (), Path("/x")))
        with (
            patch("mocop.terminal.sys.platform", "darwin"),
            patch("mocop.terminal.installed_apps", return_value=(TERMINAL,)),
        ):
            launcher = TerminalLauncher.detect(lambda: (), Path("/x"))
        self.assertIsNotNone(launcher)
        self.assertEqual(launcher.apps, ({"id": "terminal", "label": "Terminal.app"},))


if __name__ == "__main__":
    unittest.main()
