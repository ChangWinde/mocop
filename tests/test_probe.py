from __future__ import annotations

import os
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch

from mocop.config import HostOverrideConfig, MonitorConfig
from mocop.models import GpuHealthMetrics, GpuMetrics
from mocop.probe import (
    OpenSshLinuxResourceProbe,
    _ActiveProcessRegistry,
    _BoundedProcessResult,
    _ProcessCancelled,
    _ProcessOutputLimitExceeded,
    _remote_script,
    _run_bounded_process,
    parse_linux_resource_payload,
    parse_nvidia_combined_csv,
    parse_nvidia_health_csv,
    parse_nvidia_processes_csv,
    parse_nvidia_smi_csv,
    parse_workload_records,
)
from mocop.remote_script import _CONTAINER_IDENTITY_AWK


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
    protocol: str = "MONITOR_V8",
    cpu_total: int = 1000,
    cpu_idle: int = 800,
    rx_bytes: int = 10000,
    tx_bytes: int = 20000,
    disk_read_bytes: int = 30000,
    disk_write_bytes: int = 40000,
    psi_payload: str = "",
    gpu_payload: str = (
        "0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 93, 34, "
        "81920, 40960, 40960, 287.5, 400"
    ),
    process_payload: str = "GPU-abc, 4242, python, 2048",
    health_payload: str = "GPU-abc, 0, No, No, Not Active, Not Active, Disabled",
    workload_payload: str = "",
) -> str:
    psi_lines = f"{psi_payload}\n" if psi_payload else ""
    return (
        f"{protocol}\n"
        "HOST\tnode-a\n"
        f"{psi_lines}"
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
        "WORKLOADS_BEGIN\n"
        f"{workload_payload}\n"
        "WORKLOADS_END\n"
        "GPU_HEALTH_BEGIN\n"
        f"{health_payload}\n"
        "GPU_HEALTH_END\n"
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
        self.assertEqual(
            gpus[0].health,
            GpuHealthMetrics(
                ecc_uncorrected_volatile=0,
                retired_pages_pending=False,
                remapped_rows_pending=False,
                thermal_slowdown=False,
                power_brake_slowdown=False,
                mig_mode="Disabled",
            ),
        )
        self.assertIsNone(gpu_message)

    def test_parses_gpu_health_and_isolates_optional_query_failure(self) -> None:
        health = parse_nvidia_health_csv(
            "GPU-a, 2, Yes, No, Active, Not Active, Enabled\n"
            "GPU-b, [N/A], [N/A], [N/A], [N/A], [N/A], [N/A]\n"
        )
        self.assertEqual(health["GPU-a"].ecc_uncorrected_volatile, 2)
        self.assertTrue(health["GPU-a"].retired_pages_pending)
        self.assertTrue(health["GPU-a"].thermal_slowdown)
        self.assertIsNone(health["GPU-b"].mig_mode)

        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(health_payload="GPU_HEALTH_ERROR\t2")
        )
        self.assertIsNone(gpus[0].health)

    def test_parses_combined_gpu_and_health_query(self) -> None:
        combined = (
            "0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 93, 34, "
            "81920, 40960, 40960, 287.5, 400, "
            "2, No, Yes, Active, Not Active, Enabled"
        )

        gpus, health = parse_nvidia_combined_csv(combined)

        self.assertEqual(gpus[0].uuid, "GPU-abc")
        self.assertEqual(gpus[0].utilization_gpu_pct, 93)
        self.assertEqual(health["GPU-abc"].ecc_uncorrected_volatile, 2)
        self.assertTrue(health["GPU-abc"].remapped_rows_pending)

        invalid_health = combined.replace(
            "2, No, Yes, Active, Not Active, Enabled",
            "2, Maybe, Yes, Active, Not Active, Enabled",
        )
        base_gpus, ignored_health = parse_nvidia_combined_csv(invalid_health)
        self.assertEqual(base_gpus[0].uuid, "GPU-abc")
        self.assertEqual(ignored_health, {})

        duplicate_health = (
            combined + "\n" + combined.replace("0, GPU-abc", "1, GPU-abc")
        )
        duplicate_gpus, ignored_duplicate_health = parse_nvidia_combined_csv(
            duplicate_health
        )
        self.assertEqual(len(duplicate_gpus), 2)
        self.assertEqual(ignored_duplicate_health, {})

        _, parsed, _ = parse_linux_resource_payload(
            resource_payload(gpu_payload=combined, health_payload="")
        )
        self.assertEqual(parsed[0].health, health["GPU-abc"])

        with self.assertRaisesRegex(ValueError, "combined GPU columns"):
            parse_nvidia_combined_csv(combined + ", unexpected")

        with self.assertRaisesRegex(ValueError, "health boolean"):
            parse_nvidia_health_csv(
                "GPU-a, 0, Maybe, No, Not Active, Not Active, Disabled"
            )
        with self.assertRaisesRegex(ValueError, "duplicate GPU health"):
            parse_nvidia_health_csv(
                "GPU-a, 0, No, No, Not Active, Not Active, Disabled\n"
                "GPU-a, 0, No, No, Not Active, Not Active, Disabled"
            )

        duplicate_index = combined + "\n" + combined.replace("GPU-abc", "GPU-def")
        with self.assertRaisesRegex(ValueError, "duplicate GPU indices"):
            parse_nvidia_combined_csv(duplicate_index)

        base_row = combined.rsplit(", ", 6)[0]
        with self.assertRaisesRegex(ValueError, "duplicate GPU indices"):
            parse_nvidia_smi_csv(base_row + "\n" + base_row)

        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(
                health_payload=(
                    "GPU-abc, 0, Maybe, No, Not Active, Not Active, Disabled"
                )
            )
        )
        self.assertIsNone(gpus[0].health)

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

    def test_parses_an_explicitly_skipped_process_sample(self) -> None:
        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(
                protocol="MONITOR_V8",
                process_payload="PROCESS_SKIPPED",
            )
        )

        self.assertFalse(gpus[0].processes_sampled)
        self.assertTrue(gpus[0].processes_available)
        self.assertEqual(gpus[0].processes, ())

        with self.assertRaisesRegex(ValueError, "conflicting process telemetry"):
            parse_linux_resource_payload(
                resource_payload(
                    protocol="MONITOR_V8",
                    process_payload=("PROCESS_SKIPPED\nGPU-abc, 4242, python, 2048"),
                )
            )
        # The script and parser ship together, so retired protocol versions
        # are rejected outright instead of being half-supported.
        for retired in ("MONITOR_V7", "MONITOR_V6", "MONITOR_V5", "MONITOR_V4"):
            with self.assertRaisesRegex(ValueError, "protocol version"):
                parse_linux_resource_payload(resource_payload(protocol=retired))

    def test_maps_gpu_processes_to_read_only_slurm_and_kubernetes_metadata(
        self,
    ) -> None:
        workloads = parse_workload_records(
            "WORKLOAD\t4242\tslurm\t9182\ttrain-llm\talice\tgpu-long\t\t\t\n"
            "WORKLOAD\t4243\tkubernetes\tpod-uid\tinference\t1001\tbatch\tml\t\t"
        )

        self.assertEqual(workloads[4242].kind, "slurm")
        self.assertEqual(workloads[4242].queue, "gpu-long")
        self.assertEqual(workloads[4243].kind, "kubernetes")
        self.assertEqual(workloads[4243].namespace, "ml")

        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(
                workload_payload=(
                    "WORKLOAD\t4242\tslurm\t9182\ttrain-llm\talice\tgpu-long\t\t\t"
                )
            )
        )
        self.assertEqual(gpus[0].processes[0].workload, workloads[4242])

        with self.assertRaisesRegex(ValueError, "workload kind"):
            parse_workload_records("WORKLOAD\t4242\troot-shell\t1\tbad\troot\t\t\t\t")
        # Legacy eight-column records died with the retired protocols.
        with self.assertRaisesRegex(ValueError, "invalid workload record"):
            parse_workload_records(
                "WORKLOAD\t4242\tslurm\t9182\ttrain-llm\talice\tgpu-long\t"
            )

    def test_maps_container_runtime_workload_kinds(self) -> None:
        workloads = parse_workload_records(
            "WORKLOAD\t4242\tdocker\tdeadbeef1234\t\talice\t\t\t1754000000\tpython\n"
            "WORKLOAD\t4243\tpodman\tcafebabe5678\t\tbob\t\t\t\t"
        )

        self.assertEqual(workloads[4242].kind, "docker")
        self.assertEqual(workloads[4242].workload_id, "deadbeef1234")
        self.assertEqual(workloads[4242].owner, "alice")
        self.assertEqual(workloads[4243].kind, "podman")
        self.assertEqual(workloads[4243].workload_id, "cafebabe5678")

    def test_parses_pressure_stall_records(self) -> None:
        raw, _, _ = parse_linux_resource_payload(
            resource_payload(
                psi_payload=(
                    "PSI\tcpu\t1.5\t0.8\t\t\n"
                    "PSI\tmemory\t12.25\t8.5\t3.75\t2.1\n"
                    "PSI\tio\t45\t30.5\t20\t15"
                )
            )
        )
        pressure = raw.pressure
        self.assertIsNotNone(pressure)
        self.assertEqual(pressure.cpu.some_avg10, 1.5)
        self.assertEqual(pressure.cpu.some_avg60, 0.8)
        # The kernel omits the CPU full line on older releases.
        self.assertIsNone(pressure.cpu.full_avg10)
        self.assertIsNone(pressure.cpu.full_avg60)
        self.assertEqual(pressure.memory.some_avg60, 8.5)
        self.assertEqual(pressure.memory.full_avg10, 3.75)
        self.assertEqual(pressure.io.some_avg10, 45)
        self.assertEqual(pressure.io.full_avg60, 15)

        # Kernels without CONFIG_PSI emit nothing: pressure stays unknown.
        raw, _, _ = parse_linux_resource_payload(resource_payload())
        self.assertIsNone(raw.pressure)

    def test_rejects_malformed_pressure_records(self) -> None:
        malformed = (
            "PSI\tmemory\t1.0\t2.0",  # missing full columns
            "PSI\tswap\t1.0\t2.0\t\t",  # unknown resource
            "PSI\tmemory\t\t2.0\t\t",  # missing required some average
            "PSI\tmemory\t101\t2.0\t\t",  # out of the percentage range
            "PSI\tmemory\t-1\t2.0\t\t",  # negative average
            "PSI\tmemory\t1.0\t2.0\tabc\t",  # non-numeric full average
            "PSI\tmemory\t1.0\t2.0\t\t\nPSI\tmemory\t1.0\t2.0\t\t",  # duplicate
        )
        for psi_payload in malformed:
            with (
                self.subTest(psi_payload=psi_payload),
                self.assertRaisesRegex(ValueError, "pressure"),
            ):
                parse_linux_resource_payload(resource_payload(psi_payload=psi_payload))

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
            "GPU_HEALTH_BEGIN\n",
            "GPU_HEALTH_END\n",
            "WORKLOADS_BEGIN\n",
            "WORKLOADS_END\n",
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

        rows = [
            (
                f"{index}, GPU-{index}, NVIDIA A100, 550.54, P0, 61, 93, 34, "
                "81920, 40960, 40960, 287.5, 400"
            )
            for index in range(257)
        ]
        with self.assertRaisesRegex(ValueError, "too many GPU records"):
            parse_nvidia_smi_csv("\n".join(rows))

    @patch("mocop.probe._run_bounded_process")
    def test_uses_argv_and_strict_host_key_checking(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0, stdout=resource_payload(), stderr=""
        )
        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())
        self.assertEqual(result.status, "online")
        self.assertEqual(result.system.hostname, "node-a")
        arguments = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", arguments)
        self.assertIn("BatchMode=yes", arguments)
        self.assertIn("ServerAliveInterval=2", arguments)
        self.assertIn("ServerAliveCountMax=2", arguments)
        self.assertEqual(arguments[arguments.index("--") + 1], "gpu-1")
        self.assertEqual(arguments[-2:], ["sh", "-s"])
        self.assertIn("MONITOR_V8", run.call_args.kwargs["input_text"])
        self.assertIn("--query-compute-apps", run.call_args.kwargs["input_text"])
        self.assertIn(
            "ecc.errors.uncorrected.volatile.total",
            run.call_args.kwargs["input_text"],
        )
        self.assertIn(
            "power.limit,ecc.errors.uncorrected.volatile.total",
            run.call_args.kwargs["input_text"],
        )
        self.assertNotIn(
            "--query-gpu=uuid,ecc.errors.uncorrected.volatile.total",
            run.call_args.kwargs["input_text"],
        )
        self.assertIn("/proc/meminfo", run.call_args.kwargs["input_text"])
        self.assertIn("/proc/pressure/memory", run.call_args.kwargs["input_text"])
        self.assertEqual(run.call_args.kwargs["max_output_bytes"], 2_097_152)
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("mocop.probe._run_bounded_process")
    def test_workload_metadata_is_explicitly_enabled_and_read_only(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0, stdout=resource_payload(), stderr=""
        )
        enabled = replace(config(), workloads=replace(config().workloads, mode="auto"))

        OpenSshLinuxResourceProbe().probe("gpu-1", enabled)

        script = run.call_args.kwargs["input_text"]
        self.assertIn("workload_tier=2", script)
        self.assertIn("/proc/$process_pid/environ", script)
        self.assertIn("od -An -v -tx1", script)
        self.assertIn("function environment_record", script)
        self.assertNotIn('RS = "\\0"', script)
        self.assertNotIn("tr '\\000' '\\n'", script)
        self.assertIn("function valid_container_id", script)
        self.assertIn("detect_container(cgroup)", script)
        self.assertNotIn("{12,64}", script)
        self.assertNotIn("scontrol", script)
        self.assertNotIn("kubectl", script)

    def test_container_detection_is_posix_awk_portable_and_segment_bounded(
        self,
    ) -> None:
        program = (
            _CONTAINER_IDENTITY_AWK + '\nBEGIN { detect_container(ENVIRON["CGROUP"]); '
            'printf "%s\\t%s\\n", container_kind, container_id }'
        )

        def detected(cgroup: str) -> str:
            environment = os.environ.copy()
            environment["CGROUP"] = cgroup
            completed = subprocess.run(
                ["awk", program],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
                timeout=2,
            )
            return completed.stdout.strip()

        twelve = "0123456789ab"
        sixty_four = "0123456789abcdef" * 4
        self.assertEqual(
            detected(f"0::/system.slice/docker-{twelve}.scope"),
            f"docker\t{twelve}",
        )
        self.assertEqual(
            detected(f"5:cpu:/docker/{sixty_four}"), f"docker\t{sixty_four}"
        )
        self.assertEqual(
            detected(f"0::/machine.slice/libpod-{twelve}.scope"),
            f"podman\t{twelve}",
        )
        for lookalike in (
            f"0::/docker/{twelve[:-1]}",
            f"0::/docker/{sixty_four}0",
            f"0::/docker/{twelve}z",
            f"0::/docker-{twelve}xyz.scope",
            f"0::/prefixdocker-{twelve}.scope",
            f"0::/libpod-{twelve}.scope-extra",
        ):
            self.assertEqual(detected(lookalike), "")

    @patch("mocop.probe._run_bounded_process")
    def test_identity_workload_mode_renders_the_light_tier(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0, stdout=resource_payload(), stderr=""
        )
        enabled = replace(
            config(), workloads=replace(config().workloads, mode="identity")
        )

        OpenSshLinuxResourceProbe().probe("gpu-1", enabled)

        script = run.call_args.kwargs["input_text"]
        self.assertIn("workload_tier=1", script)
        self.assertIn("/proc/$process_pid/cmdline", script)
        self.assertIn("/proc/$process_pid/stat", script)

    @patch("mocop.probe._run_bounded_process")
    def test_uses_per_host_probe_timeout(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0, stdout=resource_payload(), stderr=""
        )
        overridden = replace(
            config(),
            host_overrides=(("gpu-1", HostOverrideConfig(probe_timeout_seconds=20)),),
        )

        OpenSshLinuxResourceProbe().probe("gpu-1", overridden)

        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 20)

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

    @patch(
        "mocop.probe.utc_now",
        side_effect=(
            "2026-08-11T00:00:00Z",
            "2026-08-11T00:00:05Z",
            "2026-08-11T00:00:10Z",
            "2026-08-11T00:00:15Z",
            "2026-08-11T00:00:20Z",
            "2026-08-11T00:00:25Z",
        ),
    )
    @patch(
        "mocop.probe.time.monotonic",
        side_effect=(0, 0.1, 5, 5.1, 10, 10.1, 15, 15.1, 20, 20.1, 25, 25.1),
    )
    @patch("mocop.probe._run_bounded_process")
    def test_samples_gpu_processes_once_per_independent_cadence(
        self,
        run,
        _monotonic,
        _utc_now,
    ) -> None:
        due_processes = iter((4242, 4343))

        def execute(_command, **kwargs):
            script = kwargs["input_text"]
            if "process_enabled=1" in script:
                pid = next(due_processes)
                payload = resource_payload(
                    protocol="MONITOR_V8",
                    process_payload=f"GPU-abc, {pid}, python, 2048",
                )
            else:
                payload = resource_payload(
                    protocol="MONITOR_V8",
                    process_payload="PROCESS_SKIPPED",
                )
            return _BoundedProcessResult(0, payload, "")

        run.side_effect = execute
        probe = OpenSshLinuxResourceProbe()

        results = [probe.probe("gpu-1", config()) for _ in range(6)]

        scripts = [item.kwargs["input_text"] for item in run.call_args_list]
        self.assertEqual(
            ["process_enabled=1" in script for script in scripts],
            [True, False, False, True, False, False],
        )
        self.assertEqual(
            [result.gpus[0].processes[0].pid for result in results],
            [4242, 4242, 4242, 4343, 4343, 4343],
        )
        self.assertEqual(
            [result.gpus[0].processes_sampled for result in results],
            [True, False, False, True, False, False],
        )
        self.assertEqual(
            [result.gpus[0].processes_observed_at for result in results],
            [
                "2026-08-11T00:00:00Z",
                "2026-08-11T00:00:00Z",
                "2026-08-11T00:00:00Z",
                "2026-08-11T00:00:15Z",
                "2026-08-11T00:00:15Z",
                "2026-08-11T00:00:15Z",
            ],
        )
        baseline_queries = len(results) * 2
        optimized_queries = len(results) + sum(
            "process_enabled=1" in script for script in scripts
        )
        self.assertEqual(optimized_queries, 8)
        self.assertAlmostEqual(1 - optimized_queries / baseline_queries, 1 / 3)

    @patch(
        "mocop.probe.utc_now",
        side_effect=(
            "2026-08-11T00:00:00Z",
            "2026-08-11T00:00:15Z",
            "2026-08-11T00:00:20Z",
        ),
    )
    @patch(
        "mocop.probe.time.monotonic",
        side_effect=(0, 0.1, 15, 15.1, 20, 20.1),
    )
    @patch("mocop.probe._run_bounded_process")
    def test_retries_an_unavailable_process_query_on_the_next_core_sample(
        self,
        run,
        _monotonic,
        _utc_now,
    ) -> None:
        run.side_effect = (
            _BoundedProcessResult(
                0,
                resource_payload(protocol="MONITOR_V8"),
                "",
            ),
            _BoundedProcessResult(
                0,
                resource_payload(
                    protocol="MONITOR_V8",
                    process_payload="PROCESS_ERROR\t1",
                ),
                "",
            ),
            _BoundedProcessResult(
                0,
                resource_payload(
                    protocol="MONITOR_V8",
                    process_payload="GPU-abc, 4343, python, 1024",
                ),
                "",
            ),
        )
        probe = OpenSshLinuxResourceProbe()

        results = [probe.probe("gpu-1", config()) for _ in range(3)]

        self.assertTrue(
            all(
                "process_enabled=1" in item.kwargs["input_text"]
                for item in run.call_args_list
            )
        )
        self.assertEqual(
            [result.gpus[0].processes_available for result in results],
            [True, False, True],
        )
        self.assertEqual(
            [result.gpus[0].processes_observed_at for result in results],
            ["2026-08-11T00:00:00Z", None, "2026-08-11T00:00:20Z"],
        )

    @patch("mocop.probe.time.monotonic", side_effect=(0.0, 1.0, 6.0, 7.0))
    @patch("mocop.probe._run_bounded_process")
    def test_removed_host_does_not_reuse_an_old_rate_baseline(
        self, run, _monotonic
    ) -> None:
        run.side_effect = (
            _BoundedProcessResult(0, resource_payload(), ""),
            _BoundedProcessResult(
                0,
                resource_payload(
                    cpu_total=1500,
                    cpu_idle=1000,
                    rx_bytes=16000,
                    tx_bytes=32000,
                ),
                "",
            ),
        )
        probe = OpenSshLinuxResourceProbe()

        first = probe.probe("gpu-1", config())
        probe.retain_hosts(set())
        readded = probe.probe("gpu-1", config())

        self.assertIsNone(first.system.cpu_usage_pct)
        self.assertIsNone(readded.system.cpu_usage_pct)
        self.assertIsNone(readded.system.network_rx_bps)

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
                OpenSshLinuxResourceProbe().probe("host; touch /tmp/bad", config())
            run.assert_not_called()

    @patch("mocop.probe._run_bounded_process")
    def test_does_not_expose_ssh_stderr(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            255, stdout="", stderr="secret-user@192.0.2.4 private/key/path"
        )
        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())
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

    @patch("mocop.probe.time.monotonic", side_effect=(0.0, 1.0, 2.0))
    @patch("mocop.probe._run_bounded_process")
    def test_retries_stale_multiplexed_connection_within_total_timeout(
        self, run, _monotonic
    ) -> None:
        run.side_effect = (
            _BoundedProcessResult(
                255,
                stdout="",
                stderr="mux_client_request_session: read from master failed: Broken pipe",
            ),
            _BoundedProcessResult(0, stdout=resource_payload(), stderr=""),
        )

        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())

        self.assertEqual(result.status, "online")
        self.assertEqual(result.transport_retries, 1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["timeout_seconds"], 12)
        self.assertEqual(run.call_args_list[1].kwargs["timeout_seconds"], 11)

    @patch("mocop.probe.time.monotonic", side_effect=(0.0, 12.0, 12.0))
    @patch("mocop.probe._run_bounded_process")
    def test_does_not_retry_when_transport_failure_exhausts_timeout(
        self, run, _monotonic
    ) -> None:
        run.return_value = _BoundedProcessResult(
            255,
            stdout="",
            stderr="mux_client_request_session: Broken pipe",
        )

        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())

        self.assertEqual(result.status, "unreachable")
        self.assertEqual(result.transport_retries, 0)
        self.assertEqual(run.call_count, 1)

    @patch("mocop.probe.time.monotonic", side_effect=(0.0, 0.5, 16.0, 16.5, 31.0, 31.5))
    @patch("mocop.probe._run_bounded_process")
    def test_idle_host_stretches_the_process_cadence(self, run, _monotonic) -> None:
        idle_gpu = (
            "0, GPU-abc, NVIDIA A100, 550.54, P0, 35, 0, 0, "
            "81920, 2048, 79872, 60.0, 400"
        )

        def execute(_command, **kwargs):
            return _BoundedProcessResult(
                0,
                resource_payload(
                    protocol="MONITOR_V8",
                    gpu_payload=idle_gpu,
                    process_payload="",
                ),
                "",
            )

        run.side_effect = execute
        probe = OpenSshLinuxResourceProbe()

        for _ in range(3):
            self.assertEqual(probe.probe("gpu-1", config()).status, "online")

        scripts = [item.kwargs["input_text"] for item in run.call_args_list]
        # First sample collects processes and observes an idle device; the
        # base 12+ second point is stretched away; the doubled deadline
        # passes at 31 seconds and processes are collected again.
        self.assertEqual(
            [("process_enabled=1" in script) for script in scripts],
            [True, False, True],
        )

    @patch("mocop.probe.time.monotonic", side_effect=(0.0, 0.5, 5.0, 5.5, 16.0, 16.5))
    @patch("mocop.probe._run_bounded_process")
    def test_activity_hint_cancels_the_cadence_stretch(self, run, _monotonic) -> None:
        idle_gpu = (
            "0, GPU-abc, NVIDIA A100, 550.54, P0, 35, 0, 0, "
            "81920, 2048, 79872, 60.0, 400"
        )
        busy_gpu = (
            "0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 95, 34, "
            "81920, 40960, 40960, 287.5, 400"
        )
        payloads = iter((idle_gpu, busy_gpu, busy_gpu))

        def execute(_command, **kwargs):
            return _BoundedProcessResult(
                0,
                resource_payload(
                    protocol="MONITOR_V8",
                    gpu_payload=next(payloads),
                    process_payload="",
                ),
                "",
            )

        run.side_effect = execute
        probe = OpenSshLinuxResourceProbe()

        for _ in range(3):
            self.assertEqual(probe.probe("gpu-1", config()).status, "online")

        scripts = [item.kwargs["input_text"] for item in run.call_args_list]
        # The idle first sample starts a stretch; the busy core sample at
        # five seconds raises the activity hint, so the third probe returns
        # to the base fifteen-second cadence instead of waiting thirty.
        self.assertEqual(
            [("process_enabled=1" in script) for script in scripts],
            [True, False, True],
        )

    def test_process_cadence_stretch_is_bounded_and_pruned(self) -> None:
        from mocop.probe import _ProcessSample

        probe = OpenSshLinuxResourceProbe()
        sample = _ProcessSample(
            sampled_at_monotonic=0.0,
            observed_at="2026-08-11T00:00:00Z",
            workload_mode="disabled",
            processes_by_gpu={"GPU-abc": ()},
            idle_streak=5,
        )
        probe._process_samples["gpu-1"] = sample
        probe._activity_hints["gpu-1"] = False

        # Streak five is capped at a fourfold stretch: due at 60, not 480.
        self.assertFalse(probe._processes_due("gpu-1", 59.9, 15, "disabled"))
        self.assertTrue(probe._processes_due("gpu-1", 60.0, 15, "disabled"))

        probe._activity_hints["gpu-1"] = True
        self.assertTrue(probe._processes_due("gpu-1", 15.0, 15, "disabled"))

        probe.retain_hosts(set())
        self.assertEqual(probe._process_samples, {})
        self.assertEqual(probe._activity_hints, {})

    @patch("mocop.probe._run_bounded_process")
    def test_distinguishes_transport_silence_from_partial_output(self, run) -> None:
        run.side_effect = subprocess.TimeoutExpired(["ssh"], 12, output=b"")
        silent = OpenSshLinuxResourceProbe().probe("gpu-1", config())
        self.assertEqual(silent.status, "unreachable")
        self.assertEqual(
            silent.message, "SSH produced no output before the collection timeout"
        )

        run.side_effect = subprocess.TimeoutExpired(
            ["ssh"], 12, output=b"MONITOR_V8\nHOST\tnode-a\n"
        )
        stalled = OpenSshLinuxResourceProbe().probe("gpu-1", config())
        # Partial output proves the transport reached the host, so a remote
        # stall is an error rather than a connectivity ("unreachable") event.
        self.assertEqual(stalled.status, "error")
        self.assertEqual(
            stalled.message, "Remote collection stalled after partial output"
        )

        run.side_effect = subprocess.TimeoutExpired(
            ["ssh"], 12, output=b"", stderr=b"remote command started\n"
        )
        stderr_only = OpenSshLinuxResourceProbe().probe("gpu-1", config())
        self.assertEqual(stderr_only.status, "error")
        self.assertEqual(
            stderr_only.message, "Remote collection stalled after partial output"
        )

    @patch("mocop.probe._run_bounded_process")
    def test_classifies_dead_transport_keepalive_failure(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            255, stdout="", stderr="Timeout, server gpu-1 not responding."
        )
        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())
        self.assertEqual(result.status, "unreachable")
        self.assertEqual(result.message, "SSH transport stopped responding")

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

    def test_fixed_linux_script_collects_a_parseable_local_sample(self) -> None:
        completed = _run_bounded_process(
            ["sh", "-s"],
            input_text=_remote_script("disabled"),
            timeout_seconds=5,
            max_output_bytes=2_097_152,
            environment={**os.environ, "LC_ALL": "C"},
        )

        self.assertEqual(completed.returncode, 0)
        system, _gpus, _message = parse_linux_resource_payload(completed.stdout)
        self.assertGreater(system.cpu_cores, 0)
        self.assertGreater(system.memory_total_kib, 0)
        self.assertGreater(system.uptime_seconds, 0)

    def test_failed_gpu_query_discards_partial_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "nvidia-smi"
            executable.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                "  --query-gpu=*) printf '%s\\n' "
                "'0, GPU-partial, NVIDIA A100, 550.54, P0, 61, 93, 34, "
                "81920, 40960, 40960, 287.5, 400'; exit 9 ;;\n"
                "  --query-compute-apps=*) exit 9 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            completed = _run_bounded_process(
                ["sh", "-s"],
                input_text=_remote_script("disabled", True),
                timeout_seconds=5,
                max_output_bytes=2_097_152,
                environment={
                    **os.environ,
                    "LC_ALL": "C",
                    "PATH": f"{root}:{os.environ['PATH']}",
                },
            )

        self.assertEqual(completed.returncode, 0)
        system, gpus, message = parse_linux_resource_payload(completed.stdout)
        self.assertGreater(system.cpu_cores, 0)
        self.assertEqual(gpus, ())
        self.assertEqual(message, "nvidia-smi query failed")

    def test_fixed_script_omits_the_process_query_when_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "queries.log"
            executable = root / "nvidia-smi"
            executable.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$MOCOP_TEST_QUERY_LOG"\n'
                'case "$1" in\n'
                "  --query-gpu=*) printf '%s\\n' "
                "'0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 93, 34, "
                "81920, 40960, 40960, 287.5, 400, 0, No, No, "
                "Not Active, Not Active, Disabled' ;;\n"
                "  --query-compute-apps=*) printf '%s\\n' "
                "'GPU-abc, 4242, python, 2048' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            environment = {
                **os.environ,
                "LC_ALL": "C",
                "PATH": f"{root}:{os.environ['PATH']}",
                "MOCOP_TEST_QUERY_LOG": str(log),
            }

            sampled = _run_bounded_process(
                ["sh", "-s"],
                input_text=_remote_script("disabled", True),
                timeout_seconds=5,
                max_output_bytes=2_097_152,
                environment=environment,
            )
            skipped = _run_bounded_process(
                ["sh", "-s"],
                input_text=_remote_script("disabled", False),
                timeout_seconds=5,
                max_output_bytes=2_097_152,
                environment=environment,
            )

            _, sampled_gpus, _ = parse_linux_resource_payload(sampled.stdout)
            _, skipped_gpus, _ = parse_linux_resource_payload(skipped.stdout)
            queries = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(sampled_gpus[0].processes), 1)
            self.assertFalse(skipped_gpus[0].processes_sampled)
            self.assertEqual(
                sum(query.startswith("--query-gpu=") for query in queries),
                2,
            )
            self.assertEqual(
                sum(query.startswith("--query-compute-apps=") for query in queries),
                1,
            )

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

    def test_bounded_process_preserves_stderr_when_it_times_out(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired) as context:
            _run_bounded_process(
                [
                    sys.executable,
                    "-c",
                    "import sys,time; sys.stderr.write('started\\n'); "
                    "sys.stderr.flush(); time.sleep(2)",
                ],
                input_text="",
                timeout_seconds=0.1,
                max_output_bytes=65_536,
                environment=os.environ.copy(),
            )

        self.assertEqual(context.exception.output, b"")
        self.assertEqual(context.exception.stderr, b"started\n")

    def test_bounded_process_cancels_active_child_promptly(self) -> None:
        cancelled = threading.Event()
        timer = threading.Timer(0.05, cancelled.set)
        started = time.monotonic()
        timer.start()
        self.addCleanup(timer.cancel)

        with self.assertRaises(_ProcessCancelled):
            _run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                input_text="",
                timeout_seconds=5,
                max_output_bytes=65_536,
                environment=os.environ.copy(),
                cancel_event=cancelled,
            )

        self.assertLess(time.monotonic() - started, 1)

    @patch("mocop.probe._kill_process_group")
    def test_registry_cancellation_wakes_the_full_deadline_wait(self, _kill) -> None:
        # With the child kill suppressed, only the registry wake-up pipe can
        # interrupt the selector before the five-second deadline. The bounded
        # cleanup still terminates the real child after that behavior is proven
        # so the test cannot leak a subprocess into the rest of the suite.
        def kill_only_during_cleanup(process) -> None:
            if _kill.call_count > 1:
                process.kill()

        _kill.side_effect = kill_only_during_cleanup
        registry = _ActiveProcessRegistry()
        timer = threading.Timer(0.05, registry.cancel)
        started = time.monotonic()
        timer.start()
        self.addCleanup(timer.cancel)

        with self.assertRaises(_ProcessCancelled):
            _run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                input_text="",
                timeout_seconds=5,
                max_output_bytes=65_536,
                environment=os.environ.copy(),
                cancel_event=registry.cancelled,
                process_registry=registry,
            )

        self.assertLess(time.monotonic() - started, 2.5)

    @patch("mocop.probe._kill_process_group")
    def test_active_process_registry_kills_every_child_synchronously(
        self, kill
    ) -> None:
        registry = _ActiveProcessRegistry()
        first = object()
        second = object()
        late = object()
        self.assertTrue(registry.register(first))
        self.assertTrue(registry.register(second))

        registry.cancel()

        kill.assert_has_calls([call(first), call(second)], any_order=True)
        self.assertEqual(kill.call_count, 2)
        self.assertTrue(registry.cancelled.is_set())
        self.assertFalse(registry.register(late))

    def test_probe_cancel_terminates_an_active_child_immediately(self) -> None:
        probe = OpenSshLinuxResourceProbe()
        local_config = replace(
            config(), hosts=("star-0",), local_host="star-0", probe_timeout_seconds=5
        )
        entered_select = threading.Event()
        results = []
        original_select = selectors.DefaultSelector.select

        def observed_select(selector, timeout=None):
            entered_select.set()
            return original_select(selector, timeout)

        with (
            patch("mocop.probe._remote_script", return_value="sleep 5"),
            patch.object(selectors.DefaultSelector, "select", observed_select),
        ):
            worker = threading.Thread(
                target=lambda: results.append(probe.probe("star-0", local_config))
            )
            worker.start()
            self.assertTrue(entered_select.wait(1), "probe never entered process wait")
            probe.cancel()
            worker.join(1)

        self.assertTrue(
            not worker.is_alive(),
            "active probe did not stop after its process group was cancelled",
        )
        self.assertEqual(results[0].message, "Resource collection cancelled")

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

    @patch("mocop.probe.time.monotonic", side_effect=(0.0, 1.0, 2.0))
    @patch("mocop.probe._run_bounded_process")
    def test_transport_retry_forces_a_fresh_connection(self, run, _monotonic) -> None:
        run.side_effect = (
            _BoundedProcessResult(
                255, "", "mux_client_request_session: read from master failed: pipe"
            ),
            _BoundedProcessResult(0, resource_payload(), ""),
        )

        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())

        self.assertEqual(result.status, "online")
        self.assertEqual(result.transport_retries, 1)
        first_command = run.call_args_list[0].args[0]
        retry_command = run.call_args_list[1].args[0]
        self.assertNotIn("ControlMaster=no", first_command)
        self.assertIn("ControlMaster=no", retry_command)
        self.assertIn("ControlPath=none", retry_command)
        # The bypass options must precede the host/command separator.
        self.assertLess(
            retry_command.index("ControlPath=none"), retry_command.index("--")
        )

    @patch("mocop.probe.time.monotonic", side_effect=(0.0, 1.0))
    @patch("mocop.probe._run_bounded_process")
    def test_hard_ssh_failure_is_not_retried_despite_mux_marker(
        self, run, _monotonic
    ) -> None:
        run.return_value = _BoundedProcessResult(
            255,
            "",
            "mux_client_request_session: read from master: Permission denied",
        )

        result = OpenSshLinuxResourceProbe().probe("gpu-1", config())

        self.assertEqual(run.call_count, 1)
        self.assertEqual(result.transport_retries, 0)
        self.assertEqual(result.status, "unreachable")

    def test_transport_retry_classification_excludes_hard_failures(self) -> None:
        from mocop.probe import _is_retryable_ssh_transport_failure as retryable

        self.assertTrue(
            retryable("mux_client_request_session: read from master failed: pipe")
        )
        self.assertTrue(retryable("Control socket connect(/x): Connection refused"))
        self.assertTrue(retryable("ssh_exchange_identification: broken pipe"))
        self.assertFalse(
            retryable("mux_client_request_session: session open refused by peer")
        )
        self.assertFalse(retryable("user@host: Permission denied (publickey)"))
        self.assertFalse(retryable("Host key verification failed."))

    def test_bounded_process_survives_epipe_on_stdin_close(self) -> None:
        result = _run_bounded_process(
            [sys.executable, "-c", "import os, sys; os.close(0); sys.exit(7)"],
            input_text="x" * 2_000_000,
            timeout_seconds=5,
            max_output_bytes=65_536,
            environment=os.environ.copy(),
        )

        self.assertEqual(result.returncode, 7)

    def test_active_process_registry_closes_wakeup_pipe(self) -> None:
        registry = _ActiveProcessRegistry()
        read_fd = registry.cancel_wakeup
        write_fd = registry._cancel_write_fd
        self.assertTrue(registry._finalizer.alive)

        registry.close()

        self.assertFalse(registry._finalizer.alive)
        for fd in (read_fd, write_fd):
            with self.assertRaises(OSError):
                os.close(fd)
        registry.close()

    def test_unknown_gpu_metrics_count_as_activity(self) -> None:
        from mocop.probe import _gpu_activity

        thresholds = config().thresholds

        def gpu(**overrides: object) -> GpuMetrics:
            base = dict(
                index=0,
                uuid="GPU-x",
                name="NVIDIA A100",
                driver_version="550.54",
                pstate="P0",
                temperature_c=35.0,
                utilization_gpu_pct=0.0,
                utilization_memory_pct=0.0,
                memory_total_mib=81920.0,
                memory_used_mib=100.0,
                memory_free_mib=81820.0,
                power_draw_w=60.0,
                power_limit_w=400.0,
            )
            base.update(overrides)
            return GpuMetrics(**base)

        self.assertTrue(_gpu_activity((gpu(utilization_gpu_pct=None),), thresholds))
        self.assertTrue(_gpu_activity((gpu(memory_used_mib=None),), thresholds))
        self.assertTrue(_gpu_activity((gpu(memory_total_mib=None),), thresholds))
        self.assertFalse(_gpu_activity((gpu(),), thresholds))

    @patch(
        "mocop.probe.time.monotonic",
        side_effect=(0, 0.1, 5, 5.1, 10, 10.1, 15, 15.1),
    )
    @patch("mocop.probe._run_bounded_process")
    def test_activity_hint_latches_across_an_idle_core_sample(
        self, run, _monotonic
    ) -> None:
        idle_gpu = (
            "0, GPU-abc, NVIDIA A100, 550.54, P0, 35, 0, 0, "
            "81920, 2048, 79872, 60.0, 400"
        )
        busy_gpu = (
            "0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 95, 34, "
            "81920, 40960, 40960, 287.5, 400"
        )
        payloads = iter((idle_gpu, busy_gpu, idle_gpu, idle_gpu))

        def execute(_command, **kwargs):
            return _BoundedProcessResult(
                0,
                resource_payload(
                    protocol="MONITOR_V8",
                    gpu_payload=next(payloads),
                    process_payload="",
                ),
                "",
            )

        run.side_effect = execute
        probe = OpenSshLinuxResourceProbe()

        for _ in range(4):
            self.assertEqual(probe.probe("gpu-1", config()).status, "online")

        scripts = [item.kwargs["input_text"] for item in run.call_args_list]
        # The busy core sample at five seconds raises the hint; an idle sample
        # at ten must not clear it, so the fourth probe returns to the base
        # cadence at fifteen seconds instead of waiting for the stretch.
        self.assertEqual(
            [("process_enabled=1" in script) for script in scripts],
            [True, False, False, True],
        )

    def test_failed_process_query_forces_retry_before_stretched_deadline(self) -> None:
        probe = OpenSshLinuxResourceProbe()
        thresholds = config().thresholds
        idle_gpu = GpuMetrics(
            index=0,
            uuid="GPU-abc",
            name="NVIDIA A100",
            driver_version="550.54",
            pstate="P0",
            temperature_c=35.0,
            utilization_gpu_pct=0.0,
            utilization_memory_pct=0.0,
            memory_total_mib=81920.0,
            memory_used_mib=100.0,
            memory_free_mib=81820.0,
            power_draw_w=60.0,
            power_limit_w=400.0,
        )

        probe._merge_process_sample(
            "gpu-1",
            (idle_gpu,),
            process_sampled=True,
            processes_available=True,
            sampled_at_monotonic=0.0,
            observed_at="2026-08-11T00:00:00Z",
            workload_mode="disabled",
            thresholds=thresholds,
        )
        # The idle sample stretches the cadence: not due at ten seconds.
        self.assertFalse(probe._processes_due("gpu-1", 10.0, 15, "disabled"))

        probe._merge_process_sample(
            "gpu-1",
            (idle_gpu,),
            process_sampled=True,
            processes_available=False,
            sampled_at_monotonic=10.0,
            observed_at="2026-08-11T00:00:10Z",
            workload_mode="disabled",
            thresholds=thresholds,
        )
        # A failed query forces a retry on the very next core cycle.
        self.assertTrue(probe._processes_due("gpu-1", 11.0, 15, "disabled"))

    def test_unicode_line_boundaries_stay_within_a_field(self) -> None:
        for separator in ("\u2028", "\u2029", "\x85"):
            name = f"NVIDIA{separator}A100"
            gpu = (
                f'0, GPU-abc, "{name}", 550.54, P0, 61, 93, 34, '
                "81920, 40960, 40960, 287.5, 400"
            )
            _, gpus, _ = parse_linux_resource_payload(
                resource_payload(
                    protocol="MONITOR_V8",
                    gpu_payload=gpu,
                    process_payload="",
                )
            )
            self.assertEqual(len(gpus), 1)
            self.assertEqual(gpus[0].name, name)

    def test_csv_module_errors_classify_as_protocol_value_errors(self) -> None:
        # An oversized quoted field raises csv.Error on every interpreter and
        # must surface as the protocol ValueError instead of escaping the
        # collector's classification.
        oversized = '0, "' + "x" * 200_000 + '", n, d, P0, 61, 93, 34, 1, 1, 1, 1, 1'
        with self.assertRaisesRegex(ValueError, "unparseable CSV"):
            parse_nvidia_smi_csv(oversized)

        # An oversized field inside the process section degrades only that
        # view while the core sample stays online.
        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(
                protocol="MONITOR_V8",
                process_payload='GPU-abc, 4242, "' + "x" * 200_000 + '", 2048',
            )
        )
        self.assertEqual(len(gpus), 1)
        self.assertFalse(gpus[0].processes_available)

        # NUL handling differs by interpreter (older csv modules raise, newer
        # ones pass the byte through); both outcomes must stay within the
        # protocol's ValueError contract.
        with suppress(ValueError):
            parse_nvidia_smi_csv(
                "0, GPU-\x00abc, NVIDIA A100, 550.54, P0, 61, 93, 34, "
                "81920, 40960, 40960, 287.5, 400"
            )

    def test_container_root_is_collected_and_file_binds_are_ignored(self) -> None:
        # A container's overlay root is real capacity; the single-file mounts
        # a runtime injects report the host's filesystem and must not appear.
        payload = resource_payload(
            protocol="MONITOR_V8",
            process_payload="",
        ).replace(
            "DISK\t/dev/sda1\text4\t104857600\t52428800\t52428800\t50\t/\n",
            "DISK\toverlay\toverlay\t52428800\t50331648\t2097152\t95\t/\n"
            "DISK\t/dev/nvme0n1p2\text4\t1048576000\t367001600\t681574400\t35"
            "\t/etc/hosts\n"
            "DISK\t/dev/nvme0n1p3\text4\t2097152000\t901775360\t1195376640\t43"
            "\t/etc/hostname\n"
            "DISK\t100.1.2.3:/pvc\tnfs\t10485760000\t10276044800\t209715200\t99"
            "\t/data\n",
        )

        raw, _, _ = parse_linux_resource_payload(payload)

        mounts = [disk.mountpoint for disk in raw.disks]
        self.assertEqual(mounts, ["/", "/data"])
        root = raw.disks[0]
        self.assertEqual(root.filesystem_type, "overlay")
        self.assertEqual(root.used_pct, 95)
        self.assertAlmostEqual(root.available_mib, 2048.0, places=1)

    def test_disk_filter_keeps_container_roots_but_drops_docker_host_layers(
        self,
    ) -> None:
        # Run the real script against a stubbed df so the awk filter itself is
        # under test, not a copy of it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / "df"
            stub.write_text(
                "#!/bin/sh\n"
                "cat <<'ROWS'\n"
                "Filesystem Type 1024-blocks Used Available Capacity Mounted on\n"
                "overlay overlay 52428800 50331648 2097152 95% /\n"
                "overlay overlay 100 50 50 50% /var/lib/docker/overlay2/a/merged\n"
                "tmpfs tmpfs 100 50 50 50% /run\n"
                "/dev/nvme0n1p2 ext4 1048576000 367001600 681574400 35% /etc/hosts\n"
                "/dev/sda1 ext4 1000 500 500 50% /data\n"
                "ROWS\n",
                encoding="utf-8",
            )
            stub.chmod(0o700)
            completed = _run_bounded_process(
                ["sh", "-s"],
                input_text=_remote_script("disabled", False),
                timeout_seconds=10,
                max_output_bytes=2_097_152,
                environment={
                    **os.environ,
                    "LC_ALL": "C",
                    "PATH": f"{root}:{os.environ['PATH']}",
                },
            )

        self.assertEqual(completed.returncode, 0)
        emitted = [
            line.split("\t")[7]
            for line in completed.stdout.splitlines()
            if line.startswith("DISK\t")
        ]
        # The container root survives; the Docker host's per-container layer
        # and the pseudo filesystem do not.
        self.assertIn("/", emitted)
        self.assertIn("/data", emitted)
        self.assertNotIn("/var/lib/docker/overlay2/a/merged", emitted)
        self.assertNotIn("/run", emitted)

        raw, _, _ = parse_linux_resource_payload(completed.stdout)
        # The injected file bind mount is dropped during parsing.
        self.assertEqual(sorted(disk.mountpoint for disk in raw.disks), ["/", "/data"])

    def test_malformed_process_row_keeps_the_core_sample_online(self) -> None:
        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(
                protocol="MONITOR_V8",
                process_payload="GPU-abc, 0, python, 10",
            )
        )

        self.assertEqual(len(gpus), 1)
        self.assertFalse(gpus[0].processes_available)
        self.assertEqual(gpus[0].processes, ())

    def test_parses_v7_workload_records_with_start_and_command(self) -> None:
        workloads = parse_workload_records(
            "WORKLOAD\t4242\tslurm\t9182\ttrain-llm\talice\tgpu-long\t\t"
            "1767225600\tpython train.py --epochs 3\n"
            "WORKLOAD\t4243\tprocess\t\t\tbob\t\t\t\t"
        )

        self.assertEqual(workloads[4242].started_at, "2026-01-01T00:00:00Z")
        self.assertEqual(workloads[4242].command, "python train.py --epochs 3")
        self.assertEqual(workloads[4242].kind, "slurm")
        self.assertIsNone(workloads[4243].started_at)
        self.assertIsNone(workloads[4243].command)

        with self.assertRaisesRegex(ValueError, "workload start"):
            parse_workload_records("WORKLOAD\t1\tprocess\t\t\tx\t\t\tnot-a-number\tcmd")

    @patch(
        "mocop.probe.time.monotonic",
        side_effect=(0, 0.1, 15, 15.1, 16, 16.1),
    )
    @patch("mocop.probe._run_bounded_process")
    def test_unattended_probe_stretches_and_catches_up_on_return(
        self, run, _monotonic
    ) -> None:
        def execute(_command, **kwargs):
            return _BoundedProcessResult(0, resource_payload(), "")

        run.side_effect = execute
        probe = OpenSshLinuxResourceProbe()
        probe.set_attended(False)

        probe.probe("gpu-1", config())  # first sample always collects
        probe.probe("gpu-1", config())  # attended cadence would be due at 15s
        probe.set_attended(True)
        probe.probe("gpu-1", config())  # returning viewer forces a catch-up

        scripts = [item.kwargs["input_text"] for item in run.call_args_list]
        self.assertEqual(
            [("process_enabled=1" in script) for script in scripts],
            [True, False, True],
        )

    def test_malformed_workload_row_keeps_processes(self) -> None:
        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(
                protocol="MONITOR_V8",
                workload_payload="WORKLOAD\t4242\tbad-kind\t1\tx\troot\t\t",
            )
        )

        self.assertEqual(gpus[0].processes[0].pid, 4242)
        self.assertIsNone(gpus[0].processes[0].workload)

    def test_duplicate_gpu_uuid_does_not_share_processes(self) -> None:
        two = (
            "0, GPU-dup, NVIDIA A100, 550.54, P0, 61, 93, 34, "
            "81920, 40960, 40960, 287.5, 400\n"
            "1, GPU-dup, NVIDIA A100, 550.54, P0, 61, 93, 34, "
            "81920, 40960, 40960, 287.5, 400"
        )
        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(
                protocol="MONITOR_V8",
                gpu_payload=two,
                process_payload="GPU-dup, 4242, python, 2048",
                health_payload="",
            )
        )

        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0].processes, ())
        self.assertEqual(gpus[1].processes, ())

    def test_unavailable_gpu_uuid_does_not_receive_processes(self) -> None:
        gpu = (
            "0, [N/A], NVIDIA A100, 550.54, P0, 61, 93, 34, "
            "81920, 40960, 40960, 287.5, 400"
        )
        _, gpus, _ = parse_linux_resource_payload(
            resource_payload(
                protocol="MONITOR_V8",
                gpu_payload=gpu,
                process_payload="[N/A], 4242, python, 2048",
                health_payload="",
            )
        )

        self.assertEqual(gpus[0].uuid, "[N/A]")
        self.assertEqual(gpus[0].processes, ())

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/etc/passwd").exists(),
        "workload attribution requires Linux /proc and /etc/passwd",
    )
    def test_workload_owner_comes_from_uid_not_process_environment(self) -> None:
        uid = str(os.getuid())
        expected_owner: str | None = None
        for entry in Path("/etc/passwd").read_text(encoding="utf-8").splitlines():
            fields = entry.split(":")
            if len(fields) >= 3 and fields[2] == uid:
                expected_owner = fields[0]
                break
        if expected_owner is None:
            self.skipTest("current UID is absent from the /etc/passwd file")

        helper = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            env={
                **os.environ,
                "SLURM_JOB_ID": "777",
                "SLURM_JOB_NAME": "train-llm",
                "SLURM_JOB_USER": "spoofed-owner",
                # Newlines are valid inside an environment value. They must
                # not manufacture a second NUL-delimited variable record.
                "MOCOP_UNTRUSTED": "value\nSLURM_JOB_ID=forged",
            },
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root / "nvidia-smi"
                executable.write_text(
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    "  --query-gpu=*) printf '%s\\n' "
                    "'0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 93, 34, "
                    "81920, 40960, 40960, 287.5, 400, 0, No, No, "
                    "Not Active, Not Active, Disabled' ;;\n"
                    "  --query-compute-apps=*) printf '%s\\n' "
                    f"'GPU-abc, {helper.pid}, python, 1024' ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                executable.chmod(0o700)
                completed = _run_bounded_process(
                    ["sh", "-s"],
                    input_text=_remote_script("auto", True),
                    timeout_seconds=5,
                    max_output_bytes=2_097_152,
                    environment={
                        **os.environ,
                        "LC_ALL": "C",
                        "PATH": f"{root}:{os.environ['PATH']}",
                    },
                )
        finally:
            if helper.stdin is not None:
                helper.stdin.close()
            helper.wait()

        self.assertEqual(completed.returncode, 0)
        _, gpus, _ = parse_linux_resource_payload(completed.stdout)
        workload = gpus[0].processes[0].workload
        self.assertIsNotNone(workload)
        # Ownership is the real UID resolved via passwd, never the spoofed
        # SLURM_JOB_USER from the process environment.
        self.assertEqual(workload.owner, expected_owner)
        self.assertNotEqual(workload.owner, "spoofed-owner")
        self.assertEqual(workload.kind, "slurm")
        self.assertEqual(workload.workload_id, "777")
        # V7 identity columns ride along in the full tier.
        self.assertIn("sys.stdin.read", workload.command or "")
        self.assertIsNotNone(workload.started_at)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/etc/passwd").exists(),
        "identity attribution requires Linux /proc and /etc/passwd",
    )
    def test_identity_tier_collects_owner_command_and_start_time(self) -> None:
        helper = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdin.read()  #C:\\models\\train",
            ],
            stdin=subprocess.PIPE,
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root / "nvidia-smi"
                executable.write_text(
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    "  --query-gpu=*) printf '%s\\n' "
                    "'0, GPU-abc, NVIDIA A100, 550.54, P0, 61, 93, 34, "
                    "81920, 40960, 40960, 287.5, 400, 0, No, No, "
                    "Not Active, Not Active, Disabled' ;;\n"
                    "  --query-compute-apps=*) printf '%s\\n' "
                    f"'GPU-abc, {helper.pid}, python, 1024' ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                executable.chmod(0o700)
                completed = _run_bounded_process(
                    ["sh", "-s"],
                    input_text=_remote_script("identity", True),
                    timeout_seconds=5,
                    max_output_bytes=2_097_152,
                    environment={
                        **os.environ,
                        "LC_ALL": "C",
                        "PATH": f"{root}:{os.environ['PATH']}",
                    },
                )
        finally:
            if helper.stdin is not None:
                helper.stdin.close()
            helper.wait()

        self.assertEqual(completed.returncode, 0)
        self.assertIn("workload_tier=1", _remote_script("identity", True))
        _, gpus, _ = parse_linux_resource_payload(completed.stdout)
        workload = gpus[0].processes[0].workload
        self.assertIsNotNone(workload)
        # The light tier never reads cgroup or environ, so the kind stays
        # "process" while owner, command line and start time are populated.
        self.assertEqual(workload.kind, "process")
        self.assertIsNotNone(workload.owner)
        self.assertIn("sys.stdin.read", workload.command or "")
        # Backslashes survive verbatim: the command line travels through the
        # awk ENVIRON table, which never interprets escape sequences.
        self.assertIn("C:\\models\\train", workload.command or "")
        self.assertIsNotNone(workload.started_at)
        started = datetime.fromisoformat(workload.started_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        self.assertLessEqual(started, now)
        self.assertGreater(started, now - timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
