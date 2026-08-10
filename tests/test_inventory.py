from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from mocop.config import load_config
from mocop.discovery import OpenSshConfigHostSource
from mocop.inventory import ConfigInventory, InventoryError
from mocop.lifecycle import initialize_config


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_path = self.root / "config.json"
        self.ssh_config = self.root / "ssh-config"
        self.ssh_config.write_text(
            "Host gpu-01 gpu-02 github.com corp-gitlab git.internal bastion\n",
            encoding="utf-8",
        )
        initialize_config(self.config_path, ("gpu-01",))
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["ssh_config"] = str(self.ssh_config)
        data["exclude_hosts"] = ["bastion"]
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.config_path.chmod(0o600)
        self.updates = []
        self.inventory = ConfigInventory(
            self.config_path,
            OpenSshConfigHostSource(),
            self.updates.append,
        )

    def test_scan_reports_configured_and_eligible_aliases_without_connecting(
        self,
    ) -> None:
        snapshot = self.inventory.snapshot()

        self.assertEqual(snapshot["configuredHosts"], ["gpu-01"])
        self.assertEqual(snapshot["availableHosts"], ["gpu-02"])
        self.assertEqual(snapshot["localHost"], None)
        self.assertEqual(snapshot["ignoredCodeHostCount"], 3)
        self.assertEqual(snapshot["excludedHostCount"], 1)
        self.assertEqual(
            snapshot["collectorSettings"],
            {
                "pollIntervalSeconds": 5,
                "probeTimeoutSeconds": 12,
                "maxWorkers": 8,
            },
        )
        self.assertFalse(snapshot["autoDiscover"])
        self.assertTrue(snapshot["writable"])

    def test_updates_collector_settings_atomically_and_persists_them(self) -> None:
        settings = self.inventory.update_collector_settings(
            {
                "pollIntervalSeconds": 2,
                "probeTimeoutSeconds": 30,
                "maxWorkers": 7,
            }
        )

        reloaded = load_config(self.config_path)
        self.assertEqual(
            settings,
            {
                "pollIntervalSeconds": 2,
                "probeTimeoutSeconds": 30,
                "maxWorkers": 7,
            },
        )
        self.assertEqual(reloaded.poll_interval_seconds, 2)
        self.assertEqual(reloaded.probe_timeout_seconds, 30)
        self.assertEqual(reloaded.max_workers, 7)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.updates[-1], reloaded)

    def test_rejects_invalid_collector_settings_without_modifying_config(self) -> None:
        before = self.config_path.read_bytes()
        cases = (
            {},
            {"pollIntervalSeconds": 1},
            {"pollIntervalSeconds": True},
            {"probeTimeoutSeconds": 5},
            {"probeTimeoutSeconds": float("inf")},
            {"maxWorkers": 2.5},
            {"maxWorkers": 65},
            {"unknown": 1},
        )

        for settings in cases:
            with self.subTest(settings=settings), self.assertRaises(InventoryError):
                self.inventory.update_collector_settings(settings)
            self.assertEqual(self.config_path.read_bytes(), before)

    def test_noop_collector_update_does_not_rewrite_or_notify(self) -> None:
        before = self.config_path.stat().st_mtime_ns

        settings = self.inventory.update_collector_settings({"pollIntervalSeconds": 5})

        self.assertEqual(settings["pollIntervalSeconds"], 5)
        self.assertEqual(self.config_path.stat().st_mtime_ns, before)
        self.assertEqual(self.updates, [])

    def test_add_requires_a_fresh_eligible_scan_and_persists_privately(self) -> None:
        changed = self.inventory.change("add", "gpu-02")

        self.assertEqual(changed["configuredHosts"], ["gpu-01", "gpu-02"])
        self.assertEqual(changed["availableHosts"], [])
        self.assertEqual(load_config(self.config_path).hosts, ("gpu-01", "gpu-02"))
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.updates[-1].hosts, ("gpu-01", "gpu-02"))

        for alias in ("unknown", "github.com", "bastion", "--proxy-command"):
            with self.subTest(alias=alias), self.assertRaises(InventoryError):
                self.inventory.change("add", alias)

    def test_remove_cleans_inventory_metadata_and_local_transport(self) -> None:
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["local_host"] = "gpu-01"
        data["expected_gpu_counts"] = {"gpu-01": 8}
        data["host_overrides"] = {"gpu-01": {"poll_interval_seconds": 30}}
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        changed = self.inventory.change("remove", "gpu-01")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))

        self.assertEqual(changed["configuredHosts"], [])
        self.assertIsNone(raw["local_host"])
        self.assertEqual(raw["expected_gpu_counts"], {})
        self.assertEqual(raw["host_overrides"], {})
        self.assertEqual(self.updates[-1].hosts, ())

    def test_remove_stays_removed_when_automatic_discovery_is_enabled(self) -> None:
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["auto_discover"] = True
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        changed = self.inventory.change("remove", "gpu-02")

        reloaded = load_config(self.config_path)
        self.assertIn("gpu-02", reloaded.exclude_hosts)
        self.assertEqual(reloaded.hosts, ("gpu-01",))
        self.assertNotIn("gpu-02", changed["activeHosts"])

    def test_invalid_actions_and_missing_hosts_do_not_modify_the_file(self) -> None:
        before = self.config_path.read_bytes()

        for action, host in (("replace", "gpu-01"), ("remove", "missing")):
            with (
                self.subTest(action=action, host=host),
                self.assertRaises(InventoryError),
            ):
                self.inventory.change(action, host)
            self.assertEqual(self.config_path.read_bytes(), before)

    def test_concurrent_additions_are_serialized_without_losing_an_update(self) -> None:
        self.ssh_config.write_text(
            self.ssh_config.read_text(encoding="utf-8").rstrip() + " gpu-03\n",
            encoding="utf-8",
        )
        barrier = Barrier(3)

        def add(host: str) -> None:
            barrier.wait()
            self.inventory.change("add", host)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(add, "gpu-02")
            second = pool.submit(add, "gpu-03")
            barrier.wait()
            first.result(timeout=2)
            second.result(timeout=2)

        self.assertEqual(
            set(load_config(self.config_path).hosts),
            {"gpu-01", "gpu-02", "gpu-03"},
        )

    def test_failed_atomic_replace_preserves_the_previous_configuration(self) -> None:
        before = self.config_path.read_bytes()

        with (
            patch("mocop.inventory.os.replace", side_effect=OSError("read only")),
            self.assertRaises(InventoryError),
        ):
            self.inventory.change("add", "gpu-02")

        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".config.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
