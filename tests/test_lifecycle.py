from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mocop.lifecycle import (
    LifecycleError,
    UserServiceManager,
    initialize_config,
    render_user_unit,
)


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_init_writes_a_private_valid_whitelist_config(self) -> None:
        path = self.root / "config" / "config.json"

        initialize_config(path, ("gpu-01", "gpu-02", "gpu-01"))

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(data["auto_discover"])
        self.assertEqual(data["hosts"], ["gpu-01", "gpu-02"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_init_refuses_overwrite_and_unsafe_aliases(self) -> None:
        path = self.root / "config.json"
        initialize_config(path, ())

        with self.assertRaisesRegex(LifecycleError, "already exists"):
            initialize_config(path, ("gpu-01",))

        unsafe_path = self.root / "unsafe.json"
        with self.assertRaisesRegex(LifecycleError, "invalid SSH host alias"):
            initialize_config(unsafe_path, ("--proxy-command=bad",))
        self.assertFalse(unsafe_path.exists())

    def test_unit_rendering_is_hardened_and_rejects_control_characters(self) -> None:
        unit = render_user_unit(
            Path("/opt/mocop env/bin/python"),
            self.root / "config file.json",
        )

        self.assertIn('ExecStart="/opt/mocop env/bin/python" -m mocop', unit)
        self.assertIn(" -m mocop --managed-service ", unit)
        self.assertIn(f'--config="{self.root}/config file.json"', unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn(
            f"EnvironmentFile=-{self.root}/environment",
            unit,
        )
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("StateDirectory=mocop", unit)
        self.assertIn("StateDirectoryMode=0700", unit)
        self.assertIn(
            f'ReadWritePaths="{self.root}"',
            unit,
        )
        self.assertIn("UMask=0077", unit)

        with self.assertRaisesRegex(LifecycleError, "control characters"):
            render_user_unit(Path("/usr/bin/python3"), Path("/tmp/bad\nconfig"))

    def test_unit_preserves_virtual_environment_interpreter_symlink(self) -> None:
        base_python = self.root / "managed" / "python3"
        base_python.parent.mkdir()
        base_python.touch(mode=0o755)
        venv_python = self.root / "tool" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.symlink_to(base_python)

        unit = render_user_unit(venv_python, self.root / "config.json")

        self.assertIn(f'ExecStart="{venv_python}" -m mocop', unit)
        self.assertNotIn(f'ExecStart="{base_python}"', unit)

    def test_optional_environment_path_escapes_spaces_without_hiding_prefix(
        self,
    ) -> None:
        unit = render_user_unit(
            Path("/usr/bin/python3"),
            self.root / "config directory" / "config.json",
        )

        self.assertIn(
            f"EnvironmentFile=-{self.root}/config\\x20directory/environment",
            unit,
        )

    def test_service_install_uses_fixed_systemctl_arguments(self) -> None:
        config_path = self.root / "config.json"
        initialize_config(config_path, ("gpu-01",))
        unit_path = self.root / "systemd" / "user" / "mocop.service"
        calls: list[tuple[str, ...]] = []

        def run(arguments: tuple[str, ...]) -> int:
            calls.append(arguments)
            return 0

        manager = UserServiceManager(
            config_path=config_path,
            unit_path=unit_path,
            python_executable=Path("/usr/bin/python3"),
            run=run,
        )
        manager.install()

        self.assertTrue(unit_path.is_file())
        self.assertEqual(unit_path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(
            calls,
            [
                ("systemctl", "--user", "daemon-reload"),
                ("systemctl", "--user", "enable", "mocop.service"),
                ("systemctl", "--user", "restart", "mocop.service"),
            ],
        )

    def test_service_install_requires_a_loadable_config(self) -> None:
        manager = UserServiceManager(
            config_path=self.root / "missing.json",
            unit_path=self.root / "mocop.service",
            python_executable=Path("/usr/bin/python3"),
            run=lambda _arguments: 0,
        )

        with self.assertRaisesRegex(LifecycleError, "configuration is not ready"):
            manager.install()
        self.assertFalse(manager.unit_path.exists())

    def test_service_install_rejects_an_exposed_secret_environment(self) -> None:
        config_path = self.root / "config.json"
        initialize_config(config_path, ("gpu-01",))
        environment = self.root / "environment"
        environment.write_text("MOCOP_WEBHOOK_SECRET=secret\n", encoding="utf-8")
        environment.chmod(0o644)
        manager = UserServiceManager(
            config_path=config_path,
            unit_path=self.root / "mocop.service",
            python_executable=Path("/usr/bin/python3"),
            run=lambda _arguments: 0,
        )

        with self.assertRaisesRegex(LifecycleError, "private regular file"):
            manager.install()

        self.assertFalse(manager.unit_path.exists())

    def build_manager(self, run) -> UserServiceManager:
        config_path = self.root / "config.json"
        initialize_config(config_path, ("gpu-01",))
        return UserServiceManager(
            config_path=config_path,
            unit_path=self.root / "mocop.service",
            python_executable=Path("/usr/bin/python3"),
            run=run,
        )

    def test_wait_until_active_polls_is_active_through_the_runner(self) -> None:
        calls: list[tuple[str, ...]] = []
        responses = iter((3, 3, 0))

        def run(arguments: tuple[str, ...]) -> int:
            calls.append(arguments)
            return next(responses)

        sleeps: list[float] = []
        manager = self.build_manager(run)

        active = manager.wait_until_active(
            timeout_seconds=5.0,
            poll_interval_seconds=0.5,
            sleep=sleeps.append,
            clock=lambda: 0.5 * len(sleeps),
        )

        self.assertTrue(active)
        # One settle delay precedes every check so an immediately crashing
        # unit is never reported active.
        self.assertEqual(sleeps, [0.5, 0.5, 0.5])
        self.assertEqual(
            calls,
            [("systemctl", "--user", "is-active", "--quiet", "mocop.service")] * 3,
        )

    def test_wait_until_active_gives_up_at_the_deadline(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(arguments: tuple[str, ...]) -> int:
            calls.append(arguments)
            return 3

        sleeps: list[float] = []
        manager = self.build_manager(run)

        active = manager.wait_until_active(
            timeout_seconds=5.0,
            poll_interval_seconds=0.5,
            sleep=sleeps.append,
            clock=lambda: 0.5 * len(sleeps),
        )

        self.assertFalse(active)
        self.assertEqual(len(calls), 10)

    def test_status_is_read_only_and_uninstall_removes_only_its_unit(self) -> None:
        config_path = self.root / "config.json"
        initialize_config(config_path, ())
        unit_path = self.root / "mocop.service"
        unit_path.write_text("unit", encoding="utf-8")
        sibling = self.root / "keep.service"
        sibling.write_text("keep", encoding="utf-8")
        calls: list[tuple[str, ...]] = []

        def run(arguments: tuple[str, ...]) -> int:
            calls.append(arguments)
            return 0

        manager = UserServiceManager(
            config_path=config_path,
            unit_path=unit_path,
            python_executable=Path("/usr/bin/python3"),
            run=run,
        )

        self.assertEqual(manager.status(), 0)
        self.assertTrue(unit_path.exists())
        manager.uninstall()

        self.assertFalse(unit_path.exists())
        self.assertTrue(sibling.exists())
        self.assertEqual(
            calls,
            [
                ("systemctl", "--user", "status", "--no-pager", "mocop.service"),
                ("systemctl", "--user", "disable", "--now", "mocop.service"),
                ("systemctl", "--user", "daemon-reload"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
