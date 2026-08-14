from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from mocop.config import MaintenanceWindowConfig
from mocop.metrics import OpenMetricsLimitError, render_openmetrics
from mocop.models import GpuHealthMetrics, GpuMetrics, ProbeResult, SystemMetrics
from mocop.service import StateStore


def _system() -> SystemMetrics:
    return SystemMetrics(
        hostname="gpu-01",
        uptime_seconds=3600,
        load_1m=1.5,
        load_5m=1.25,
        load_15m=1,
        cpu_cores=32,
        cpu_usage_pct=25,
        memory_total_mib=1024,
        memory_used_mib=512,
        memory_available_mib=512,
        swap_total_mib=128,
        swap_used_mib=16,
        disk_total_mib=4096,
        disk_used_mib=1024,
        network_rx_bps=2048,
        network_tx_bps=1024,
        disk_read_bps=512,
        disk_write_bps=256,
    )


def _gpu() -> GpuMetrics:
    return GpuMetrics(
        index=0,
        uuid='GPU-quote"slash\\line\ncr\rend',
        name='NVIDIA "Test"\\GPU\r\nModel',
        driver_version="550.1",
        pstate="P0",
        temperature_c=65,
        utilization_gpu_pct=75,
        utilization_memory_pct=50,
        memory_total_mib=81920,
        memory_used_mib=40960,
        memory_free_mib=40960,
        power_draw_w=350,
        power_limit_w=700,
        processes=(),
        processes_available=True,
        processes_sampled=True,
        processes_observed_at="2030-06-15T12:30:00Z",
        health=GpuHealthMetrics(
            ecc_uncorrected_volatile=0,
            retired_pages_pending=False,
            remapped_rows_pending=False,
            thermal_slowdown=False,
            power_brake_slowdown=False,
            mig_mode="Disabled",
        ),
    )


class OpenMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StateStore(5, host_groups=(("gpu-01", "Training"),))
        self.store.set_hosts(("gpu-01", "gpu-02"))
        self.store.apply(
            ProbeResult(
                "gpu-01",
                "online",
                250,
                gpus=(_gpu(),),
                observed_at="2030-06-15T12:30:00Z",
                system=_system(),
            )
        )
        self.store.apply(
            ProbeResult(
                "gpu-02",
                "unreachable",
                5000,
                observed_at="2030-06-15T12:30:00Z",
            )
        )
        self.store.record_poll_cycle(0.5)

    def test_renders_openmetrics_metadata_units_values_and_eof(self) -> None:
        body = render_openmetrics(self.store.snapshot()).decode()

        self.assertTrue(body.endswith("# EOF\n"))
        self.assertIn("# TYPE mocop_gpu_utilization_ratio gauge\n", body)
        self.assertIn("# UNIT mocop_gpu_memory_total_bytes bytes\n", body)
        self.assertIn(
            'mocop_host_info{host="gpu-01",mocop_group="Training"} 1\n',
            body,
        )
        self.assertIn('mocop_host_up{host="gpu-01"} 1\n', body)
        self.assertIn('mocop_host_up{host="gpu-02"} 0\n', body)
        self.assertIn('mocop_host_incidents_active{host="gpu-02"} 1\n', body)
        self.assertIn('mocop_host_incidents_actionable{host="gpu-02"} 1\n', body)
        self.assertIn(
            'mocop_gpu_utilization_ratio{host="gpu-01",index="0",uuid=',
            body,
        )
        self.assertIn("} 0.75\n", body)
        self.assertIn("mocop_gpu_memory_total_bytes", body)
        self.assertIn(" 85899345920\n", body)
        self.assertIn("mocop_gpu_process_telemetry_sampled", body)
        self.assertIn(
            "mocop_gpu_process_sample_timestamp_seconds",
            body,
        )
        self.assertIn("mocop_collection_duration_seconds 0.5\n", body)
        self.assertIn("mocop_persistence_enabled 0\n", body)
        self.assertIn("mocop_persistence_healthy 1\n", body)
        self.assertIn("mocop_notifications_enabled 0\n", body)
        self.assertIn("mocop_notifications_healthy 1\n", body)
        self.assertIn("# TYPE mocop_persistence_queued_writes gauge\n", body)
        self.assertIn("# TYPE mocop_persistence_dropped_writes counter\n", body)
        self.assertIn("mocop_persistence_dropped_writes_total 0\n", body)
        self.assertNotIn("# TYPE mocop_persistence_dropped_writes_total ", body)
        self.assertIn("# TYPE mocop_notifications_dropped_deliveries counter\n", body)
        self.assertIn("mocop_notifications_dropped_deliveries_total 0\n", body)
        self.assertNotIn("# TYPE mocop_notifications_dropped_deliveries_total ", body)

        self.store.set_maintenance_windows(
            (
                (
                    "gpu-02",
                    MaintenanceWindowConfig(
                        until=datetime.now(timezone.utc) + timedelta(hours=1),
                        reason="Network work",
                    ),
                ),
            )
        )
        silenced = render_openmetrics(self.store.snapshot()).decode()
        self.assertIn('mocop_host_incidents_active{host="gpu-02"} 1\n', silenced)
        self.assertIn('mocop_host_incidents_actionable{host="gpu-02"} 0\n', silenced)

    def test_escapes_untrusted_labels_and_omits_stale_gpu_samples(self) -> None:
        body = render_openmetrics(self.store.snapshot()).decode()

        self.assertIn('uuid="GPU-quote\\"slash\\\\line\\ncr\\nend"', body)
        self.assertIn('model="NVIDIA \\"Test\\"\\\\GPU\\nModel"', body)
        self.assertNotIn("\r", body)
        self.store.apply(
            ProbeResult(
                "gpu-01",
                "unreachable",
                5000,
                observed_at="2030-06-15T12:31:00Z",
            )
        )

        stale_body = render_openmetrics(self.store.snapshot()).decode()

        self.assertIn('mocop_host_stale{host="gpu-01"} 1\n', stale_body)
        self.assertNotIn('mocop_gpu_info{host="gpu-01"', stale_body)
        self.assertNotIn('mocop_host_cpu_utilization_ratio{host="gpu-01"', stale_body)

    def test_omits_non_finite_or_missing_optional_values(self) -> None:
        payload = {
            "appVersion": "test",
            "pollIntervalSeconds": float("nan"),
            "stats": {},
            "servers": [],
        }

        body = render_openmetrics(payload).decode()

        self.assertNotIn("mocop_collection_poll_interval_seconds", body)
        self.assertTrue(body.endswith("# EOF\n"))

    def test_omits_process_count_when_process_telemetry_failed(self) -> None:
        snapshot = self.store.snapshot()
        gpu = snapshot["servers"][0]["gpus"][0]
        gpu["processes"] = []
        gpu["processes_available"] = False
        gpu["processes_sampled"] = True

        body = render_openmetrics(snapshot).decode()

        labels = 'host="gpu-01",index="0"'
        self.assertNotIn(f"mocop_gpu_processes{{{labels}", body)
        self.assertIn("mocop_gpu_process_telemetry_available", body)
        self.assertIn("mocop_gpu_process_telemetry_sampled", body)
        self.assertIn("attempted process telemetry", body)

    def test_rejects_expositions_above_the_series_budget_before_rendering(self) -> None:
        snapshot = {
            "appVersion": "test",
            "stats": {},
            "servers": [
                {
                    "host": "gpu-01",
                    "status": "online",
                    "stale": False,
                    "gpus": [
                        {"index": index, "uuid": f"GPU-{index}"}
                        for index in range(5_001)
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(OpenMetricsLimitError, "series budget"):
            render_openmetrics(snapshot)

    def test_exports_gpu_metrics_when_system_metrics_are_missing(self) -> None:
        self.store.apply(
            ProbeResult(
                "gpu-01",
                "online",
                250,
                gpus=(_gpu(),),
                observed_at="2030-06-15T12:32:00Z",
                system=None,
            )
        )

        body = render_openmetrics(self.store.snapshot()).decode()

        self.assertIn('mocop_host_up{host="gpu-01"} 1\n', body)
        self.assertNotIn('mocop_host_cpu_utilization_ratio{host="gpu-01"', body)
        self.assertNotIn('mocop_host_memory_total_bytes{host="gpu-01"', body)
        self.assertIn('mocop_gpu_info{host="gpu-01"', body)
        self.assertIn(
            'mocop_gpu_utilization_ratio{host="gpu-01",index="0",uuid=',
            body,
        )

    def test_exports_pressure_stall_ratios_with_resource_labels(self) -> None:
        payload = {
            "appVersion": "test",
            "stats": {},
            "servers": [
                {
                    "host": "gpu-01",
                    "status": "online",
                    "stale": False,
                    "system": {
                        "pressure": {
                            "cpu": {
                                "some_avg10": 1.5,
                                "some_avg60": 1.0,
                                "full_avg10": None,
                                "full_avg60": None,
                            },
                            "memory": {
                                "some_avg10": 25.0,
                                "some_avg60": 20.0,
                                "full_avg10": 10.0,
                                "full_avg60": 5.0,
                            },
                            "io": None,
                        },
                    },
                    "gpus": [],
                }
            ],
        }

        body = render_openmetrics(payload).decode()

        self.assertIn(
            'mocop_host_pressure_some_ratio{host="gpu-01",resource="cpu"} 0.015\n',
            body,
        )
        self.assertIn(
            'mocop_host_pressure_some_ratio{host="gpu-01",resource="memory"} 0.25\n',
            body,
        )
        self.assertIn(
            'mocop_host_pressure_full_ratio{host="gpu-01",resource="memory"} 0.1\n',
            body,
        )
        # Unreported resources and unavailable full averages are omitted, not zero.
        self.assertNotIn('resource="io"', body)
        self.assertNotIn(
            'mocop_host_pressure_full_ratio{host="gpu-01",resource="cpu"}', body
        )

    def test_omits_mib_samples_that_overflow_when_scaled_to_bytes(self) -> None:
        payload = {
            "appVersion": "test",
            "stats": {"memoryTotalMiB": 1e308, "memoryUsedMiB": 1024},
            "servers": [
                {
                    "host": "gpu-01",
                    "status": "online",
                    "stale": False,
                    "system": {"memory_total_mib": 1e308, "memory_used_mib": 512},
                    "gpus": [],
                }
            ],
        }

        body = render_openmetrics(payload).decode()

        self.assertNotIn("mocop_cluster_gpu_memory_total_bytes", body)
        self.assertIn("mocop_cluster_gpu_memory_used_bytes 1073741824\n", body)
        self.assertNotIn("mocop_host_memory_total_bytes", body)
        self.assertIn('mocop_host_memory_used_bytes{host="gpu-01"} 536870912\n', body)
        self.assertTrue(body.endswith("# EOF\n"))


if __name__ == "__main__":
    unittest.main()
