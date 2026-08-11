from __future__ import annotations

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
            0,
            "GPU-1",
            "Test GPU",
            "550",
            "P0",
            60,
            50,
            20,
            1000,
            250,
            750,
            100,
            200,
            (process,),
            True,
            health,
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
                self.assertEqual(
                    set(model.to_dict()),
                    {field.name for field in fields(model)},
                )


if __name__ == "__main__":
    unittest.main()
