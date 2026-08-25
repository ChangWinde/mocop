from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mocop.lifecycle import (
    LifecycleError,
    UserServiceManager,
    ensure_access_token,
    initialize_config,
    read_access_token,
    render_user_unit,
    user_unit_path,
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

    def test_init_can_generate_the_fresh_host_deployment_profile(self) -> None:
        path = self.root / "deploy" / "config.json"

        initialize_config(
            path,
            ("gpu-01", "monitor-01"),
            local_host="monitor-01",
            display_name="console-0",
            ssh_config="~/.ssh/fleet-config",
            auto_discover=True,
        )

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["hosts"], ["monitor-01", "gpu-01"])
        self.assertEqual(data["local_host"], "monitor-01")
        self.assertEqual(
            data["host_overrides"],
            {"monitor-01": {"display_name": "console-0"}},
        )
        self.assertEqual(data["ssh_config"], "~/.ssh/fleet-config")
        self.assertTrue(data["auto_discover"])
        self.assertEqual(data["ssh_discovery"]["mode"], "topology")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_access_token_is_private_stable_and_rejects_symlinks(self) -> None:
        config_path = self.root / "private" / "config.json"
        initialize_config(config_path, ())

        token_path = ensure_access_token(config_path)
        first = read_access_token(token_path)
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(ensure_access_token(config_path), token_path)
        self.assertEqual(read_access_token(token_path), first)

        target = self.root / "token-target"
        target.write_text("B" * 43, encoding="ascii")
        token_path.unlink()
        token_path.symlink_to(target)
        with self.assertRaisesRegex(LifecycleError, "private file"):
            read_access_token(token_path)

    def test_user_unit_path_is_canonical_across_shell_xdg_overrides(self) -> None:
        self.assertEqual(
            user_unit_path({"XDG_CONFIG_HOME": str(self.root / "shadow")}),
            Path.home() / ".config" / "systemd" / "user" / "mocop.service",
        )

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
            f'EnvironmentFile=-"{self.root}/environment"',
            unit,
        )
        self.assertNotIn("ProtectSystem=", unit)
        self.assertIn("StateDirectory=mocop", unit)
        self.assertIn("StateDirectoryMode=0700", unit)
        self.assertNotIn("ReadWritePaths=", unit)
        self.assertNotIn("PrivateTmp=", unit)
        self.assertIn("--access-token-file=", unit)
        self.assertIn("UMask=0077", unit)

        with self.assertRaisesRegex(LifecycleError, "valid UTF-8"):
            render_user_unit(Path("/usr/bin/python3"), Path("/tmp/bad\nconfig"))
        with self.assertRaisesRegex(LifecycleError, "valid UTF-8"):
            render_user_unit(Path("/usr/bin/python3"), Path("/tmp/\udcff/config.json"))

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

    def test_optional_environment_path_quotes_spaces_without_hiding_prefix(
        self,
    ) -> None:
        # systemd interprets C-style escapes only inside quotes; an unquoted
        # \x20 path names a nonexistent literal-backslash file and the "-"
        # prefix then silently drops the webhook environment.
        unit = render_user_unit(
            Path("/usr/bin/python3"),
            self.root / "config directory" / "config.json",
        )

        self.assertIn(
            f'EnvironmentFile=-"{self.root}/config directory/environment"',
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
                (
                    "systemctl",
                    "--user",
                    "is-enabled",
                    "--quiet",
                    "mocop.service",
                ),
                (
                    "systemctl",
                    "--user",
                    "is-active",
                    "--quiet",
                    "mocop.service",
                ),
                ("systemctl", "--user", "daemon-reload"),
                ("systemctl", "--user", "enable", str(unit_path)),
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

    def test_install_restores_existing_unit_after_each_systemctl_failure(self) -> None:
        for failing_action in ("daemon-reload", "enable", "restart"):
            with self.subTest(failing_action=failing_action):
                root = self.root / failing_action
                root.mkdir(mode=0o700)
                config_path = root / "config.json"
                initialize_config(config_path, ("gpu-01",))
                unit_path = root / "mocop.service"
                unit_path.write_text("OLD-WORKING-UNIT", encoding="utf-8")

                failures = 0

                def run(
                    arguments: tuple[str, ...], failure: str = failing_action
                ) -> int:
                    nonlocal failures
                    if arguments[2] in {"is-enabled", "is-active"}:
                        return 1
                    if arguments[2] == failure and failures == 0:
                        failures += 1
                        return 1
                    return 0

                manager = UserServiceManager(
                    config_path=config_path,
                    unit_path=unit_path,
                    python_executable=Path("/usr/bin/python3"),
                    run=run,
                )

                with self.assertRaisesRegex(LifecycleError, "command failed"):
                    manager.install()
                self.assertEqual(
                    unit_path.read_text(encoding="utf-8"), "OLD-WORKING-UNIT"
                )

    def test_install_reports_an_incomplete_rollback(self) -> None:
        config_path = self.root / "config.json"
        initialize_config(config_path, ("gpu-01",))
        unit_path = self.root / "mocop.service"
        unit_path.write_text("OLD-WORKING-UNIT", encoding="utf-8")
        reloads = 0

        def run(arguments: tuple[str, ...]) -> int:
            nonlocal reloads
            action = arguments[2]
            if action in {"is-enabled", "is-active"}:
                return 1
            if action == "daemon-reload":
                reloads += 1
                return 1 if reloads == 2 else 0
            return 1 if action == "enable" else 0

        manager = UserServiceManager(
            config_path=config_path,
            unit_path=unit_path,
            python_executable=Path("/usr/bin/python3"),
            run=run,
        )

        with self.assertRaisesRegex(LifecycleError, "rollback is incomplete"):
            manager.install()
        self.assertEqual(unit_path.read_text(encoding="utf-8"), "OLD-WORKING-UNIT")

    def test_preflight_state_failure_does_not_mutate_the_old_service(self) -> None:
        for failing_action in ("is-enabled", "is-active"):
            with self.subTest(failing_action=failing_action):
                root = self.root / f"preflight-{failing_action}"
                root.mkdir(mode=0o700)
                root.chmod(0o700)
                config_path = root / "config.json"
                initialize_config(config_path, ("gpu-01",))
                unit_path = root / "mocop.service"
                unit_path.write_text("OLD-WORKING-UNIT", encoding="utf-8")
                calls = []

                def run(
                    arguments: tuple[str, ...],
                    failure: str = failing_action,
                    observed: list[str] = calls,
                ) -> int:
                    action = arguments[2]
                    observed.append(action)
                    if action == failure:
                        raise RuntimeError("systemd unavailable")
                    return 0

                manager = UserServiceManager(
                    config_path=config_path,
                    unit_path=unit_path,
                    python_executable=Path("/usr/bin/python3"),
                    run=run,
                )

                with self.assertRaisesRegex(RuntimeError, "systemd unavailable"):
                    manager.install()
                self.assertEqual(
                    unit_path.read_text(encoding="utf-8"), "OLD-WORKING-UNIT"
                )
                self.assertNotIn("daemon-reload", calls)
                self.assertNotIn("disable", calls)
                self.assertNotIn("stop", calls)

    def test_install_transactions_are_serialized_across_managers(self) -> None:
        config_path = self.root / "config.json"
        initialize_config(config_path, ("gpu-01",))
        unit_path = self.root / "mocop.service"
        first = UserServiceManager(
            config_path=config_path,
            unit_path=unit_path,
            python_executable=Path("/usr/bin/python3"),
            run=lambda _arguments: 0,
        )
        second = UserServiceManager(
            config_path=config_path,
            unit_path=unit_path,
            python_executable=Path("/usr/bin/python3"),
            run=lambda _arguments: 0,
        )
        first.install()
        finished = threading.Event()

        def install_second() -> None:
            second.install()
            second.commit_install()
            finished.set()

        contender = threading.Thread(target=install_second)
        contender.start()
        time.sleep(0.05)
        self.assertFalse(finished.is_set())
        first.commit_install()
        contender.join(1)
        self.assertTrue(finished.is_set())

    def test_uninstall_is_idempotent_after_its_unit_is_removed(self) -> None:
        calls = 0

        def run(arguments: tuple[str, ...]) -> int:
            nonlocal calls
            if arguments[2] == "disable":
                calls += 1
                return 0 if calls == 1 else 1
            return 0

        manager = self.build_manager(run)
        manager.unit_path.write_text("unit", encoding="utf-8")

        manager.uninstall()
        manager.uninstall()

        self.assertFalse(manager.unit_path.exists())

    def test_install_and_uninstall_replace_a_unit_symlink_not_its_target(self) -> None:
        config_path = self.root / "config.json"
        initialize_config(config_path, ("gpu-01",))
        valuable = self.root / "valuable.txt"
        valuable.write_text("preserve me", encoding="utf-8")
        unit_path = self.root / "systemd" / "user" / "mocop.service"
        unit_path.parent.mkdir(parents=True)
        unit_path.parent.chmod(0o700)
        unit_path.symlink_to(valuable)
        manager = UserServiceManager(
            config_path=config_path,
            unit_path=unit_path,
            python_executable=Path("/usr/bin/python3"),
            run=lambda _arguments: 0,
        )

        manager.install()
        self.assertFalse(unit_path.is_symlink())
        self.assertEqual(valuable.read_text(encoding="utf-8"), "preserve me")

        manager.uninstall()
        self.assertFalse(unit_path.exists())
        self.assertEqual(valuable.read_text(encoding="utf-8"), "preserve me")

    def test_service_install_rejects_a_config_symlink(self) -> None:
        target = self.root / "real-config.json"
        initialize_config(target, ("gpu-01",))
        link = self.root / "config.json"
        link.symlink_to(target)
        manager = UserServiceManager(
            config_path=link,
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

    def test_wait_until_healthy_requires_an_authenticated_snapshot(self) -> None:
        manager = self.build_manager(lambda _arguments: 0)
        connection = Mock()
        meta = Mock(status=200)
        meta.read.return_value = b'{"apiVersion":"2","authenticationRequired":true}'
        protected = Mock(status=200)
        protected.read.return_value = b""
        connection.getresponse.side_effect = (meta, protected)
        with patch(
            "mocop.lifecycle.http.client.HTTPConnection", return_value=connection
        ):
            self.assertTrue(
                manager.wait_until_healthy("0.0.0.0", 8787, "A" * 43, timeout_seconds=0)
            )
        self.assertEqual(connection.request.call_count, 2)
        connection.request.assert_called_with(
            "HEAD",
            "/api/snapshot",
            headers={"Authorization": f"Bearer {'A' * 43}"},
        )

    def test_wait_until_healthy_rejects_a_wrong_capability(self) -> None:
        manager = self.build_manager(lambda _arguments: 0)
        connection = Mock()
        meta = Mock(status=200)
        meta.read.return_value = b'{"apiVersion":"2","authenticationRequired":true}'
        protected = Mock(status=403)
        protected.read.return_value = b""
        connection.getresponse.side_effect = (meta, protected)
        with patch(
            "mocop.lifecycle.http.client.HTTPConnection", return_value=connection
        ):
            self.assertFalse(
                manager.wait_until_healthy(
                    "127.0.0.1", 8787, "wrong", timeout_seconds=0
                )
            )

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
