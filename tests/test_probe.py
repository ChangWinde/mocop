from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mocop.config import MonitorConfig
from mocop.probe import (
    OpenSshLinuxResourceProbe,
    OpenSshNvidiaSmiProbe,
    _BoundedProcessResult,
    _ProcessOutputLimitExceeded,
    _run_bounded_process,
    parse_linux_resource_payload,
    parse_nvidia_processes_csv,
    parse_nvidia_smi_csv,
)


def config() -> MonitorConfig:
    return MonitorConfig(
        ssh_config=Path("/tmp/ssh-config"),
        auto_discover=False,
        hosts=("gpu-1",),
        exclude_hosts=frozenset(),
        poll_interval_seconds=5,
        probe_timeout_seconds=12,
        connect_timeout_seconds=5,
        max_workers=1,
        listen_host="127.0.0.1",
        listen_port=8787,
    )


def resource_payload(
    *,
    cpu_total: int = 1000,
    cpu_idle: int = 800,
    rx_bytes: int = 10000,
    tx_bytes: int = 20000,
    disk_read_bytes: int = 30000,
    disk_write_bytes: int = 40000,
    gpu_payload: str = (
        "0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 93, 34, "
        "81920, 40960, 40960, 287.5, 400"
    ),
    process_payload: str = "GPU-abc, 4242, python, 2048",
) -> str:
    return (
        "MONITOR_V3\n"
        "HOST\tnode-a\n"
        f"CPU\t{cpu_total}\t{cpu_idle}\n"
        "CORES\t8\n"
        "MEM\t16384000\t8192000\t2097152\t1048576\n"
        "LOAD\t1.5\t1.0\t0.5\n"
        "UPTIME\t86400.5\n"
        f"NET\t{rx_bytes}\t{tx_bytes}\n"
        f"IO\t{disk_read_bytes}\t{disk_write_bytes}\n"
        "DISKS_BEGIN\n"
        "DISK\t/dev/sda1\text4\t104857600\t52428800\t52428800\t50\t/\n"
        "DISKS_END\n"
        "GPUS_BEGIN\n"
        f"{gpu_payload}\n"
        "GPUS_END\n"
        "PROCESSES_BEGIN\n"
        f"{process_payload}\n"
        "PROCESSES_END\n"
    )


class ProbeTests(unittest.TestCase):
    def test_parses_complete_and_unavailable_values(self) -> None:
        payload = (
            '0, GPU-abc, "NVIDIA A100, Special", 550.54, P0, 61, 93, 34, '
            "81920, 40960, 40960, 287.5, 400\n"
            "1, GPU-def, NVIDIA A100, 550.54, [N/A], [N/A], 0, 0, "
            "81920, 0, 81920, [N/A], [N/A]\n"
        )
        gpus = parse_nvidia_smi_csv(payload)
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0].name, "NVIDIA A100, Special")
        self.assertEqual(gpus[0].utilization_gpu_pct, 93)
        self.assertIsNone(gpus[1].temperature_c)
        self.assertIsNone(gpus[1].power_draw_w)

    def test_parses_versioned_system_resource_contract(self) -> None:
        raw, gpus, gpu_message = parse_linux_resource_payload(resource_payload())
        self.assertEqual(raw.hostname, "node-a")
        self.assertEqual(raw.cpu_cores, 8)
        self.assertEqual(raw.memory_total_kib, 16384000)
        self.assertEqual(raw.swap_free_kib, 1048576)
        self.assertEqual(raw.disk_read_bytes, 30000)
        self.assertEqual(raw.disks[0].mountpoint, "/")
        self.assertEqual(raw.disks[0].used_pct, 50)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].processes[0].pid, 4242)
        self.assertEqual(gpus[0].processes[0].used_memory_mib, 2048)
        self.assertIsNone(gpu_message)

    def test_parses_and_bounds_gpu_compute_processes(self) -> None:
        processes = parse_nvidia_processes_csv(
            'GPU-abc, 42, "python, trainer.py", 1024\n'
            "GPU-abc, 43, inference-server, [N/A]\n"
        )

        self.assertEqual([process.pid for process in processes["GPU-abc"]], [42, 43])
        self.assertEqual(processes["GPU-abc"][0].name, "python, trainer.py")
        self.assertIsNone(processes["GPU-abc"][1].used_memory_mib)

        with self.assertRaisesRegex(ValueError, "process PID"):
            parse_nvidia_processes_csv("GPU-abc, 0, python, 10")

    def test_rejects_unknown_resource_protocol(self) -> None:
        with self.assertRaisesRegex(ValueError, "protocol version"):
            parse_linux_resource_payload("MONITOR_V999\n")

    def test_rejects_incomplete_metric_sections(self) -> None:
        for marker in (
            "DISKS_BEGIN\n",
            "DISKS_END\n",
            "GPUS_BEGIN\n",
            "GPUS_END\n",
            "PROCESSES_BEGIN\n",
            "PROCESSES_END\n",
        ):
            with (
                self.subTest(marker=marker),
                self.assertRaisesRegex(ValueError, "section"),
            ):
                parse_linux_resource_payload(resource_payload().replace(marker, ""))

    def test_rejects_invalid_or_excessive_gpu_records(self) -> None:
        invalid = (
            "0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 101, 34, "
            "81920, 40960, 40960, 287.5, 400"
        )
        with self.assertRaisesRegex(ValueError, "GPU utilization"):
            parse_nvidia_smi_csv(invalid)

        row = (
            "0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 93, 34, "
            "81920, 40960, 40960, 287.5, 400"
        )
        with self.assertRaisesRegex(ValueError, "too many GPU records"):
            parse_nvidia_smi_csv("\n".join([row] * 257))

    @patch("mocop.probe._run_bounded_process")
    def test_uses_argv_and_strict_host_key_checking(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0, stdout=resource_payload(), stderr=""
        )
        result = OpenSshNvidiaSmiProbe().probe("gpu-1", config())
        self.assertEqual(result.status, "online")
        self.assertEqual(result.system.hostname, "node-a")
        arguments = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", arguments)
        self.assertIn("BatchMode=yes", arguments)
        self.assertEqual(arguments[arguments.index("--") + 1], "gpu-1")
        self.assertEqual(arguments[-2:], ["sh", "-s"])
        self.assertIn("MONITOR_V3", run.call_args.kwargs["input_text"])
        self.assertIn("--query-compute-apps", run.call_args.kwargs["input_text"])
        self.assertIn("/proc/meminfo", run.call_args.kwargs["input_text"])
        self.assertEqual(run.call_args.kwargs["max_output_bytes"], 2_097_152)
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("mocop.probe._run_bounded_process")
    def test_local_host_uses_the_fixed_probe_without_ssh(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0, stdout=resource_payload(), stderr=""
        )

        result = OpenSshLinuxResourceProbe().probe(
            "star-0", replace(config(), hosts=("star-0",), local_host="star-0")
        )

        self.assertEqual(result.status, "online")
        self.assertEqual(run.call_args.args[0], ["sh", "-s"])

    @patch("mocop.probe._run_bounded_process", side_effect=OSError)
    def test_local_host_reports_local_probe_start_failure(self, _run) -> None:
        result = OpenSshLinuxResourceProbe().probe(
            "star-0", replace(config(), hosts=("star-0",), local_host="star-0")
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.message, "Local resource probe could not be started")

    @patch("mocop.probe.time.monotonic")
    @patch("mocop.probe._run_bounded_process")
    def test_calculates_cpu_and_network_rates_between_samples(
        self, run, monotonic
    ) -> None:
        run.side_effect = [
            _BoundedProcessResult(0, resource_payload(), ""),
            _BoundedProcessResult(
                0,
                resource_payload(
                    cpu_total=1500,
                    cpu_idle=1000,
                    rx_bytes=16000,
                    tx_bytes=32000,
                    disk_read_bytes=48000,
                    disk_write_bytes=70000,
                ),
                "",
            ),
        ]
        monotonic.side_effect = [0, 1, 6, 7]
        probe = OpenSshLinuxResourceProbe()

        first = probe.probe("gpu-1", config())
        second = probe.probe("gpu-1", config())

        self.assertIsNone(first.system.cpu_usage_pct)
        self.assertEqual(second.system.cpu_usage_pct, 60)
        self.assertEqual(second.system.network_rx_bps, 1000)
        self.assertEqual(second.system.network_tx_bps, 2000)
        self.assertEqual(second.system.disk_read_bps, 3000)
        self.assertEqual(second.system.disk_write_bps, 5000)

    @patch("mocop.probe._run_bounded_process")
    def test_system_stays_online_when_gpu_tool_is_unavailable(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0, resource_payload(gpu_payload="GPU_UNAVAILABLE"), ""
        )
        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())
        self.assertEqual(result.status, "online")
        self.assertEqual(result.gpus, ())
        self.assertEqual(result.message, "nvidia-smi is unavailable")

    def test_rejects_injected_alias_before_process_start(self) -> None:
        with patch("mocop.probe._run_bounded_process") as run:
            with self.assertRaisesRegex(ValueError, "unsafe SSH alias"):
                OpenSshNvidiaSmiProbe().probe("host; touch /tmp/bad", config())
            run.assert_not_called()

    @patch("mocop.probe._run_bounded_process")
    def test_does_not_expose_ssh_stderr(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            255, stdout="", stderr="secret-user@192.0.2.4 private/key/path"
        )
        result = OpenSshNvidiaSmiProbe().probe("gpu-1", config())
        self.assertEqual(result.status, "unreachable")
        self.assertEqual(result.message, "SSH connection failed")
        self.assertNotIn("secret-user", result.message or "")

    @patch("mocop.probe._run_bounded_process")
    def test_classifies_safe_ssh_failure_reason(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            255, stdout="", stderr="user@192.0.2.4: Permission denied (publickey)"
        )
        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())
        self.assertEqual(result.message, "SSH authentication failed")
        self.assertNotIn("192.0.2.4", result.message or "")

    def test_bounded_process_captures_output_without_shell(self) -> None:
        result = _run_bounded_process(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            input_text="monitor-input",
            timeout_seconds=2,
            max_output_bytes=65_536,
            environment=os.environ.copy(),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "monitor-input\n")
        self.assertEqual(result.stderr, "")

    def test_bounded_process_rejects_excess_remote_output(self) -> None:
        with self.assertRaises(_ProcessOutputLimitExceeded):
            _run_bounded_process(
                [sys.executable, "-c", "print('x' * 100000)"],
                input_text="",
                timeout_seconds=2,
                max_output_bytes=65_536,
                environment=os.environ.copy(),
            )

    def test_bounded_process_enforces_total_timeout(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                input_text="",
                timeout_seconds=0.05,
                max_output_bytes=65_536,
                environment=os.environ.copy(),
            )

    @patch(
        "mocop.probe._run_bounded_process",
        side_effect=_ProcessOutputLimitExceeded,
    )
    def test_reports_oversized_remote_output_without_exposing_it(self, _run) -> None:
        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())

        self.assertEqual(result.status, "error")
        self.assertEqual(
            result.message, "Remote resource output exceeded the configured limit"
        )


if __name__ == "__main__":
    unittest.main()
