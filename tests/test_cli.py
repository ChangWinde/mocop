from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from mocop.__main__ import _arguments, main
from mocop.lifecycle import LifecycleError


class CliTests(unittest.TestCase):
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
    def test_lifecycle_errors_have_a_stable_exit_code(self, initialize) -> None:
        initialize.side_effect = LifecycleError("already exists")

        with redirect_stderr(io.StringIO()):
            result = main(["init"])

        self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
