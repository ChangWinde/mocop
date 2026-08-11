from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mocop.config import MonitorConfig
from mocop.doctor import run_doctor
from mocop.probe import _BoundedProcessResult


def config(hosts: tuple[str, ...] = ("gpu-1",), local_host: str | None = None):
    return MonitorConfig(
        ssh_config=Path("/tmp/ssh-config"),
        auto_discover=False,
        hosts=hosts,
        exclude_hosts=frozenset(),
        poll_interval_seconds=5,
        probe_timeout_seconds=12,
        connect_timeout_seconds=5,
        max_workers=1,
        listen_host="127.0.0.1",
        listen_port=8787,
        local_host=local_host,
    )


def ssh_g_output(
    *,
    controlmaster: str = "no",
    controlpath: str = "none",
    controlpersist: str = "no",
    proxyjump: str | None = None,
) -> str:
    lines = [
        "user operator",
        "hostname 192.0.2.10",
        "port 22",
        "batchmode no",
        f"controlmaster {controlmaster}",
        f"controlpath {controlpath}",
        f"controlpersist {controlpersist}",
        "serveraliveinterval 0",
        "serveralivecountmax 3",
        "stricthostkeychecking ask",
    ]
    if proxyjump:
        lines.append(f"proxyjump {proxyjump}")
    return "\n".join(lines) + "\n"


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep host-diagnosis tests independent from the local systemd state.
        patcher = patch("mocop.doctor._service_staleness", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_doctor(self, config_value, **kwargs):
        stdout = io.StringIO()
        code = run_doctor(config_value, stdout=stdout, **kwargs)
        return code, stdout.getvalue()

    @patch("mocop.doctor._run_bounded_process")
    def test_reports_reachable_alias_with_reuse_configured(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_dir = Path(directory) / "sockets"
            socket_dir.mkdir(mode=0o700)
            run.side_effect = (
                _BoundedProcessResult(
                    0,
                    stdout=ssh_g_output(
                        controlmaster="auto",
                        controlpath=str(socket_dir / "probe@example:22"),
                        controlpersist="600",
                        proxyjump="bastion",
                    ),
                    stderr="",
                ),
                _BoundedProcessResult(0, stdout="", stderr=""),
                _BoundedProcessResult(0, stdout="", stderr=""),
            )

            code, output = self.run_doctor(config(), as_json=True)

        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(report["status"], "ok")
        host = report["hosts"][0]
        self.assertEqual(host["alias"], "gpu-1")
        self.assertTrue(host["reachable"])
        self.assertTrue(host["proxyJump"])
        self.assertTrue(host["reuse"]["reuseEnabled"])
        self.assertEqual(host["reuse"]["socketDirectoryMode"], "0700")
        self.assertIsInstance(host["coldLatencyMs"], float)
        reuse_warnings = [warning for warning in host["warnings"] if "reuse" in warning]
        self.assertEqual(reuse_warnings, [])
        self.assertEqual(report["transportDiscipline"]["serverAliveIntervalSeconds"], 2)
        self.assertNotIn("192.0.2.10", output)
        self.assertNotIn("operator", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_warns_when_connection_reuse_is_disabled(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
        )

        code, output = self.run_doctor(config())

        self.assertEqual(code, 0)
        self.assertIn("connection reuse is disabled", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_warns_on_group_accessible_socket_directory(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_dir = Path(directory) / "sockets"
            socket_dir.mkdir()
            os.chmod(socket_dir, 0o755)
            run.side_effect = (
                _BoundedProcessResult(
                    0,
                    stdout=ssh_g_output(
                        controlmaster="auto",
                        controlpath=str(socket_dir / "probe@example:22"),
                        controlpersist="600",
                    ),
                    stderr="",
                ),
            )

            code, output = self.run_doctor(config(), probe_connection=False)

        self.assertEqual(code, 0)
        self.assertIn("restrict it to 0700", output)
        self.assertIn("connection test skipped", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_missing_socket_directory_is_reported(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(
                0,
                stdout=ssh_g_output(
                    controlmaster="auto",
                    controlpath="/nonexistent/sockets/probe@example:22",
                    controlpersist="600",
                ),
                stderr="",
            ),
        )

        code, output = self.run_doctor(config(), probe_connection=False)

        self.assertEqual(code, 0)
        self.assertIn("control socket directory does not exist", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_unreachable_alias_fails_with_redacted_reason(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(
                255,
                stdout="",
                stderr="operator@192.0.2.10: Permission denied (publickey)",
            ),
            _BoundedProcessResult(
                255,
                stdout="",
                stderr="operator@192.0.2.10: Permission denied (publickey)",
            ),
        )

        code, output = self.run_doctor(config())

        self.assertEqual(code, 1)
        self.assertIn("UNREACHABLE", output)
        self.assertIn("SSH authentication failed", output)
        self.assertNotIn("192.0.2.10", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_timeout_during_connection_test_is_bounded(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            subprocess.TimeoutExpired(["ssh"], 12),
            subprocess.TimeoutExpired(["ssh"], 12),
        )

        code, output = self.run_doctor(config())

        self.assertEqual(code, 1)
        self.assertIn("SSH connection attempt timed out", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_unresolvable_alias_is_a_failure(self, run) -> None:
        run.side_effect = (_BoundedProcessResult(255, stdout="", stderr="no alias"),)

        code, output = self.run_doctor(config(), probe_connection=False)

        self.assertEqual(code, 1)
        self.assertIn("ssh -G could not resolve the alias", output)

    def test_unknown_host_filter_is_a_usage_error(self) -> None:
        code, _ = self.run_doctor(config(), host_filter=("absent",))
        self.assertEqual(code, 2)

    @patch("mocop.doctor._run_bounded_process")
    def test_local_host_skips_ssh_and_is_reported(self, run) -> None:
        code, output = self.run_doctor(
            config(hosts=("star-l",), local_host="star-l"), probe_connection=False
        )

        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("local target, SSH not used", output)
        self.assertIn("no remote SSH aliases", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_rejects_unsafe_alias_without_subprocess(self, run) -> None:
        code, output = self.run_doctor(
            config(hosts=("gpu-1;rm",)), probe_connection=False
        )

        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("unsafe characters", output)

    def test_profile_requires_connection_tests(self) -> None:
        code, _ = self.run_doctor(config(), probe_connection=False, profile=True)
        self.assertEqual(code, 2)

    @patch(
        "mocop.doctor.time.monotonic",
        side_effect=(0.0, 0.1, 1.0, 1.05, 2.0, 2.2, 3.0, 3.89, 4.0, 4.62),
    )
    @patch("mocop.doctor._run_bounded_process")
    def test_profile_decomposes_collection_latency(self, run, _monotonic) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),  # cold probe
            _BoundedProcessResult(0, stdout="", stderr=""),  # reuse probe
            _BoundedProcessResult(0, stdout="", stderr=""),  # transport stage
            _BoundedProcessResult(0, stdout="MONITOR_V6", stderr=""),  # script
            _BoundedProcessResult(0, stdout="0, GPU-abc", stderr=""),  # nvidia
        )

        code, output = self.run_doctor(config(), profile=True, as_json=True)

        self.assertEqual(code, 0)
        report = json.loads(output)
        profile = report["hosts"][0]["profile"]
        self.assertEqual(profile["transportMs"], 200.0)
        self.assertEqual(profile["scriptTotalMs"], 890.0)
        self.assertEqual(profile["nvidiaQueryMs"], 620.0)
        self.assertEqual(profile["remoteExecutionMs"], 690.0)
        self.assertEqual(profile["nvidiaExecutionMs"], 420.0)
        script_call = run.call_args_list[4]
        self.assertIn("MONITOR_V6", script_call.kwargs["input_text"])
        self.assertEqual(script_call.args[0][-2:], ["sh", "-s"])
        nvidia_call = run.call_args_list[5]
        self.assertTrue(nvidia_call.args[0][-2].startswith("--query-gpu="))

    @patch("mocop.doctor._run_bounded_process")
    def test_profile_reports_missing_nvidia_without_failing(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="MONITOR_V6", stderr=""),
            _BoundedProcessResult(127, stdout="", stderr="nvidia-smi: not found"),
        )

        code, output = self.run_doctor(config(), profile=True)

        self.assertEqual(code, 0)
        self.assertIn("profile:", output)
        self.assertIn("profile warning:", output)


class ServiceStalenessTests(unittest.TestCase):
    def build_install(self, directory: Path) -> Path:
        python_path = directory / "bin" / "python"
        python_path.parent.mkdir(parents=True)
        python_path.write_text("", encoding="utf-8")
        package = directory / "lib" / "python3.10" / "site-packages" / "mocop"
        package.mkdir(parents=True)
        (package / "probe.py").write_text("# installed", encoding="utf-8")
        unit = directory / "mocop.service"
        unit.write_text(
            f'[Service]\nExecStart="{python_path}" -m mocop --managed-service\n',
            encoding="utf-8",
        )
        return unit

    def staleness(self, unit: Path, *, uptime: float, start_usec: int):
        from mocop import doctor

        systemctl = _BoundedProcessResult(
            0,
            stdout=(
                f"ActiveState=active\nExecMainStartTimestampMonotonic={start_usec}\n"
            ),
            stderr="",
        )
        with (
            patch("mocop.doctor.user_unit_path", return_value=unit),
            patch("mocop.doctor._run_bounded_process", return_value=systemctl),
            patch("mocop.doctor._system_uptime_seconds", return_value=uptime),
        ):
            return doctor._service_staleness()

    def test_detects_install_newer_than_running_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = self.build_install(root)
            # Service started 4,999 seconds ago; package written just now.
            result = self.staleness(unit, uptime=5_000.0, start_usec=1_000_000)

        self.assertIsNotNone(result)
        self.assertTrue(result["staleCode"])

    def test_running_service_with_current_code_is_not_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = self.build_install(root)
            package_file = (
                root / "lib" / "python3.10" / "site-packages" / "mocop" / "probe.py"
            )
            old = time.time() - 10_000
            os.utime(package_file, (old, old))
            result = self.staleness(unit, uptime=5_000.0, start_usec=1_000_000)

        self.assertIsNotNone(result)
        self.assertFalse(result["staleCode"])

    def test_missing_unit_is_silently_skipped(self) -> None:
        from mocop import doctor

        with patch(
            "mocop.doctor.user_unit_path",
            return_value=Path("/nonexistent/mocop.service"),
        ):
            self.assertIsNone(doctor._service_staleness())


if __name__ == "__main__":
    unittest.main()
