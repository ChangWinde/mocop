from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from mocop.config import BUNDLED_CONFIG_PATH, load_config
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
        self.assertEqual(snapshot["hostGroups"], {})
        self.assertEqual(
            snapshot["collectorSettings"],
            {
                "pollIntervalSeconds": 5,
                "probeTimeoutSeconds": 12,
                "connectTimeoutSeconds": 5,
                "maxWorkers": 8,
            },
        )
        self.assertFalse(snapshot["autoDiscover"])
        self.assertTrue(snapshot["writable"])

    def test_snapshot_serializes_recurring_maintenance_from_one_clock_sample(
        self,
    ) -> None:
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["maintenance_windows"] = {
            "gpu-01": {
                "reason": "Weekly patching",
                "recurrence": {
                    "weekday": 0,
                    "start": "00:00",
                    "duration_minutes": 60,
                },
            }
        }
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.config_path.chmod(0o600)
        # Monday 00:30 UTC sits inside the weekly instance that ends at 01:00.
        sampled = datetime(2026, 1, 5, 0, 30, tzinfo=timezone.utc)

        with patch("mocop.inventory.datetime") as clock:
            clock.now.return_value = sampled
            snapshot = self.inventory.snapshot()

        # The activity decision and the serialized end share one clock sample,
        # so a boundary crossing cannot mix two different instances.
        clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(
            snapshot["maintenanceWindows"]["gpu-01"],
            {
                "until": "2026-01-05T01:00:00Z",
                "reason": "Weekly patching",
                "recurring": True,
                "active": True,
            },
        )

    def test_snapshot_reports_inactive_maintenance_windows_with_active_flag(
        self,
    ) -> None:
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["hosts"] = ["gpu-01", "gpu-02"]
        data["maintenance_windows"] = {
            "gpu-01": {
                "reason": "Weekly patching",
                "recurrence": {
                    "weekday": 0,
                    "start": "00:00",
                    "duration_minutes": 60,
                },
            },
            "gpu-02": {"until": "2020-01-01T00:00:00Z", "reason": "Expired"},
        }
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.config_path.chmod(0o600)
        # Tuesday noon UTC: outside the weekly Monday instance and past the
        # one-shot window, so both stay visible but inactive.
        sampled = datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc)

        with patch("mocop.inventory.datetime") as clock:
            clock.now.return_value = sampled
            snapshot = self.inventory.snapshot()

        self.assertEqual(
            snapshot["maintenanceWindows"],
            {
                "gpu-01": {
                    "until": "2026-01-12T01:00:00Z",
                    "reason": "Weekly patching",
                    "recurring": True,
                    "active": False,
                },
                "gpu-02": {
                    "until": "2020-01-01T00:00:00Z",
                    "reason": "Expired",
                    "active": False,
                },
            },
        )

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
                "connectTimeoutSeconds": 5,
                "maxWorkers": 7,
            },
        )
        self.assertEqual(reloaded.poll_interval_seconds, 2)
        self.assertEqual(reloaded.probe_timeout_seconds, 30)
        self.assertEqual(reloaded.max_workers, 7)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.updates[-1], reloaded)

    def test_updates_a_collector_settings_subset_and_keeps_other_fields(self) -> None:
        settings = self.inventory.update_collector_settings({"probeTimeoutSeconds": 30})

        self.assertEqual(
            settings,
            {
                "pollIntervalSeconds": 5,
                "probeTimeoutSeconds": 30,
                "connectTimeoutSeconds": 5,
                "maxWorkers": 8,
            },
        )
        reloaded = load_config(self.config_path)
        self.assertEqual(reloaded.poll_interval_seconds, 5)
        self.assertEqual(reloaded.probe_timeout_seconds, 30)
        self.assertEqual(reloaded.max_workers, 8)

    def test_persists_and_clears_one_bounded_incident_action(self) -> None:
        snapshot = self.inventory.update_incident_action(
            "gpu-01", "disk:/dev/a:/data", "silenced", 3600, "cleanup running"
        )

        action = load_config(self.config_path).incident_actions[0]
        self.assertEqual(action.host, "gpu-01")
        self.assertEqual(action.condition_key, "disk:/dev/a:/data")
        self.assertEqual(action.reason, "cleanup running")
        self.assertEqual(snapshot["incidentActions"][0]["action"], "silenced")

        cleared = self.inventory.update_incident_action(
            "gpu-01", "disk:/dev/a:/data", "clear", 0, ""
        )

        self.assertEqual(load_config(self.config_path).incident_actions, ())
        self.assertEqual(cleared["incidentActions"], [])

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
            # Down-linked read-only context; the write path must reject it.
            {"connectTimeoutSeconds": 10},
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
        data["maintenance_windows"] = {
            "gpu-01": {"until": "2030-06-15T12:30:00Z", "reason": "Repair"}
        }
        data["host_groups"] = {"gpu-01": "Training"}
        data["incident_overrides"] = {
            "hosts": {"gpu-01": {"thresholds": {"swap_warning_pct": 70}}},
            "groups": {"Training": {"thresholds": {"disk_warning_pct": 92}}},
        }
        data["topology"] = {"root": "gpu-01", "links": []}
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
        self.assertEqual(raw["maintenance_windows"], {})
        self.assertEqual(raw["host_groups"], {})
        self.assertEqual(raw["incident_overrides"], {"hosts": {}, "groups": {}})
        self.assertNotIn("topology", raw)
        self.assertEqual(self.updates[-1].hosts, ())

    def test_exposes_topology_without_scanning_and_prunes_removed_links(self) -> None:
        self.inventory.change("add", "gpu-02")
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["topology"] = {
            "root": "monitor-host",
            "links": [
                {
                    "source": "monitor-host",
                    "target": "bastion",
                    "transport": "frp-stcp",
                    "label": "STCP · 7009",
                },
                {
                    "source": "bastion",
                    "target": "gpu-02",
                    "transport": "ssh",
                },
            ],
        }
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with patch.object(
            self.inventory._host_source,
            "aliases",
            side_effect=AssertionError("topology must not scan OpenSSH"),
        ):
            self.assertEqual(self.inventory.topology(), data["topology"])

        self.inventory.change("remove", "gpu-02")

        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))["topology"],
            {
                "root": "monitor-host",
                "links": [
                    {
                        "source": "monitor-host",
                        "target": "bastion",
                        "transport": "frp-stcp",
                        "label": "STCP · 7009",
                    }
                ],
            },
        )

    def test_sets_clears_and_avoids_rewriting_shared_host_groups(self) -> None:
        changed = self.inventory.update_host_group("gpu-01", " Training ")

        self.assertEqual(changed["hostGroups"], {"gpu-01": "Training"})
        self.assertEqual(load_config(self.config_path).host_group("gpu-01"), "Training")
        persisted_at = self.config_path.stat().st_mtime_ns
        updates = len(self.updates)

        unchanged = self.inventory.update_host_group("gpu-01", "Training")

        self.assertEqual(unchanged["hostGroups"], {"gpu-01": "Training"})
        self.assertEqual(self.config_path.stat().st_mtime_ns, persisted_at)
        self.assertEqual(len(self.updates), updates)

        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["incident_overrides"] = {
            "groups": {"Training": {"thresholds": {"disk_warning_pct": 92}}}
        }
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        cleared = self.inventory.update_host_group("gpu-01", "")
        self.assertEqual(cleared["hostGroups"], {})
        self.assertIsNone(load_config(self.config_path).host_group("gpu-01"))
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["incident_overrides"]["groups"], {})

    def test_rejects_unsafe_host_groups_without_rewriting_config(self) -> None:
        before = self.config_path.read_bytes()
        cases = (
            ("unknown", "Training"),
            ("--bad", "Training"),
            ("gpu-01", "x\n"),
            ("gpu-01", "x\u007f"),
            ("gpu-01", "x\u202e"),
            ("gpu-01", "x" * 49),
        )

        for host, group in cases:
            with (
                self.subTest(host=host, group=group),
                self.assertRaises(InventoryError),
            ):
                self.inventory.update_host_group(host, group)
            self.assertEqual(self.config_path.read_bytes(), before)

    def test_sets_and_clears_bounded_maintenance_windows_atomically(self) -> None:
        changed = self.inventory.update_maintenance("gpu-01", 14_400, "Driver upgrade")

        window = changed["maintenanceWindows"]["gpu-01"]
        self.assertEqual(window["reason"], "Driver upgrade")
        reloaded = load_config(self.config_path)
        self.assertTrue(reloaded.maintenance_window("gpu-01").is_active())
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

        cleared = self.inventory.update_maintenance("gpu-01", 0, "")

        self.assertEqual(cleared["maintenanceWindows"], {})
        self.assertIsNone(load_config(self.config_path).maintenance_window("gpu-01"))

    def test_clearing_absent_maintenance_is_a_noop(self) -> None:
        before = self.config_path.stat().st_mtime_ns

        snapshot = self.inventory.update_maintenance("gpu-01", 0, "")

        self.assertEqual(snapshot["maintenanceWindows"], {})
        self.assertEqual(self.config_path.stat().st_mtime_ns, before)
        self.assertEqual(self.updates, [])

    def test_rejects_unsafe_maintenance_changes_without_rewriting_config(self) -> None:
        before = self.config_path.read_bytes()
        cases = (
            ("unknown", 3600, "Work"),
            ("--bad", 3600, "Work"),
            ("gpu-01", 60, "Work"),
            ("gpu-01", 3600, ""),
            ("gpu-01", 3600, "x\n"),
            ("gpu-01", 3600, "x\u007f"),
            ("gpu-01", 3600, "x\u202e"),
            ("gpu-01", 3600, "x" * 121),
        )

        for host, duration, reason in cases:
            with (
                self.subTest(host=host, duration=duration),
                self.assertRaises(InventoryError),
            ):
                self.inventory.update_maintenance(host, duration, reason)
            self.assertEqual(self.config_path.read_bytes(), before)

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

    def test_reports_dashboard_writability_without_scanning_openssh(self) -> None:
        with (
            patch.object(
                self.inventory._host_source,
                "aliases",
                side_effect=AssertionError("writable() must not scan OpenSSH"),
            ),
            patch.object(
                self.inventory._host_source,
                "hosts",
                side_effect=AssertionError("writable() must not resolve hosts"),
            ),
        ):
            self.assertTrue(self.inventory.writable())

        bundled = ConfigInventory(
            BUNDLED_CONFIG_PATH,
            OpenSshConfigHostSource(),
            self.updates.append,
        )
        self.assertFalse(bundled.writable())

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
