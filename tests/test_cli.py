from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mocop.__main__ import _arguments, main
from mocop.lifecycle import LifecycleError


def write_config(path: Path, **overrides: object) -> Path:
    """Write a minimal valid monitor configuration for CLI-level tests."""
    data: dict[str, object] = {
        "ssh_config": str(path.parent / "ssh-config"),
        "auto_discover": False,
        "hosts": ["gpu-1", "gpu-2"],
        "exclude_hosts": [],
        "poll_interval_seconds": 5,
        "probe_timeout_seconds": 12,
        "connect_timeout_seconds": 5,
        "max_workers": 2,
        "listen_host": "127.0.0.1",
        "listen_port": 8787,
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_monitor_mode_remains_the_default(self) -> None:
        args = _arguments(["--config", "/tmp/config.json", "--once"])

        self.assertIsNone(args.command)
        self.assertEqual(args.config, Path("/tmp/config.json"))
        self.assertTrue(args.once)

    def test_managed_service_mode_is_explicit(self) -> None:
        args = _arguments(["--managed-service"])

        self.assertTrue(args.managed_service)

    def test_init_and_service_commands_are_unambiguous(self) -> None:
        init_args = _arguments(["init", "--host", "gpu-01", "--host", "gpu-02"])
        install_args = _arguments(["service", "install", "--config", "/tmp/c.json"])

        self.assertEqual(init_args.command, "init")
        self.assertEqual(init_args.hosts, ["gpu-01", "gpu-02"])
        self.assertEqual(install_args.command, "service")
        self.assertEqual(install_args.action, "install")

    def test_config_check_and_doctor_probe_are_parsed(self) -> None:
        check_args = _arguments(["config", "check", "--config", "/tmp/c.json"])
        doctor_args = _arguments(["doctor", "--probe"])

        self.assertEqual(check_args.command, "config")
        self.assertEqual(check_args.action, "check")
        self.assertEqual(check_args.config, Path("/tmp/c.json"))
        self.assertTrue(doctor_args.probe)
        self.assertFalse(_arguments(["doctor"]).probe)

    def test_service_status_and_uninstall_reject_config_argument(self) -> None:
        for action in ("status", "uninstall"):
            with (
                self.subTest(action=action),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                _arguments(["service", action, "--config", "/tmp/c.json"])

    @patch("mocop.__main__.initialize_config")
    def test_init_reports_the_created_path(self, initialize) -> None:
        initialize.return_value = Path("/tmp/mocop/config.json")

        with patch("builtins.print") as output:
            result = main(["init", "--host", "gpu-01"])

        self.assertEqual(result, 0)
        initialize.assert_called_once()
        self.assertTrue(
            any(
                "/tmp/mocop/config.json" in call.args[0]
                for call in output.call_args_list
            )
        )

    @patch("mocop.__main__.initialize_config")
    def test_init_suggests_doctor_before_service_install(self, initialize) -> None:
        initialize.return_value = Path("/tmp/mocop/config.json")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(["init", "--host", "gpu-01"])

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("mocop doctor", output)
        self.assertLess(output.index("mocop doctor"), output.index("service install"))

    @patch("mocop.__main__.initialize_config")
    def test_lifecycle_errors_have_a_stable_exit_code(self, initialize) -> None:
        initialize.side_effect = LifecycleError("already exists")

        with redirect_stderr(io.StringIO()):
            result = main(["init"])

        self.assertEqual(result, 2)

    def test_config_check_reports_summary_and_environment_names(self) -> None:
        config_path = write_config(
            self.root / "config.json",
            webhooks=[
                {
                    "name": "ops",
                    "url_env": "MOCOP_CLI_TEST_URL",
                    "secret_env": "MOCOP_CLI_TEST_SECRET",
                }
            ],
        )
        secret_url = "https://hooks.example/secret-path"

        stdout = io.StringIO()
        environment = {"MOCOP_CLI_TEST_URL": secret_url, "MOCOP_CLI_TEST_SECRET": ""}
        with (
            patch.dict("os.environ", environment),
            redirect_stdout(stdout),
        ):
            result = main(["config", "check", "--config", str(config_path)])

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn(f"configuration OK: {config_path}", output)
        self.assertIn("hosts: 2", output)
        self.assertIn("persistence: disabled", output)
        self.assertIn("workloads: disabled", output)
        self.assertIn("topology: none", output)
        self.assertIn("url_env MOCOP_CLI_TEST_URL (set)", output)
        self.assertIn("secret_env MOCOP_CLI_TEST_SECRET (unset)", output)
        self.assertNotIn(secret_url, output)

    def test_config_check_reports_enabled_subsystems(self) -> None:
        config_path = write_config(
            self.root / "config.json",
            local_host="gpu-1",
            persistence={"enabled": True},
            workloads={"mode": "identity"},
            topology={
                "root": "gpu-1",
                "links": [{"source": "gpu-1", "target": "gpu-2", "transport": "ssh"}],
            },
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(["config", "check", "--config", str(config_path)])

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("hosts: 2 (local: gpu-1)", output)
        self.assertIn("persistence: enabled", output)
        self.assertIn("workloads: identity", output)
        self.assertIn("topology: configured (1 links)", output)
        self.assertIn("webhooks: none", output)

    def test_config_check_rejects_invalid_configuration_with_exit_2(self) -> None:
        config_path = self.root / "config.json"
        config_path.write_text('{"hosts": []}', encoding="utf-8")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(["config", "check", "--config", str(config_path)])

        self.assertEqual(result, 2)
        self.assertIn("Configuration error", stderr.getvalue())

    @patch("mocop.__main__.UserServiceManager")
    def test_install_verifies_activation_and_prints_dashboard(
        self, manager_cls
    ) -> None:
        config_path = write_config(self.root / "config.json")
        manager = manager_cls.return_value
        manager.wait_until_active.return_value = True
        manager.unit_path = Path("/tmp/systemd/mocop.service")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(["service", "install", "--config", str(config_path)])

        self.assertEqual(result, 0)
        manager.install.assert_called_once()
        manager.wait_until_active.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("Installed and started /tmp/systemd/mocop.service", output)
        self.assertIn("Dashboard: http://127.0.0.1:8787", output)
        self.assertIn("Logs: journalctl --user -u mocop -f", output)

    @patch("mocop.__main__.UserServiceManager")
    def test_install_timeout_prints_status_hint_not_success(self, manager_cls) -> None:
        config_path = write_config(self.root / "config.json")
        manager = manager_cls.return_value
        manager.wait_until_active.return_value = False
        manager.unit_path = Path("/tmp/systemd/mocop.service")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(["service", "install", "--config", str(config_path)])

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertNotIn("Installed and started", output)
        self.assertNotIn("Dashboard:", output)
        self.assertIn("not active yet", output)
        self.assertIn("systemctl --user status mocop", output)
        self.assertIn("Logs: journalctl --user -u mocop -f", output)


if __name__ == "__main__":
    unittest.main()
