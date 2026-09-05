from __future__ import annotations

import _thread
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mocop import doctor
from mocop.config import HostOverrideConfig, MonitorConfig, WorkloadConfig
from mocop.diagnostics import diagnose_condition
from mocop.doctor import run_doctor
from mocop.models import GpuMetrics, GpuProcess, ProbeResult, WorkloadMetadata
from mocop.probe import _BoundedProcessResult, _ProcessOutputLimitExceeded


def config(
    hosts: tuple[str, ...] = ("gpu-1",),
    local_host: str | None = None,
    *,
    max_workers: int = 1,
    poll_interval_seconds: float = 5,
    host_overrides: tuple[tuple[str, HostOverrideConfig], ...] = (),
    workloads: WorkloadConfig | None = None,
):
    return MonitorConfig(
        ssh_config=Path("/tmp/ssh-config"),
        auto_discover=False,
        hosts=hosts,
        exclude_hosts=frozenset(),
        poll_interval_seconds=poll_interval_seconds,
        probe_timeout_seconds=12,
        connect_timeout_seconds=5,
        max_workers=max_workers,
        listen_host="127.0.0.1",
        listen_port=8787,
        local_host=local_host,
        host_overrides=host_overrides,
        workloads=workloads or WorkloadConfig(),
    )


def gpu(index: int = 0, processes: tuple[GpuProcess, ...] = ()) -> GpuMetrics:
    return GpuMetrics(
        index=index,
        uuid=f"GPU-{index}",
        name="Test GPU",
        driver_version="550.00",
        pstate=None,
        temperature_c=None,
        utilization_gpu_pct=None,
        utilization_memory_pct=None,
        memory_total_mib=None,
        memory_used_mib=None,
        memory_free_mib=None,
        power_draw_w=None,
        power_limit_w=None,
        processes=processes,
    )


class FakeCollectionProbe:
    """Stands in for the production probe; never spawns SSH."""

    def __init__(self, results: dict[str, ProbeResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[str] = []
        self.closed = False

    def probe(self, host: str, _config: MonitorConfig) -> ProbeResult:
        self.calls.append(host)
        return self.results.get(
            host, ProbeResult(host=host, status="online", latency_ms=100)
        )

    def close(self) -> None:
        self.closed = True


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
    ]
    if proxyjump:
        lines.append(f"proxyjump {proxyjump}")
    return "\n".join(lines) + "\n"


def profile_marker(
    *,
    start_ns: int = 100_000_000,
    script_ns: int = 270_000_000,
    nvidia_ns: int = 420_000_000,
    nvidia_status: int = 0,
) -> str:
    t1 = start_ns + script_ns
    t2 = t1 + nvidia_ns
    return f"MOCOP_PROFILE_V1\t{start_ns}\t{t1}\t{t2}\t{nvidia_status}\n"


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
                _BoundedProcessResult(0, stdout="", stderr=""),  # cold probe
                _BoundedProcessResult(0, stdout="", stderr=""),  # reuse warm-up
                _BoundedProcessResult(0, stdout="", stderr=""),  # timed reuse
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
        self.assertEqual(host["probeTimeoutSeconds"], 12)
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
    def test_warns_when_socket_parent_is_not_a_directory(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "sockets"
            parent.write_text("", encoding="utf-8")
            run.side_effect = (
                _BoundedProcessResult(
                    0,
                    stdout=ssh_g_output(
                        controlmaster="auto",
                        controlpath=str(parent / "probe@example:22"),
                        controlpersist="600",
                    ),
                    stderr="",
                ),
            )

            code, output = self.run_doctor(config(), probe_connection=False)

        self.assertEqual(code, 0)
        self.assertIn("not a directory", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_warns_when_socket_directory_owned_by_another_user(self, run) -> None:
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
                    ),
                    stderr="",
                ),
            )

            with patch("mocop.doctor.os.geteuid", return_value=os.geteuid() + 1):
                code, output = self.run_doctor(config(), probe_connection=False)

        self.assertEqual(code, 0)
        self.assertIn("not owned by the current user", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_warns_when_controlpersist_is_shorter_than_poll_interval(self, run) -> None:
        cases = (
            ("1", True),
            ("3s", True),
            ("600", False),
            ("10m", False),
            ("yes", False),
        )
        for persist, expect_warning in cases:
            with self.subTest(controlpersist=persist):
                with tempfile.TemporaryDirectory() as directory:
                    socket_dir = Path(directory) / "sockets"
                    socket_dir.mkdir(mode=0o700)
                    run.side_effect = (
                        _BoundedProcessResult(
                            0,
                            stdout=ssh_g_output(
                                controlmaster="auto",
                                controlpath=str(socket_dir / "probe@example:22"),
                                controlpersist=persist,
                            ),
                            stderr="",
                        ),
                    )

                    code, output = self.run_doctor(config(), probe_connection=False)

                self.assertEqual(code, 0)
                if expect_warning:
                    self.assertIn("shorter than the 5s collection interval", output)
                else:
                    self.assertNotIn("shorter than", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_controlpersist_compares_against_host_poll_override(self, run) -> None:
        override = HostOverrideConfig(poll_interval_seconds=600.0)
        with tempfile.TemporaryDirectory() as directory:
            socket_dir = Path(directory) / "sockets"
            socket_dir.mkdir(mode=0o700)
            run.side_effect = (
                _BoundedProcessResult(
                    0,
                    stdout=ssh_g_output(
                        controlmaster="auto",
                        controlpath=str(socket_dir / "probe@example:22"),
                        controlpersist="300",
                    ),
                    stderr="",
                ),
            )

            code, output = self.run_doctor(
                config(host_overrides=(("gpu-1", override),)),
                probe_connection=False,
            )

        self.assertEqual(code, 0)
        self.assertIn("shorter than the 600s collection interval", output)

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
    def test_remote_command_failure_is_not_reported_as_ssh_failure(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(127, stdout="", stderr="sh: true: not found"),
            _BoundedProcessResult(127, stdout="", stderr="sh: true: not found"),
        )

        code, output = self.run_doctor(config())

        self.assertEqual(code, 1)
        self.assertIn("remote command failed (exit 127)", output)
        self.assertNotIn("SSH connection failed", output)
        self.assertNotIn("not found", output)

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
    def test_output_limit_during_probe_is_a_redacted_failure(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _ProcessOutputLimitExceeded(),
            _ProcessOutputLimitExceeded(),
        )

        code, output = self.run_doctor(config())

        self.assertEqual(code, 1)
        self.assertIn("remote output exceeded the configured limit", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_output_limit_during_alias_resolution_is_reported(self, run) -> None:
        run.side_effect = (_ProcessOutputLimitExceeded(),)

        code, output = self.run_doctor(config(), probe_connection=False)

        self.assertEqual(code, 1)
        self.assertIn("ssh -G could not resolve the alias", output)

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
        self.assertNotIn("no remote SSH aliases", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_local_host_filter_is_valid_and_never_uses_ssh(self, run) -> None:
        code, output = self.run_doctor(
            config(hosts=("star-l", "gpu-1"), local_host="star-l"),
            host_filter=("star-l",),
            probe_connection=False,
        )

        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("star-l: local target", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_json_hosts_include_the_local_target(self, run) -> None:
        code, output = self.run_doctor(
            config(hosts=("star-l",), local_host="star-l"),
            probe_connection=False,
            as_json=True,
        )

        self.assertEqual(code, 0)
        run.assert_not_called()
        report = json.loads(output)
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["hosts"],
            [
                {
                    "alias": "star-l",
                    "local": True,
                    "reachable": True,
                    "transport": "local",
                }
            ],
        )

    @patch("mocop.doctor._run_bounded_process")
    def test_json_refusals_use_the_cli_envelope(self, _run) -> None:
        code, output = self.run_doctor(config(), host_filter=("absent",), as_json=True)

        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertEqual(report["ok"], False)
        self.assertEqual(report["code"], "UNKNOWN_HOST")
        self.assertIn("absent", report["error"])

    @patch("mocop.doctor._run_bounded_process")
    def test_auto_discovered_hosts_are_diagnosed(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ssh_config = Path(directory) / "config"
            ssh_config.write_text("Host discovered-gpu\n", encoding="utf-8")
            discovered = replace(
                config(hosts=()), auto_discover=True, ssh_config=ssh_config
            )
            run.return_value = _BoundedProcessResult(
                0, stdout=ssh_g_output(), stderr=""
            )

            code, output = self.run_doctor(
                discovered, probe_connection=False, as_json=True
            )

        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(
            [item["alias"] for item in report["hosts"]], ["discovered-gpu"]
        )

    @patch("mocop.doctor._run_bounded_process")
    def test_rejects_unsafe_alias_without_subprocess(self, run) -> None:
        code, output = self.run_doctor(
            config(hosts=("gpu-1;rm",)), probe_connection=False
        )

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertEqual(output, "")

    @patch("mocop.doctor._run_bounded_process")
    def test_probe_uses_keepalive_and_host_timeout_override(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
        )
        override = HostOverrideConfig(probe_timeout_seconds=44.0)

        code, output = self.run_doctor(
            config(host_overrides=(("gpu-1", override),)), as_json=True
        )

        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(report["hosts"][0]["probeTimeoutSeconds"], 44.0)
        probe_call = run.call_args_list[1]
        command = probe_call.args[0]
        self.assertIn("ServerAliveInterval=2", command)
        self.assertIn("ServerAliveCountMax=2", command)
        self.assertEqual(probe_call.kwargs["timeout_seconds"], 44.0)

    @patch("mocop.doctor._run_bounded_process")
    def test_reuse_probe_warms_up_master_before_timing(self, run) -> None:
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
                    ),
                    stderr="",
                ),
                _BoundedProcessResult(0, stdout="", stderr=""),  # cold probe
                _BoundedProcessResult(0, stdout="", stderr=""),  # reuse warm-up
                _BoundedProcessResult(0, stdout="", stderr=""),  # timed reuse
            )

            code, _ = self.run_doctor(config())

        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 4)
        cold_command = run.call_args_list[1].args[0]
        self.assertIn("ControlMaster=no", cold_command)
        for call in run.call_args_list[2:]:
            self.assertNotIn("ControlMaster=no", call.args[0])

    @patch("mocop.doctor._run_bounded_process")
    def test_expired_budget_short_circuits_all_stages(self, run) -> None:
        report = doctor._diagnose_host(
            "gpu-1",
            config(),
            probe_connection=True,
            profile=True,
            budget_seconds=0.0,
        )

        run.assert_not_called()
        self.assertFalse(report["reachable"])
        self.assertIn("host diagnosis time budget exhausted", report["warnings"])

    @patch("mocop.doctor._run_bounded_process")
    def test_hosts_are_diagnosed_concurrently_in_config_order(self, run) -> None:
        def fake_run(command, **kwargs):
            if "-G" in command:
                return _BoundedProcessResult(0, stdout=ssh_g_output(), stderr="")
            return _BoundedProcessResult(0, stdout="", stderr="")

        run.side_effect = fake_run
        hosts = ("gpu-1", "gpu-2", "gpu-3")

        code, output = self.run_doctor(config(hosts=hosts, max_workers=4), as_json=True)

        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual([host["alias"] for host in report["hosts"]], list(hosts))
        self.assertTrue(all(host["reachable"] for host in report["hosts"]))

    def test_profile_requires_connection_tests(self) -> None:
        code, _ = self.run_doctor(config(), probe_connection=False, profile=True)
        self.assertEqual(code, 2)

    @patch("mocop.doctor._run_remote")
    def test_profile_stages_are_exclusive_and_sum_to_total(self, run_remote) -> None:
        run_remote.return_value = (
            890.0,
            _BoundedProcessResult(0, stdout=profile_marker(), stderr=""),
            None,
        )

        profile = doctor._profile_host("gpu-1", config(), timeout_seconds=12.0)

        self.assertEqual(profile["totalMs"], 890.0)
        self.assertEqual(profile["scriptMs"], 270.0)
        self.assertEqual(profile["nvidiaQueryMs"], 420.0)
        self.assertEqual(profile["transportMs"], 200.0)
        self.assertAlmostEqual(
            profile["transportMs"] + profile["scriptMs"] + profile["nvidiaQueryMs"],
            profile["totalMs"],
        )
        self.assertNotIn("failure", profile)
        self.assertNotIn("nvidiaFailure", profile)
        self.assertEqual(run_remote.call_args.args[2], ("sh", "-s"))
        script = run_remote.call_args.kwargs["input_text"]
        self.assertIn("/proc/uptime", script)
        self.assertNotIn("date +%s%N", script)
        self.assertIn("nvidia-smi --query-gpu=", script)
        self.assertIn("MOCOP_PROFILE_V1", script)

    def test_profile_script_uses_a_busybox_compatible_monotonic_clock(self) -> None:
        completed = subprocess.run(
            ["sh", "-s"],
            input=doctor._PROFILE_SCRIPT,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIsNotNone(doctor._parse_profile_marker(completed.stdout))

    @patch("mocop.doctor._run_bounded_process")
    def test_profile_runs_one_instrumented_remote_call(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),  # cold probe
            _BoundedProcessResult(0, stdout="", stderr=""),  # reuse probe
            _BoundedProcessResult(0, stdout=profile_marker(), stderr=""),  # profile
        )

        code, output = self.run_doctor(config(), profile=True, as_json=True)

        self.assertEqual(code, 0)
        report = json.loads(output)
        profile = report["hosts"][0]["profile"]
        self.assertEqual(profile["scriptMs"], 270.0)
        self.assertEqual(profile["nvidiaQueryMs"], 420.0)
        self.assertIsInstance(profile["totalMs"], float)
        self.assertIsInstance(profile["transportMs"], float)
        self.assertEqual(run.call_count, 4)
        profile_call = run.call_args_list[3]
        self.assertEqual(profile_call.args[0][-2:], ["sh", "-s"])
        self.assertIn("MOCOP_PROFILE_V1", profile_call.kwargs["input_text"])

    @patch("mocop.doctor._run_bounded_process")
    def test_profile_is_skipped_when_transport_failed(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(255, stdout="", stderr="Permission denied"),
            _BoundedProcessResult(255, stdout="", stderr="Permission denied"),
        )

        code, output = self.run_doctor(config(), profile=True, as_json=True)

        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 3)
        self.assertNotIn("profile", json.loads(output)["hosts"][0])

    @patch("mocop.doctor._run_bounded_process")
    def test_profile_reports_missing_nvidia_without_failing(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(
                0,
                stdout=profile_marker(nvidia_ns=0, nvidia_status=127),
                stderr="",
            ),
        )

        code, output = self.run_doctor(config(), profile=True)

        self.assertEqual(code, 0)
        self.assertIn("profile:", output)
        self.assertIn("profile warning:", output)
        self.assertIn("nvidia-smi is unavailable", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_profile_stage_failure_fails_doctor(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(127, stdout="", stderr="sh: not found"),
        )

        code, output = self.run_doctor(config(), profile=True, as_json=True)

        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(report["status"], "failed")
        host = report["hosts"][0]
        self.assertTrue(host["reachable"])
        self.assertEqual(host["profile"]["failure"], "remote command failed (exit 127)")

    @patch("mocop.doctor._run_bounded_process")
    def test_profile_unrecognized_output_is_a_failure(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="banner noise\n", stderr=""),
        )

        code, output = self.run_doctor(config(), profile=True)

        self.assertEqual(code, 1)
        self.assertIn("remote profiling output was not recognized", output)

    def patch_collection_probe(
        self, results: dict[str, ProbeResult] | None = None
    ) -> list[FakeCollectionProbe]:
        """Replace the production probe class; return the created instances."""
        created: list[FakeCollectionProbe] = []

        def factory() -> FakeCollectionProbe:
            probe = FakeCollectionProbe(results)
            created.append(probe)
            return probe

        patcher = patch("mocop.doctor.OpenSshLinuxResourceProbe", factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return created

    def test_collect_requires_connection_tests(self) -> None:
        code, _ = self.run_doctor(config(), probe_connection=False, collect=True)
        self.assertEqual(code, 2)

    def test_collect_reserves_one_extra_budget_stage(self) -> None:
        base = doctor._host_budget_seconds("gpu-1", config())
        collecting = doctor._host_budget_seconds("gpu-1", config(), collect=True)
        self.assertEqual(base, 10 + 3 * 12)
        self.assertEqual(collecting, 10 + 4 * 12)

    @patch("mocop.doctor._run_bounded_process")
    def test_collection_probe_reports_production_summary(self, run) -> None:
        processes = (
            GpuProcess(
                pid=4242,
                name="python",
                used_memory_mib=2048.0,
                workload=WorkloadMetadata(kind="process"),
            ),
            GpuProcess(pid=4243, name="python", used_memory_mib=1024.0),
        )
        created = self.patch_collection_probe(
            {
                "gpu-1": ProbeResult(
                    host="gpu-1",
                    status="online",
                    latency_ms=321,
                    gpus=(gpu(0, processes=processes), gpu(1)),
                )
            }
        )
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
        )

        code, output = self.run_doctor(
            config(workloads=WorkloadConfig(mode="identity")),
            collect=True,
            as_json=True,
        )

        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(
            report["hosts"][0]["probe"],
            {
                "status": "online",
                "latencyMs": 321,
                "gpuCount": 2,
                "processCount": 2,
                "workloadCoveragePct": 50.0,
                "message": None,
            },
        )
        self.assertEqual(created[0].calls, ["gpu-1"])
        self.assertTrue(created[0].closed)

    @patch("mocop.doctor._run_bounded_process")
    def test_collection_probe_text_report_and_disabled_workloads(self, run) -> None:
        processes = (GpuProcess(pid=4242, name="python", used_memory_mib=2048.0),)
        self.patch_collection_probe(
            {
                "gpu-1": ProbeResult(
                    host="gpu-1",
                    status="online",
                    latency_ms=321,
                    gpus=(gpu(0, processes=processes),),
                )
            }
        )
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
        )

        code, output = self.run_doctor(config(), collect=True)

        self.assertEqual(code, 0)
        self.assertIn(
            "probe: online (321 ms, 1 GPUs, 1 processes, workload coverage n/a)",
            output,
        )

    @patch("mocop.doctor._run_bounded_process")
    def test_collection_probe_failure_fails_doctor(self, run) -> None:
        self.patch_collection_probe(
            {
                "gpu-1": ProbeResult(
                    host="gpu-1",
                    status="unreachable",
                    latency_ms=12000,
                    message="SSH produced no output before the collection timeout",
                )
            }
        )
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
        )

        code, output = self.run_doctor(config(), collect=True, as_json=True)

        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(report["status"], "failed")
        probe_report = report["hosts"][0]["probe"]
        self.assertEqual(probe_report["status"], "unreachable")
        self.assertIn("no output", probe_report["message"])

    @patch("mocop.doctor._run_bounded_process")
    def test_collection_probe_is_skipped_when_transport_failed(self, run) -> None:
        created = self.patch_collection_probe()
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(255, stdout="", stderr="Permission denied"),
            _BoundedProcessResult(255, stdout="", stderr="Permission denied"),
        )

        code, output = self.run_doctor(config(), collect=True, as_json=True)

        self.assertEqual(code, 1)
        self.assertNotIn("probe", json.loads(output)["hosts"][0])
        self.assertEqual(created[0].calls, [])

    @patch("mocop.doctor._run_bounded_process")
    def test_collection_probe_is_shared_across_hosts(self, run) -> None:
        created = self.patch_collection_probe()

        def fake_run(command, **kwargs):
            if "-G" in command:
                return _BoundedProcessResult(0, stdout=ssh_g_output(), stderr="")
            return _BoundedProcessResult(0, stdout="", stderr="")

        run.side_effect = fake_run

        code, _ = self.run_doctor(
            config(hosts=("gpu-1", "gpu-2")), collect=True, as_json=True
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].calls, ["gpu-1", "gpu-2"])
        self.assertTrue(created[0].closed)

    @patch("mocop.doctor._run_bounded_process")
    def test_collection_stage_respects_the_host_budget(self, run) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, stdout=ssh_g_output(), stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
            _BoundedProcessResult(0, stdout="", stderr=""),
        )
        fake = FakeCollectionProbe()
        real_stage = doctor._stage_timeout
        stage_calls: list[float] = []

        def stage(deadline: float, stage_timeout_seconds: float) -> float | None:
            stage_calls.append(stage_timeout_seconds)
            # Stage 4 is the collection run: resolve, cold, reuse came first.
            if len(stage_calls) == 4:
                return None
            return real_stage(deadline, stage_timeout_seconds)

        with patch("mocop.doctor._stage_timeout", side_effect=stage):
            report = doctor._diagnose_host(
                "gpu-1", config(), probe_connection=True, collection_probe=fake
            )

        self.assertEqual(
            report["probe"], {"failure": "host diagnosis time budget exhausted"}
        )
        self.assertEqual(fake.calls, [])
        self.assertTrue(doctor._report_failed(report))

    @patch("mocop.doctor._run_bounded_process")
    def test_shared_controlpath_across_aliases_warns_each_alias(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shared_path = str(Path(directory) / "fixed-socket")

            def fake_run(command, **kwargs):
                return _BoundedProcessResult(
                    0,
                    stdout=ssh_g_output(
                        controlmaster="auto",
                        controlpath=shared_path,
                        controlpersist="600",
                    ),
                    stderr="",
                )

            run.side_effect = fake_run

            code, output = self.run_doctor(
                config(hosts=("gpu-1", "gpu-2")),
                probe_connection=False,
                as_json=True,
            )

        self.assertEqual(code, 0)
        report = json.loads(output)
        for alias, peer in (("gpu-1", "gpu-2"), ("gpu-2", "gpu-1")):
            entry = next(host for host in report["hosts"] if host["alias"] == alias)
            warning = next(
                warning
                for warning in entry["warnings"]
                if "ControlPath" in warning and "wrong host" in warning
            )
            self.assertIn("2 aliases", warning)
            self.assertIn(peer, warning)
        # The expanded path may contain the remote user, host, and port; the
        # warning names only the affected aliases.
        self.assertNotIn("fixed-socket", output)
        self.assertNotIn("_controlPath", output)

    @patch("mocop.doctor._run_bounded_process")
    def test_distinct_controlpaths_do_not_warn(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_dir = Path(directory) / "sockets"
            socket_dir.mkdir(mode=0o700)

            def fake_run(command, **kwargs):
                alias = command[-1]
                return _BoundedProcessResult(
                    0,
                    stdout=ssh_g_output(
                        controlmaster="auto",
                        controlpath=str(socket_dir / f"probe@{alias}:22"),
                        controlpersist="600",
                    ),
                    stderr="",
                )

            run.side_effect = fake_run

            code, output = self.run_doctor(
                config(hosts=("gpu-1", "gpu-2")), probe_connection=False
            )

        self.assertEqual(code, 0)
        self.assertNotIn("wrong host", output)

    def test_keyboard_interrupt_cancels_parallel_host_jobs(self) -> None:
        started = threading.Event()
        stopped = threading.Event()

        def blocked_diagnosis(_alias, _config, **kwargs):
            registry = kwargs["process_registry"]
            started.set()
            registry.cancelled.wait(2)
            stopped.set()
            return {"alias": "gpu-1", "warnings": [], "reachable": False}

        def interrupt() -> None:
            if started.wait(1):
                _thread.interrupt_main()

        trigger = threading.Thread(target=interrupt)
        trigger.start()
        begun = time.monotonic()
        try:
            with (
                patch("mocop.doctor._diagnose_host", side_effect=blocked_diagnosis),
                self.assertRaises(KeyboardInterrupt),
            ):
                run_doctor(config(), stdout=io.StringIO())
        finally:
            trigger.join(1)

        self.assertTrue(stopped.wait(0.5))
        self.assertLess(time.monotonic() - begun, 1.5)


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
        self.assertEqual(result["startedEpochSource"], "uptime-estimate")

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
        with patch(
            "mocop.doctor.user_unit_path",
            return_value=Path("/nonexistent/mocop.service"),
        ):
            self.assertIsNone(doctor._service_staleness())

    def test_staleness_prefers_systemd_realtime_start(self) -> None:
        start_epoch = 1_700_000_000
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = self.build_install(root)
            package_file = (
                root / "lib" / "python3.10" / "site-packages" / "mocop" / "probe.py"
            )
            # Written 50 s after the realtime start: stale under the systemd
            # timestamp, but far in the past for the uptime estimate.
            os.utime(package_file, (start_epoch + 50, start_epoch + 50))
            systemctl = _BoundedProcessResult(
                0,
                stdout=(
                    "ActiveState=active\n"
                    f"ExecMainStartTimestamp=@{start_epoch}\n"
                    "ExecMainStartTimestampMonotonic=1000000\n"
                ),
                stderr="",
            )
            find_spec = _BoundedProcessResult(1, stdout="", stderr="")
            with (
                patch("mocop.doctor.user_unit_path", return_value=unit),
                patch(
                    "mocop.doctor._run_bounded_process",
                    side_effect=(systemctl, find_spec),
                ),
                patch("mocop.doctor._system_uptime_seconds", return_value=1.0),
            ):
                result = doctor._service_staleness()

        self.assertIsNotNone(result)
        self.assertTrue(result["staleCode"])
        self.assertEqual(result["startedEpochSource"], "systemd")
        self.assertEqual(result["serviceStartedEpoch"], float(start_epoch))

    def test_staleness_falls_back_when_unix_timestamps_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = self.build_install(root)
            package_file = (
                root / "lib" / "python3.10" / "site-packages" / "mocop" / "probe.py"
            )
            old = time.time() - 10_000
            os.utime(package_file, (old, old))
            rejected = _BoundedProcessResult(1, stdout="", stderr="unknown option")
            systemctl = _BoundedProcessResult(
                0,
                stdout=(
                    "ActiveState=active\nExecMainStartTimestampMonotonic=1000000\n"
                ),
                stderr="",
            )
            find_spec = _BoundedProcessResult(1, stdout="", stderr="")
            with (
                patch("mocop.doctor.user_unit_path", return_value=unit),
                patch(
                    "mocop.doctor._run_bounded_process",
                    side_effect=(rejected, systemctl, find_spec),
                ) as run,
                patch("mocop.doctor._system_uptime_seconds", return_value=5_000.0),
            ):
                result = doctor._service_staleness()

        self.assertIsNotNone(result)
        self.assertFalse(result["staleCode"])
        self.assertEqual(result["startedEpochSource"], "uptime-estimate")
        self.assertIn("--timestamp=unix", run.call_args_list[0].args[0])
        self.assertNotIn("--timestamp=unix", run.call_args_list[1].args[0])

    def test_staleness_locates_install_through_unit_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python_path = root / "bin" / "python"
            python_path.parent.mkdir(parents=True)
            python_path.write_text("", encoding="utf-8")
            # A layout the fixed virtualenv glob can never find.
            package = root / "custom" / "dist-packages" / "mocop"
            package.mkdir(parents=True)
            (package / "probe.py").write_text("# installed", encoding="utf-8")
            unit = root / "mocop.service"
            unit.write_text(
                f'[Service]\nExecStart="{python_path}" -m mocop --managed-service\n',
                encoding="utf-8",
            )
            systemctl = _BoundedProcessResult(
                0,
                stdout=(
                    "ActiveState=active\nExecMainStartTimestampMonotonic=1000000\n"
                ),
                stderr="",
            )
            find_spec = _BoundedProcessResult(0, stdout=f"{package}\n", stderr="")
            with (
                patch("mocop.doctor.user_unit_path", return_value=unit),
                patch(
                    "mocop.doctor._run_bounded_process",
                    side_effect=(systemctl, find_spec),
                ) as run,
                patch("mocop.doctor._system_uptime_seconds", return_value=5_000.0),
            ):
                result = doctor._service_staleness()

        self.assertIsNotNone(result)
        # Service started 4,999 seconds ago; package written just now.
        self.assertTrue(result["staleCode"])
        interpreter_call = run.call_args_list[1].args[0]
        self.assertEqual(interpreter_call[0], str(python_path))
        self.assertEqual(interpreter_call[1], "-c")
        self.assertIn("find_spec", interpreter_call[2])


class DiagnosticsTests(unittest.TestCase):
    def gpu_server(self) -> dict[str, object]:
        return {
            "gpus": [
                {
                    "index": 1,
                    "uuid": "GPU-one",
                    "utilization_gpu_pct": 5.0,
                    "memory_used_mib": 100.0,
                    "processes": [],
                },
                {
                    "index": 10,
                    "uuid": "GPU-ten",
                    "utilization_gpu_pct": 90.0,
                    "memory_used_mib": 900.0,
                    "processes": [],
                },
            ]
        }

    def evidence_units(self, diagnosis: dict[str, object]) -> dict[str, object]:
        return {
            item["label"]: item.get("unit")
            for item in diagnosis["evidence"]
            if item["label"] in ("current", "threshold")
        }

    def test_disk_evidence_reports_absolute_headroom(self) -> None:
        server = {
            "system": {
                "disks": [
                    {
                        "mountpoint": "/other",
                        "available_mib": 999999,
                        "total_mib": 999999,
                    },
                    {
                        "mountpoint": "/",
                        "available_mib": 2252.8,
                        "total_mib": 51200,
                    },
                ]
            }
        }
        diagnosis = diagnose_condition(
            {"category": "disk", "resource": "/", "value": 96, "threshold": 85},
            server,
        )

        evidence = {item["label"]: item for item in diagnosis["evidence"]}
        # A percentage cannot be triaged on its own, so the alert carries the
        # headroom that distinguishes minutes of runway from days.
        self.assertEqual(evidence["freeSpace"]["value"], 2.2)
        self.assertEqual(evidence["freeSpace"]["unit"], "GiB")
        self.assertEqual(evidence["capacity"]["value"], 50.0)
        self.assertEqual(evidence["current"]["unit"], "%")

    def test_disk_evidence_omits_headroom_when_mount_is_unknown(self) -> None:
        diagnosis = diagnose_condition(
            {"category": "disk", "resource": "/gone", "value": 96, "threshold": 85},
            {"system": {"disks": [{"mountpoint": "/", "available_mib": 10}]}},
        )

        self.assertEqual(
            [item["label"] for item in diagnosis["evidence"]],
            ["current", "threshold"],
        )

    def test_gpu_temperature_evidence_uses_celsius(self) -> None:
        diagnosis = diagnose_condition(
            {
                "category": "gpu_temperature",
                "resource": "GPU 0",
                "value": 91,
                "threshold": 80,
            },
            None,
        )
        self.assertEqual(
            self.evidence_units(diagnosis), {"current": "°C", "threshold": "°C"}
        )

    def test_count_evidence_has_no_unit(self) -> None:
        for category in ("gpu_count", "gpu_ecc"):
            with self.subTest(category=category):
                diagnosis = diagnose_condition(
                    {
                        "category": category,
                        "resource": "GPU inventory",
                        "value": 7,
                        "threshold": 8,
                    },
                    None,
                )
                self.assertEqual(
                    self.evidence_units(diagnosis),
                    {"current": None, "threshold": None},
                )
                for item in diagnosis["evidence"]:
                    self.assertNotIn("unit", item)

    def test_ratio_categories_keep_percent_unit(self) -> None:
        categories = ("cpu", "memory", "swap", "disk", "gpu_memory", "gpu_idle_memory")
        for category in categories:
            with self.subTest(category=category):
                diagnosis = diagnose_condition(
                    {
                        "category": category,
                        "resource": "GPU 0 VRAM",
                        "value": 95,
                        "threshold": 90,
                    },
                    None,
                )
                self.assertEqual(
                    self.evidence_units(diagnosis),
                    {"current": "%", "threshold": "%"},
                )

    def test_gpu_ten_does_not_match_gpu_one(self) -> None:
        diagnosis = diagnose_condition(
            {
                "category": "gpu_memory",
                "resource": "GPU 10 VRAM",
                "conditionKey": "gpu_memory:10",
                "value": 95,
                "threshold": 90,
            },
            self.gpu_server(),
        )
        self.assertEqual(diagnosis["targetGpuIndex"], 10)
        utilization = next(
            item
            for item in diagnosis["evidence"]
            if item["label"] == "gpuUtilizationPct"
        )
        self.assertEqual(utilization["value"], 90.0)

    def test_gpu_uuid_match_takes_precedence_over_resource_label(self) -> None:
        diagnosis = diagnose_condition(
            {
                "category": "gpu_temperature",
                "resource": "GPU 10",
                "conditionKey": "gpu_temperature:GPU-one",
                "value": 85,
                "threshold": 80,
            },
            self.gpu_server(),
        )
        self.assertEqual(diagnosis["targetGpuIndex"], 1)

    def test_unmatched_gpu_resource_targets_no_gpu(self) -> None:
        diagnosis = diagnose_condition(
            {
                "category": "gpu_memory",
                "resource": "GPU 7 VRAM",
                "conditionKey": "gpu_memory:GPU-absent",
                "value": 95,
                "threshold": 90,
            },
            self.gpu_server(),
        )
        self.assertIsNone(diagnosis["targetGpuIndex"])


if __name__ == "__main__":
    unittest.main()
