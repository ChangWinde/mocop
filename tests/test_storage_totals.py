from __future__ import annotations

import json
import unittest
from pathlib import Path

from mocop.models import DiskMetrics, ProbeResult, SystemMetrics
from mocop.service import StateStore
from mocop.storage_totals import (
    SHARED_FILESYSTEMS,
    is_shared_filesystem,
    storage_totals,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "storage_totals.json"


def _system(*disks: DiskMetrics) -> SystemMetrics:
    return SystemMetrics(
        hostname="h",
        uptime_seconds=1,
        load_1m=0,
        load_5m=0,
        load_15m=0,
        cpu_cores=1,
        cpu_usage_pct=1,
        memory_total_mib=1,
        memory_used_mib=1,
        memory_available_mib=0,
        swap_total_mib=0,
        swap_used_mib=0,
        disk_total_mib=sum(disk.total_mib for disk in disks),
        disk_used_mib=sum(disk.used_mib for disk in disks),
        network_rx_bps=None,
        network_tx_bps=None,
        disk_read_bps=None,
        disk_write_bps=None,
        disks=disks,
    )


class StorageTotalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_classification_matches_the_shared_fixture(self) -> None:
        # The browser leaf reads the same lists, so a type added on one side
        # only fails here or in tests/fleet_state_test.mjs.
        filesystems = self.fixture["filesystems"]
        for kind in filesystems["shared"]:
            with self.subTest(kind=kind):
                self.assertTrue(is_shared_filesystem(kind))
        for kind in filesystems["local"]:
            with self.subTest(kind=kind):
                self.assertFalse(is_shared_filesystem(kind))
        self.assertLessEqual(SHARED_FILESYSTEMS, set(filesystems["shared"]))
        self.assertTrue(
            is_shared_filesystem("NFS4"), "types compare case-insensitively"
        )

    def test_totals_match_the_shared_fixture(self) -> None:
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(storage_totals(case["servers"]), case["expected"])

    def test_snapshot_stats_count_a_shared_volume_once(self) -> None:
        lustre = DiskMetrics("lfs@tcp:/share", "lustre", "/share", 1000, 500, 500, 50)
        store = StateStore(5)
        store.set_hosts(("gpu-1", "gpu-2"))
        store.apply(
            ProbeResult(
                "gpu-1",
                "online",
                12,
                system=_system(
                    DiskMetrics("/dev/sda1", "ext4", "/", 100, 10, 90, 10), lustre
                ),
            )
        )
        store.apply(
            ProbeResult(
                "gpu-2",
                "online",
                12,
                system=_system(
                    DiskMetrics("/dev/sda1", "ext4", "/", 200, 20, 180, 10), lustre
                ),
            )
        )

        stats = store.snapshot()["stats"]

        self.assertEqual(stats["diskTotalMiB"], 300)
        self.assertEqual(stats["diskUsedMiB"], 30)
        self.assertEqual(stats["sharedDisks"], 1)
        self.assertEqual(stats["sharedDiskTotalMiB"], 1000)
        self.assertEqual(stats["sharedDiskUsedMiB"], 500)


if __name__ == "__main__":
    unittest.main()
