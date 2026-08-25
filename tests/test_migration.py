from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mocop.config import BUNDLED_CONFIG_PATH, load_private_config
from mocop.lifecycle import LifecycleError
from mocop.migration import migrate_config


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write_source(self, directory: str = "source", **overrides: object) -> Path:
        data = json.loads(BUNDLED_CONFIG_PATH.read_text(encoding="utf-8"))
        data.update(overrides)
        path = self.root / directory / "config.json"
        path.parent.mkdir(mode=0o700)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def test_rebinds_local_host_and_drops_only_machine_bound_state(self) -> None:
        source = self.write_source(
            ssh_config="/home/old/.ssh/config",
            auto_discover=False,
            ssh_discovery={
                "mode": "aliases",
                "refresh_seconds": 120,
                "resolve_timeout_seconds": 2,
            },
            hosts=["old-monitor", "gpu-01"],
            local_host="old-monitor",
            expected_gpu_counts={"old-monitor": 2, "gpu-01": 8},
            host_overrides={
                "old-monitor": {"display_name": "old-console"},
                "gpu-01": {"probe_timeout_seconds": 20},
            },
            maintenance_windows={
                "old-monitor": {
                    "until": "2030-06-15T12:30:00Z",
                    "reason": "Old-machine repair",
                }
            },
            host_groups={"old-monitor": "Console", "gpu-01": "Training"},
            incident_overrides={
                "hosts": {
                    "old-monitor": {"thresholds": {"swap_warning_pct": 70}},
                    "gpu-01": {"thresholds": {"disk_warning_pct": 90}},
                }
            },
            incident_actions=[
                {
                    "host": "old-monitor",
                    "condition_key": "disk:/",
                    "action": "acknowledged",
                    "until": "2030-06-15T12:30:00Z",
                    "reason": "Old incident",
                },
                {
                    "host": "gpu-01",
                    "condition_key": "gpu:0:temperature",
                    "action": "silenced",
                    "until": "2030-06-15T12:30:00Z",
                    "reason": "Remote maintenance",
                },
            ],
            topology={
                "root": "old-monitor",
                "links": [
                    {
                        "source": "old-monitor",
                        "target": "gpu-01",
                        "transport": "ssh",
                    }
                ],
            },
        )
        original = source.read_bytes()
        target = self.root / "target" / "config.json"

        result = migrate_config(
            source,
            target,
            current_hostname="new-monitor",
            display_name="console-0",
            ssh_config="~/.ssh/config",
            auto_discover=True,
        )

        config = load_private_config(target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.hosts, ("new-monitor", "gpu-01"))
        self.assertEqual(config.local_host, "new-monitor")
        self.assertTrue(config.auto_discover)
        self.assertEqual(config.ssh_config, Path.home() / ".ssh" / "config")
        self.assertEqual(config.ssh_discovery.mode, "topology")
        self.assertEqual(config.ssh_discovery.refresh_seconds, 120)
        self.assertEqual(config.ssh_discovery.resolve_timeout_seconds, 2)
        self.assertEqual(dict(config.expected_gpu_counts), {"gpu-01": 8})
        self.assertEqual(
            raw["host_overrides"],
            {
                "gpu-01": {"probe_timeout_seconds": 20},
                "new-monitor": {"display_name": "console-0"},
            },
        )
        self.assertEqual(raw["maintenance_windows"], {})
        self.assertEqual(
            raw["host_groups"],
            {"gpu-01": "Training", "new-monitor": "Console"},
        )
        self.assertEqual(
            raw["incident_overrides"],
            {"hosts": {"gpu-01": {"thresholds": {"disk_warning_pct": 90}}}},
        )
        self.assertEqual(
            [action["host"] for action in raw["incident_actions"]], ["gpu-01"]
        )
        self.assertEqual(raw["topology"]["root"], "new-monitor")
        self.assertEqual(raw["topology"]["links"][0]["source"], "new-monitor")
        self.assertEqual(result.old_local_host, "old-monitor")
        self.assertEqual(result.new_local_host, "new-monitor")
        self.assertIn("expected_gpu_counts.old-monitor", result.dropped_fields)
        self.assertIn("maintenance_windows.old-monitor", result.dropped_fields)
        self.assertFalse((target.parent / "access-token").exists())

    def test_preserves_admission_policy_when_source_has_no_local_host(self) -> None:
        source = self.write_source(
            auto_discover=False,
            hosts=["gpu-01"],
            local_host=None,
        )
        target = self.root / "target" / "config.json"

        result = migrate_config(
            source,
            target,
            current_hostname="new-monitor",
        )

        config = load_private_config(target)
        self.assertFalse(config.auto_discover)
        self.assertEqual(config.hosts, ("gpu-01",))
        self.assertIsNone(config.local_host)
        self.assertIsNone(result.new_local_host)
        self.assertEqual(config.ssh_discovery.mode, "topology")

    def test_drop_local_host_removes_its_topology_and_metadata(self) -> None:
        source = self.write_source(
            hosts=["old-monitor", "gpu-01"],
            local_host="old-monitor",
            expected_gpu_counts={"old-monitor": 2},
            host_groups={"old-monitor": "Console"},
            topology={
                "root": "old-monitor",
                "links": [
                    {
                        "source": "old-monitor",
                        "target": "gpu-01",
                        "transport": "ssh",
                    }
                ],
            },
        )
        target = self.root / "target" / "config.json"

        result = migrate_config(
            source,
            target,
            current_hostname="new-monitor",
            drop_local_host=True,
        )

        config = load_private_config(target)
        self.assertEqual(config.hosts, ("gpu-01",))
        self.assertIsNone(config.local_host)
        self.assertIsNone(config.topology)
        self.assertIn("topology", result.dropped_fields)

    def test_rejects_alias_collisions_existing_targets_and_copied_tokens(self) -> None:
        source = self.write_source(
            hosts=["old-monitor", "new-monitor"],
            local_host="old-monitor",
        )

        with self.assertRaisesRegex(LifecycleError, "already identifies"):
            migrate_config(
                source,
                self.root / "collision" / "config.json",
                current_hostname="new-monitor",
            )

        # A collision with a topology-only node (a jump host, for example)
        # would fold two nodes into one and self-link; the refusal must name
        # the collision instead of surfacing a downstream schema error.
        infrastructure = self.write_source(
            directory="infrastructure",
            hosts=["old-monitor", "gpu-01"],
            local_host="old-monitor",
            topology={
                "root": "old-monitor",
                "links": [
                    {"source": "old-monitor", "target": "bastion", "transport": "ssh"},
                    {"source": "bastion", "target": "gpu-01", "transport": "ssh"},
                ],
            },
        )
        with self.assertRaisesRegex(
            LifecycleError, "already appears in the configured topology: bastion"
        ):
            migrate_config(
                infrastructure,
                self.root / "topology-collision" / "config.json",
                current_hostname="bastion",
            )
        self.assertFalse((self.root / "topology-collision" / "config.json").exists())

        existing = self.root / "existing" / "config.json"
        existing.parent.mkdir(mode=0o700)
        existing.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(LifecycleError, "already exists"):
            migrate_config(source, existing, current_hostname="other-monitor")
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")

        token_target = self.root / "token" / "config.json"
        token_target.parent.mkdir(mode=0o700)
        token = token_target.with_name("access-token")
        token.write_text("A" * 43, encoding="ascii")
        token.chmod(0o600)
        with self.assertRaisesRegex(LifecycleError, "access-token"):
            migrate_config(source, token_target, current_hostname="other-monitor")
        self.assertFalse(token_target.exists())

    def test_requires_private_source_and_valid_local_display_options(self) -> None:
        source = self.write_source(hosts=["old-monitor"], local_host="old-monitor")
        source.chmod(0o644)
        with self.assertRaisesRegex(LifecycleError, "source configuration"):
            migrate_config(
                source,
                self.root / "private" / "config.json",
                current_hostname="new-monitor",
            )

        source.chmod(0o600)
        no_local = self.write_source("no-local", hosts=["gpu-01"], local_host=None)
        with self.assertRaisesRegex(LifecycleError, "display name requires"):
            migrate_config(
                no_local,
                self.root / "display" / "config.json",
                current_hostname="new-monitor",
                display_name="Console",
            )

        with self.assertRaisesRegex(LifecycleError, "invalid local host alias"):
            migrate_config(
                source,
                self.root / "unsafe" / "config.json",
                current_hostname="bad host",
            )

    def test_reads_source_once_through_the_private_config_boundary(self) -> None:
        source = self.write_source(hosts=["gpu-01"], local_host=None)
        target = self.root / "target" / "config.json"

        with patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("migration must not reopen the source by path"),
        ):
            migrate_config(source, target, current_hostname="new-monitor")

        self.assertEqual(load_private_config(target).hosts, ("gpu-01",))

    def test_removes_invalid_output_and_rejects_an_unsafe_target_directory(
        self,
    ) -> None:
        source = self.write_source(hosts=["old-monitor"], local_host="old-monitor")
        invalid_target = self.root / "invalid" / "config.json"

        with self.assertRaisesRegex(
            LifecycleError, "migrated configuration is invalid"
        ):
            migrate_config(
                source,
                invalid_target,
                current_hostname="new-monitor",
                display_name="bad\nname",
            )
        self.assertFalse(invalid_target.exists())

        unsafe_target = self.root / "unsafe-target"
        unsafe_target.mkdir(mode=0o700)
        unsafe_target.chmod(0o777)
        with self.assertRaisesRegex(LifecycleError, "owner-controlled"):
            migrate_config(
                source,
                unsafe_target / "config.json",
                current_hostname="new-monitor",
            )
        self.assertFalse((unsafe_target / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
