from __future__ import annotations

import json
import unittest
from dataclasses import fields

from mocop.models import (
    DiskMetrics,
    GpuHealthMetrics,
    GpuMetrics,
    GpuProcess,
    SystemMetrics,
    WorkloadMetadata,
)


class ModelSerializationTests(unittest.TestCase):
    def test_metric_serializers_preserve_every_declared_field(self) -> None:
        workload = WorkloadMetadata("slurm", "42", "train", "user", "gpu")
        process = GpuProcess(1234, "python", 512, workload)
        health = GpuHealthMetrics(0, False, False, False, False, "Disabled")
        gpu = GpuMetrics(
            index=0,
            uuid="GPU-1",
            name="Test GPU",
            driver_version="550",
            pstate="P0",
            temperature_c=60,
            utilization_gpu_pct=50,
            utilization_memory_pct=20,
            memory_total_mib=1000,
            memory_used_mib=250,
            memory_free_mib=750,
            power_draw_w=100,
            power_limit_w=200,
            processes=(process,),
            processes_available=True,
            processes_sampled=False,
            processes_observed_at="2030-06-15T12:30:00Z",
            health=health,
        )
        disk = DiskMetrics("/dev/a", "ext4", "/data", 100, 40, 60, 40)
        system = SystemMetrics(
            "node-a",
            1000,
            1,
            0.5,
            0.25,
            8,
            25,
            16000,
            8000,
            8000,
            2000,
            500,
            100000,
            40000,
            1000,
            2000,
            3000,
            4000,
            (disk,),
        )

        for model in (workload, process, health, gpu, disk, system):
            with self.subTest(model=type(model).__name__):
                payload = model.to_dict()
                self.assertEqual(
                    set(payload),
                    {field.name for field in fields(model)},
                )
                json.dumps(payload)

        gpu_payload = gpu.to_dict()
        self.assertIsInstance(gpu_payload["processes_sampled"], bool)
        self.assertIs(gpu_payload["processes_sampled"], False)
        self.assertEqual(
            gpu_payload["health"],
            {
                "ecc_uncorrected_volatile": 0,
                "retired_pages_pending": False,
                "remapped_rows_pending": False,
                "thermal_slowdown": False,
                "power_brake_slowdown": False,
                "mig_mode": "Disabled",
            },
        )


if __name__ == "__main__":
    unittest.main()
