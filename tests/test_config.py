from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from mocop.config import (
    BUNDLED_CONFIG_PATH,
    CONFIG_ENV_VAR,
    ConfigError,
    load_config,
    resolve_config_path,
)


def valid_config() -> dict[str, object]:
    return {
        "ssh_config": "~/.ssh/config",
        "auto_discover": True,
        "hosts": [],
        "exclude_hosts": [],
        "poll_interval_seconds": 5,
        "probe_timeout_seconds": 12,
        "connect_timeout_seconds": 5,
        "max_workers": 8,
        "listen_host": "127.0.0.1",
        "listen_port": 8787,
    }


class ConfigTests(unittest.TestCase):
    def write(self, value: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def write_raw(self, raw: str | bytes) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        if isinstance(raw, bytes):
            path.write_bytes(raw)
        else:
            path.write_text(raw, encoding="utf-8")
        return path

    def test_loads_valid_config(self) -> None:
        config = load_config(self.write(valid_config()))
        self.assertTrue(config.auto_discover)
        self.assertEqual(config.max_workers, 8)
        self.assertEqual(config.listen_port, 8787)
        self.assertEqual(config.history_points, 720)
        self.assertEqual(config.incident_history_points, 500)
        self.assertEqual(config.collection_stale_cycles, 3)
        self.assertEqual(config.gpu_process_poll_interval_seconds, 15)
        self.assertEqual(config.retry_jitter_pct, 15)
        self.assertEqual(config.manual_probe_cooldown_seconds, 5)
        self.assertEqual(config.expected_gpu_counts, ())
        self.assertEqual(config.host_overrides, ())
        self.assertEqual(config.maintenance_windows, ())
        self.assertEqual(config.host_groups, ())
        self.assertIsNone(config.topology)
        self.assertFalse(config.persistence.enabled)
        self.assertEqual(config.persistence.retention_hours, 168)
        self.assertEqual(config.persistence.max_bytes, 134_217_728)
        self.assertEqual(config.workloads.mode, "disabled")
        self.assertEqual(config.webhooks, ())
        self.assertEqual(config.incident_actions, ())
        self.assertEqual(config.host_incident_overrides, ())
        self.assertEqual(config.group_incident_overrides, ())
        self.assertEqual(config.incidents.resource_open_cycles, 2)
        self.assertEqual(config.incidents.recovery_cycles, 2)
        self.assertEqual(config.incidents.gpu_idle_memory_cycles, 12)

    def test_validates_trusted_web_hosts(self) -> None:
        default = load_config(self.write(valid_config()))
        self.assertEqual(default.trusted_web_hosts, ())

        value = valid_config()
        value["trusted_web_hosts"] = [
            "Dashboard.Example",
            "10.0.0.8",
            "fd00::5",
            "[fd00::6]",
            "dashboard.example",
        ]
        config = load_config(self.write(value))
        self.assertEqual(
            config.trusted_web_hosts,
            ("dashboard.example", "10.0.0.8", "fd00::5", "fd00::6"),
        )

        invalid_cases = (
            "not-a-list",
            ["dashboard.example:8787"],
            ["https://dashboard.example"],
            ["dashboard.example/path"],
            ["user@dashboard.example"],
            [""],
            ["   "],
            [42],
            ["[not-an-ip]"],
            ["a" * 300],
            ["dashboard.example"] * 33,
        )
        for entries in invalid_cases:
            broken = valid_config()
            broken["trusted_web_hosts"] = entries
            with self.subTest(entries=entries), self.assertRaises(ConfigError):
                load_config(self.write(broken))

    def test_validates_durable_incident_actions_and_scoped_overrides(self) -> None:
        value = valid_config()
        value.update(
            {
                "auto_discover": False,
                "hosts": ["gpu-01"],
                "host_groups": {"gpu-01": "training"},
                "incident_actions": [
                    {
                        "host": "gpu-01",
                        "condition_key": "disk:/dev/a:/data",
                        "action": "acknowledged",
                        "until": "2030-08-10T00:00:00Z",
                        "reason": "owner notified",
                    }
                ],
                "incident_overrides": {
                    "hosts": {
                        "gpu-01": {
                            "thresholds": {"swap_warning_pct": 75},
                            "exclude_disk_mounts": ["/archive"],
                        }
                    },
                    "groups": {
                        "training": {"thresholds": {"gpu_temperature_warning_c": 84}}
                    },
                },
            }
        )

        config = load_config(self.write(value))

        self.assertEqual(config.incident_actions[0].action, "acknowledged")
        host_override = dict(config.host_incident_overrides)["gpu-01"]
        self.assertEqual(host_override.threshold("swap_warning_pct"), 75)
        self.assertEqual(host_override.exclude_disk_mounts, frozenset({"/archive"}))
        self.assertEqual(
            dict(config.group_incident_overrides)["training"].threshold(
                "gpu_temperature_warning_c"
            ),
            84,
        )

    def test_rejects_ambiguous_or_unsafe_incident_operations(self) -> None:
        cases = (
            [
                {
                    "host": "gpu-01",
                    "condition_key": "cpu",
                    "action": "forever",
                    "until": "2030-08-10T00:00:00Z",
                    "reason": "",
                }
            ],
            [
                {
                    "host": "gpu-01",
                    "condition_key": "cpu\nlog",
                    "action": "silenced",
                    "until": "2030-08-10T00:00:00Z",
                    "reason": "",
                }
            ],
        )
        for actions in cases:
            with self.subTest(actions=actions):
                value = valid_config()
                value.update(
                    {
                        "auto_discover": False,
                        "hosts": ["gpu-01"],
                        "incident_actions": actions,
                    }
                )
                with self.assertRaisesRegex(ConfigError, "incident_actions"):
                    load_config(self.write(value))

        value = valid_config()
        value.update(
            {
                "auto_discover": False,
                "hosts": ["gpu-01"],
                "incident_overrides": {
                    "hosts": {"gpu-01": {"exclude_disk_mounts": ["relative"]}}
                },
            }
        )
        with self.assertRaisesRegex(ConfigError, "exclude_disk_mounts"):
            load_config(self.write(value))

    def test_rejects_unknown_keys(self) -> None:
        value = valid_config()
        value["surprise"] = True
        with self.assertRaisesRegex(ConfigError, "unknown config keys"):
            load_config(self.write(value))

    def test_rejects_duplicate_json_keys_at_any_depth(self) -> None:
        root_duplicate = json.dumps(valid_config())[:-1] + ', "listen_port": 9999}'
        with self.assertRaisesRegex(ConfigError, "duplicate JSON key.*listen_port"):
            load_config(self.write_raw(root_duplicate))

        # The same alias declared twice would silently drop the first window
        # and bypass the "exactly one of until/recurrence" validation.
        value = valid_config()
        value.update({"auto_discover": False, "hosts": ["gpu-1"]})
        nested_duplicate = json.dumps(value)[:-1] + (
            ', "maintenance_windows": {'
            '"gpu-1": {"until": "2030-06-15T12:30:00Z"}, '
            '"gpu-1": {"reason": "x", "recurrence": '
            '{"weekday": 0, "start": "00:00", "duration_minutes": 60}}}}'
        )
        with self.assertRaisesRegex(
            ConfigError, r"duplicate JSON key.*maintenance_windows\.gpu-1"
        ):
            load_config(self.write_raw(nested_duplicate))

    def test_rejects_non_utf8_config_files_with_location(self) -> None:
        path = self.write_raw(b'{"ssh_config": "\xff\xfe"}')
        with self.assertRaises(ConfigError) as context:
            load_config(path)
        message = str(context.exception)
        self.assertIn("not valid UTF-8", message)
        self.assertIn(str(path), message)
        self.assertIn("byte 16", message)

    def test_loads_and_bounds_optional_history_persistence(self) -> None:
        value = valid_config()
        value["persistence"] = {
            "enabled": True,
            "retention_hours": 24,
            "max_bytes": 16_777_216,
        }

        persistence = load_config(self.write(value)).persistence

        self.assertTrue(persistence.enabled)
        self.assertEqual(persistence.retention_hours, 24)
        self.assertEqual(persistence.max_bytes, 16_777_216)

        invalid_values = (
            {"enabled": "yes"},
            {"enabled": False, "unknown": 1},
            {"enabled": True, "retention_hours": 0},
            {"enabled": True, "retention_hours": 1.5},
            {"enabled": True, "max_bytes": 1024},
            {"enabled": True, "max_bytes": 16_777_216.0},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                candidate = valid_config()
                candidate["persistence"] = invalid
                with self.assertRaisesRegex(ConfigError, "persistence"):
                    load_config(self.write(candidate))

    def test_validates_read_only_workload_metadata_mode(self) -> None:
        for mode in ("auto", "identity"):
            value = valid_config()
            value["workloads"] = {"mode": mode}
            with self.subTest(mode=mode):
                self.assertEqual(load_config(self.write(value)).workloads.mode, mode)

        for invalid in (None, {}, {"mode": "slurm-write"}, {"unknown": True}):
            with self.subTest(invalid=invalid):
                candidate = valid_config()
                candidate["workloads"] = invalid
                with self.assertRaisesRegex(ConfigError, "workloads"):
                    load_config(self.write(candidate))

    def test_validates_bounded_environment_backed_webhooks(self) -> None:
        value = valid_config()
        value["webhooks"] = [
            {
                "name": "operations",
                "url_env": "MOCOP_OPS_WEBHOOK_URL",
                "secret_env": "MOCOP_OPS_WEBHOOK_SECRET",
                "events": ["opened", "resolved"],
                "timeout_seconds": 4,
                "max_attempts": 3,
                "retry_base_seconds": 1,
                "min_interval_seconds": 2,
                "allow_private_networks": False,
            }
        ]

        webhook = load_config(self.write(value)).webhooks[0]

        self.assertEqual(webhook.name, "operations")
        self.assertEqual(webhook.events, ("opened", "resolved"))
        self.assertEqual(webhook.timeout_seconds, 4)
        self.assertFalse(webhook.allow_private_networks)

        invalid_values = (
            {},
            {"name": "ops", "url_env": "bad-name"},
            {"name": "ops", "url_env": "MOCOP_URL", "events": ["unknown"]},
            {"name": "ops", "url_env": "MOCOP_URL", "timeout_seconds": 31},
            {"name": "ops", "url_env": "MOCOP_URL", "unknown": True},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                candidate = valid_config()
                candidate["webhooks"] = [invalid]
                with self.assertRaisesRegex(ConfigError, "webhooks"):
                    load_config(self.write(candidate))

    def test_validates_retry_jitter_percentage(self) -> None:
        value = valid_config()
        value["retry_jitter_pct"] = 50
        self.assertEqual(load_config(self.write(value)).retry_jitter_pct, 50)

        value["retry_jitter_pct"] = 51
        with self.assertRaisesRegex(ConfigError, "retry_jitter_pct"):
            load_config(self.write(value))

    def test_loads_a_bounded_logical_connection_topology(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["gpu-1", "unmapped"]
        value["exclude_hosts"] = ["gateway"]
        value["topology"] = {
            "root": "monitor",
            "links": [
                {
                    "source": "monitor",
                    "target": "gateway",
                    "transport": "frp-stcp",
                    "label": "STCP · 7005",
                },
                {
                    "source": "gateway",
                    "target": "gpu-1",
                    "transport": "ssh",
                },
            ],
        }

        topology = load_config(self.write(value)).topology

        self.assertIsNotNone(topology)
        assert topology is not None
        self.assertEqual(
            topology.to_dict(),
            {
                "root": "monitor",
                "links": [
                    {
                        "source": "monitor",
                        "target": "gateway",
                        "transport": "frp-stcp",
                        "label": "STCP · 7005",
                    },
                    {
                        "source": "gateway",
                        "target": "gpu-1",
                        "transport": "ssh",
                    },
                ],
            },
        )

    def test_rejects_unsafe_or_ambiguous_connection_topologies(self) -> None:
        base = valid_config()
        base["auto_discover"] = False
        base["hosts"] = ["gpu-1", "gpu-2"]
        cases = (
            None,
            {"root": "monitor", "links": [], "unknown": True},
            {"root": "--monitor", "links": []},
            {
                "root": "monitor",
                "links": [{"source": "monitor", "target": "gpu-1", "transport": "tcp"}],
            },
            {
                "root": "monitor",
                "links": [
                    {
                        "source": "monitor",
                        "target": "gpu-1",
                        "transport": "ssh",
                        "label": "bad\nlabel",
                    }
                ],
            },
            {
                "root": "monitor",
                "links": [
                    {"source": "monitor", "target": "gpu-1", "transport": "ssh"},
                    {"source": "gateway", "target": "gpu-1", "transport": "ssh"},
                ],
            },
            {
                "root": "monitor",
                "links": [
                    {"source": "monitor", "target": "gateway", "transport": "ssh"},
                    {"source": "gateway", "target": "monitor", "transport": "ssh"},
                ],
            },
            {
                "root": "monitor",
                "links": [{"source": "gpu-2", "target": "gpu-1", "transport": "ssh"}],
            },
            {
                "root": "monitor",
                "links": [{"source": "gpu-1", "target": "gpu-1", "transport": "ssh"}],
            },
            {
                "root": "monitor",
                "links": [
                    {
                        "source": "monitor",
                        "target": "--missing",
                        "transport": "ssh",
                    }
                ],
            },
        )

        for topology in cases:
            with self.subTest(topology=topology):
                value = dict(base)
                value["topology"] = topology
                with self.assertRaisesRegex(ConfigError, "topology"):
                    load_config(self.write(value))

    def test_rejects_timeout_order(self) -> None:
        value = valid_config()
        value["probe_timeout_seconds"] = 5
        value["connect_timeout_seconds"] = 5
        with self.assertRaisesRegex(ConfigError, "must be greater"):
            load_config(self.write(value))

    def test_rejects_fractional_integer_fields(self) -> None:
        for key in (
            "connect_timeout_seconds",
            "max_output_bytes",
            "max_workers",
            "listen_port",
            "history_points",
            "incident_history_points",
        ):
            with self.subTest(key=key):
                value = valid_config()
                value[key] = 8.5
                with self.assertRaisesRegex(ConfigError, "must be an integer"):
                    load_config(self.write(value))

    def test_rejects_unsafe_host_and_exclusion_aliases_at_startup(self) -> None:
        for key in ("hosts", "exclude_hosts"):
            with self.subTest(key=key):
                value = valid_config()
                value[key] = ["--proxy-command=bad"]
                with self.assertRaisesRegex(ConfigError, "host aliases"):
                    load_config(self.write(value))

    def test_allows_empty_explicit_whitelist_for_safe_first_start(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        config = load_config(self.write(value))
        self.assertEqual(config.hosts, ())
        self.assertFalse(config.auto_discover)

    def test_loads_and_bounds_thresholds(self) -> None:
        value = valid_config()
        value["thresholds"] = {
            "cpu_warning_pct": 77,
            "gpu_temperature_warning_c": 91,
            "gpu_memory_warning_pct": 88,
            "gpu_idle_memory_pct": 25,
        }
        config = load_config(self.write(value))
        self.assertEqual(config.thresholds.cpu_warning_pct, 77)
        self.assertEqual(config.thresholds.gpu_temperature_warning_c, 91)
        self.assertEqual(config.thresholds.disk_warning_pct, 85)
        self.assertEqual(config.thresholds.gpu_memory_warning_pct, 88)
        self.assertEqual(config.thresholds.gpu_idle_memory_pct, 25)

        value["thresholds"] = {"disk_warning_pct": 101}
        with self.assertRaisesRegex(ConfigError, "must be between"):
            load_config(self.write(value))

    def test_rejects_numbers_too_large_for_float_conversion(self) -> None:
        huge = 10**400
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"poll_interval_seconds": huge}, "poll_interval_seconds must be between"),
            ({"poll_interval_seconds": -huge}, "poll_interval_seconds must be between"),
            (
                {"thresholds": {"cpu_warning_pct": huge}},
                "cpu_warning_pct must be between",
            ),
            (
                {
                    "auto_discover": False,
                    "hosts": ["gpu-1"],
                    "host_overrides": {"gpu-1": {"poll_interval_seconds": huge}},
                },
                "host_overrides.gpu-1.poll_interval_seconds must be between",
            ),
            (
                {
                    "webhooks": [
                        {
                            "name": "ops",
                            "url_env": "MOCOP_URL",
                            "timeout_seconds": huge,
                        }
                    ]
                },
                "timeout_seconds must be between",
            ),
            (
                {
                    "auto_discover": False,
                    "hosts": ["gpu-1"],
                    "incident_overrides": {
                        "hosts": {"gpu-1": {"thresholds": {"cpu_warning_pct": huge}}}
                    },
                },
                "cpu_warning_pct must be between",
            ),
        )
        for update, expected in cases:
            with self.subTest(expected=expected):
                value = valid_config()
                value.update(update)
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.write(value))

    def test_validates_expected_gpu_counts_against_explicit_hosts(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["gpu-1", "gpu-2"]
        value["expected_gpu_counts"] = {"gpu-1": 8, "gpu-2": 0}

        config = load_config(self.write(value))

        self.assertEqual(dict(config.expected_gpu_counts), {"gpu-1": 8, "gpu-2": 0})

        for invalid in (
            {"unknown": 8},
            {"gpu-1": -1},
            {"gpu-1": 257},
            {"gpu-1": 1.5},
            {"--bad": 1},
        ):
            with self.subTest(invalid=invalid):
                value["expected_gpu_counts"] = invalid
                with self.assertRaises(ConfigError):
                    load_config(self.write(value))

        value["expected_gpu_counts"] = {"gpu-1": 8}
        value["exclude_hosts"] = ["gpu-1"]
        with self.assertRaisesRegex(ConfigError, "cannot be excluded"):
            load_config(self.write(value))

    def test_validates_shared_host_groups_against_explicit_hosts(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["gpu-1", "gpu-2"]
        value["host_groups"] = {"gpu-1": " Training ", "gpu-2": "Inference"}

        config = load_config(self.write(value))

        self.assertEqual(
            dict(config.host_groups),
            {"gpu-1": "Training", "gpu-2": "Inference"},
        )
        self.assertEqual(config.host_group("gpu-1"), "Training")
        self.assertIsNone(config.host_group("unknown"))

        for invalid in (
            {"unknown": "Training"},
            {"gpu-1": ""},
            {"gpu-1": "x\n"},
            {"gpu-1": "x\u007f"},
            {"gpu-1": "x\u202e"},
            {"gpu-1": "x" * 49},
            {"--bad": "Training"},
        ):
            with self.subTest(invalid=invalid):
                value["host_groups"] = invalid
                with self.assertRaises(ConfigError):
                    load_config(self.write(value))

        value["host_groups"] = {"gpu-1": "Training"}
        value["exclude_hosts"] = ["gpu-1"]
        with self.assertRaisesRegex(ConfigError, "cannot be excluded"):
            load_config(self.write(value))

    def test_rebuilds_host_lookup_indexes_when_config_is_replaced(self) -> None:
        original_data = valid_config()
        original_data.update(
            {
                "auto_discover": False,
                "hosts": ["gpu-1"],
                "host_groups": {"gpu-1": "Training"},
                "host_overrides": {"gpu-1": {"poll_interval_seconds": 10}},
                "maintenance_windows": {"gpu-1": {"until": "2030-06-15T12:30:00Z"}},
            }
        )
        replacement_data = valid_config()
        replacement_data.update(
            {
                "auto_discover": False,
                "hosts": ["gpu-2"],
                "host_groups": {"gpu-2": "Inference"},
                "host_overrides": {"gpu-2": {"poll_interval_seconds": 20}},
                "maintenance_windows": {"gpu-2": {"until": "2031-06-15T12:30:00Z"}},
            }
        )
        original = load_config(self.write(original_data))
        replacement = load_config(self.write(replacement_data))

        updated = replace(
            original,
            host_overrides=replacement.host_overrides,
            maintenance_windows=replacement.maintenance_windows,
            host_groups=replacement.host_groups,
        )

        self.assertIsNone(updated.host_override("gpu-1"))
        self.assertIsNone(updated.maintenance_window("gpu-1"))
        self.assertIsNone(updated.host_group("gpu-1"))
        self.assertEqual(updated.host_override("gpu-2").poll_interval_seconds, 20)
        self.assertEqual(
            updated.maintenance_window("gpu-2").to_dict()["until"],
            "2031-06-15T12:30:00Z",
        )
        self.assertEqual(updated.host_group("gpu-2"), "Inference")

    def test_validates_incident_stability_configuration(self) -> None:
        value = valid_config()
        value["incidents"] = {
            "resource_open_cycles": 3,
            "recovery_cycles": 4,
            "gpu_idle_memory_cycles": 20,
        }

        config = load_config(self.write(value))

        self.assertEqual(config.incidents.resource_open_cycles, 3)
        self.assertEqual(config.incidents.recovery_cycles, 4)
        self.assertEqual(config.incidents.gpu_idle_memory_cycles, 20)

        for invalid in (0, 61, 2.5, True):
            with self.subTest(invalid=invalid):
                value["incidents"]["recovery_cycles"] = invalid
                with self.assertRaisesRegex(ConfigError, "incidents.recovery_cycles"):
                    load_config(self.write(value))

    def test_validates_per_host_collection_overrides(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["gpu-1"]
        value["host_overrides"] = {
            "gpu-1": {
                "poll_interval_seconds": 30,
                "probe_timeout_seconds": 20,
            }
        }

        config = load_config(self.write(value))

        override = config.host_override("gpu-1")
        self.assertIsNotNone(override)
        self.assertEqual(override.poll_interval_seconds, 30)
        self.assertEqual(override.probe_timeout_seconds, 20)
        self.assertIsNone(config.host_override("unknown"))

        invalid_overrides = (
            {"unknown": {"poll_interval_seconds": 30}},
            {"gpu-1": {}},
            {"gpu-1": {"poll_interval_seconds": 0}},
            {"gpu-1": {"probe_timeout_seconds": 5}},
            {"gpu-1": {"surprise": 20}},
            {"gpu-1": "slow"},
        )
        for invalid in invalid_overrides:
            with self.subTest(invalid=invalid):
                value["host_overrides"] = invalid
                with self.assertRaises(ConfigError):
                    load_config(self.write(value))

    def test_validates_time_bounded_maintenance_windows(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["gpu-1", "gpu-2"]
        value["maintenance_windows"] = {
            "gpu-1": {
                "until": "2030-06-15T12:30:00Z",
                "reason": "Driver upgrade",
            }
        }

        config = load_config(self.write(value))

        window = config.maintenance_window("gpu-1")
        self.assertIsNotNone(window)
        self.assertEqual(window.reason, "Driver upgrade")
        self.assertEqual(window.to_dict()["until"], "2030-06-15T12:30:00Z")
        self.assertTrue(
            window.is_active(datetime(2030, 6, 15, 12, 29, tzinfo=timezone.utc))
        )
        self.assertFalse(
            window.is_active(datetime(2030, 6, 15, 12, 30, tzinfo=timezone.utc))
        )
        self.assertIsNone(config.maintenance_window("gpu-2"))

        invalid_windows = (
            {"unknown": {"until": "2030-06-15T12:30:00Z"}},
            {"gpu-1": {"until": "2030-06-15T12:30:00+00:00"}},
            {"gpu-1": {"until": "not-a-time"}},
            {"gpu-1": {"until": "2030-06-15T12:30:00Z", "reason": 1}},
            {"gpu-1": {"until": "2030-06-15T12:30:00Z", "reason": "x\n"}},
            {"gpu-1": {"until": "2030-06-15T12:30:00Z", "reason": "x\u007f"}},
            {"gpu-1": {"until": "2030-06-15T12:30:00Z", "reason": "x\u202e"}},
            {"gpu-1": {"until": "2030-06-15T12:30:00Z", "extra": True}},
            {"gpu-1": {}},
        )
        for invalid in invalid_windows:
            with self.subTest(invalid=invalid):
                value["maintenance_windows"] = invalid
                with self.assertRaises(ConfigError):
                    load_config(self.write(value))

    def test_validates_recurring_maintenance_windows(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["gpu-1"]
        value["maintenance_windows"] = {
            "gpu-1": {
                "reason": "Weekly patching",
                "recurrence": {
                    "weekday": 2,
                    "start": "18:00",
                    "duration_minutes": 120,
                },
            }
        }

        window = load_config(self.write(value)).maintenance_window("gpu-1")

        self.assertTrue(window.recurring)
        # 2030-06-19 is a Wednesday (weekday 2). Active inside the window,
        # inactive before start and after the end, active again next week.
        inside = datetime(2030, 6, 19, 19, 0, tzinfo=timezone.utc)
        before = datetime(2030, 6, 19, 17, 59, tzinfo=timezone.utc)
        after = datetime(2030, 6, 19, 20, 0, tzinfo=timezone.utc)
        next_week = datetime(2030, 6, 26, 18, 30, tzinfo=timezone.utc)
        self.assertTrue(window.is_active(inside))
        self.assertFalse(window.is_active(before))
        self.assertFalse(window.is_active(after))
        self.assertTrue(window.is_active(next_week))
        rendered = window.to_dict(inside)
        self.assertEqual(rendered["until"], "2030-06-19T20:00:00Z")
        self.assertTrue(rendered["recurring"])
        # Before the start, the rendered expiry points at the next instance.
        self.assertEqual(window.to_dict(before)["until"], "2030-06-19T20:00:00Z")
        self.assertEqual(window.to_dict(after)["until"], "2030-06-26T20:00:00Z")

        # A window that crosses midnight stays active into the next day.
        value["maintenance_windows"] = {
            "gpu-1": {
                "reason": "Overnight",
                "recurrence": {
                    "weekday": 4,
                    "start": "23:00",
                    "duration_minutes": 180,
                },
            }
        }
        overnight = load_config(self.write(value)).maintenance_window("gpu-1")
        # 2030-06-21 is a Friday (weekday 4); 01:00 Saturday is inside.
        saturday_early = datetime(2030, 6, 22, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(overnight.is_active(saturday_early))

        invalid_windows = (
            {
                "gpu-1": {
                    "reason": "x",
                    "until": "2030-06-15T12:30:00Z",
                    "recurrence": {
                        "weekday": 0,
                        "start": "00:00",
                        "duration_minutes": 1,
                    },
                }
            },
            {
                "gpu-1": {
                    "reason": "x",
                    "recurrence": {
                        "weekday": 7,
                        "start": "00:00",
                        "duration_minutes": 1,
                    },
                }
            },
            {
                "gpu-1": {
                    "reason": "x",
                    "recurrence": {
                        "weekday": True,
                        "start": "00:00",
                        "duration_minutes": 1,
                    },
                }
            },
            {
                "gpu-1": {
                    "reason": "x",
                    "recurrence": {
                        "weekday": 0,
                        "start": "24:00",
                        "duration_minutes": 1,
                    },
                }
            },
            {
                "gpu-1": {
                    "reason": "x",
                    "recurrence": {
                        "weekday": 0,
                        "start": "0:00",
                        "duration_minutes": 1,
                    },
                }
            },
            {
                "gpu-1": {
                    "reason": "x",
                    "recurrence": {
                        "weekday": 0,
                        "start": "00:00",
                        "duration_minutes": 0,
                    },
                }
            },
            {
                "gpu-1": {
                    "reason": "x",
                    "recurrence": {
                        "weekday": 0,
                        "start": "00:00",
                        "duration_minutes": 10081,
                    },
                }
            },
            {"gpu-1": {"reason": "x", "recurrence": {"weekday": 0, "start": "00:00"}}},
            {
                "gpu-1": {
                    "reason": "x",
                    "recurrence": {
                        "weekday": 0,
                        "start": "00:00",
                        "duration_minutes": 1,
                        "extra": 1,
                    },
                }
            },
        )
        for invalid in invalid_windows:
            with self.subTest(invalid=invalid):
                value["maintenance_windows"] = invalid
                with self.assertRaises(ConfigError):
                    load_config(self.write(value))

    def test_bounds_recurring_window_duration_strictly_below_one_week(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["gpu-1"]
        value["maintenance_windows"] = {
            "gpu-1": {
                "reason": "Long window",
                "recurrence": {
                    "weekday": 0,
                    "start": "00:00",
                    "duration_minutes": 10_079,
                },
            }
        }

        window = load_config(self.write(value)).maintenance_window("gpu-1")

        self.assertEqual(window.duration_minutes, 10_079)
        # 2030-06-17 is a Monday; the instance ends 23:59 on Sunday the 23rd,
        # so the window goes inactive in the minute before the next instance.
        self.assertTrue(
            window.is_active(datetime(2030, 6, 23, 23, 58, tzinfo=timezone.utc))
        )
        self.assertFalse(
            window.is_active(datetime(2030, 6, 23, 23, 59, 30, tzinfo=timezone.utc))
        )

        # A full week would make consecutive instances seamless and the
        # window permanently active, so exactly one week is rejected.
        value["maintenance_windows"]["gpu-1"]["recurrence"]["duration_minutes"] = 10_080
        with self.assertRaisesRegex(
            ConfigError, r"duration_minutes must be 1 to 10079 \(less than one week\)"
        ):
            load_config(self.write(value))

    def test_validates_host_display_names(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["gpu-1"]
        value["host_overrides"] = {"gpu-1": {"display_name": "  训练节点 A100 × 8  "}}

        config = load_config(self.write(value))

        self.assertEqual(
            config.host_override("gpu-1").display_name, "训练节点 A100 × 8"
        )
        self.assertEqual(config.host_display_names(), (("gpu-1", "训练节点 A100 × 8"),))

        for invalid in ("", "   ", "x" * 65, "bad\nname", "bad\u202ename", 42):
            with self.subTest(invalid=invalid):
                value["host_overrides"] = {"gpu-1": {"display_name": invalid}}
                with self.assertRaises(ConfigError):
                    load_config(self.write(value))

    def test_rejects_line_and_paragraph_separators_in_text_fields(self) -> None:
        for separator in ("\u2028", "\u2029"):
            with self.subTest(separator=separator, field="display_name"):
                value = valid_config()
                value["auto_discover"] = False
                value["hosts"] = ["gpu-1"]
                value["host_overrides"] = {
                    "gpu-1": {"display_name": f"bad{separator}name"}
                }
                with self.assertRaisesRegex(ConfigError, "display_name"):
                    load_config(self.write(value))
            with self.subTest(separator=separator, field="reason"):
                value = valid_config()
                value["auto_discover"] = False
                value["hosts"] = ["gpu-1"]
                value["maintenance_windows"] = {
                    "gpu-1": {
                        "until": "2030-06-15T12:30:00Z",
                        "reason": f"bad{separator}reason",
                    }
                }
                with self.assertRaisesRegex(ConfigError, "reason"):
                    load_config(self.write(value))

    def test_bounds_history_points(self) -> None:
        value = valid_config()
        value["history_points"] = 120
        self.assertEqual(load_config(self.write(value)).history_points, 120)
        value["history_points"] = 2
        with self.assertRaisesRegex(ConfigError, "history_points must be between"):
            load_config(self.write(value))

    def test_bounds_incident_history_points(self) -> None:
        value = valid_config()
        value["incident_history_points"] = 200
        self.assertEqual(load_config(self.write(value)).incident_history_points, 200)
        value["incident_history_points"] = 5001
        with self.assertRaisesRegex(
            ConfigError, "incident_history_points must be between"
        ):
            load_config(self.write(value))

    def test_bounds_collection_stale_cycles(self) -> None:
        value = valid_config()
        value["collection_stale_cycles"] = 5
        self.assertEqual(load_config(self.write(value)).collection_stale_cycles, 5)

        for invalid in (1, 13, 3.5, True):
            with self.subTest(invalid=invalid):
                value["collection_stale_cycles"] = invalid
                with self.assertRaisesRegex(
                    ConfigError, "collection_stale_cycles must be between"
                ):
                    load_config(self.write(value))

    def test_bounds_gpu_process_poll_interval(self) -> None:
        value = valid_config()
        value["gpu_process_poll_interval_seconds"] = 30
        self.assertEqual(
            load_config(self.write(value)).gpu_process_poll_interval_seconds,
            30,
        )

        for invalid in (1, 3601, True, "15"):
            with self.subTest(invalid=invalid):
                value["gpu_process_poll_interval_seconds"] = invalid
                with self.assertRaisesRegex(
                    ConfigError,
                    "gpu_process_poll_interval_seconds must be",
                ):
                    load_config(self.write(value))

    def test_example_uses_an_explicit_host_whitelist(self) -> None:
        example = (
            Path(__file__).resolve().parents[1] / "examples" / "mocop.example.json"
        )
        config = load_config(example)

        self.assertFalse(config.auto_discover)
        self.assertEqual(
            config.hosts,
            ("monitor-host", "gpu-node-01", "gpu-node-02"),
        )
        self.assertEqual(config.exclude_hosts, frozenset({"gpu-gateway"}))
        self.assertEqual(config.poll_interval_seconds, 5)
        self.assertEqual(config.local_host, "monitor-host")
        self.assertIsNotNone(config.topology)
        assert config.topology is not None
        self.assertEqual(config.topology.root, "monitor-host")

    def test_local_host_must_be_an_explicit_non_excluded_target(self) -> None:
        value = valid_config()
        value["auto_discover"] = False
        value["hosts"] = ["star-0", "gpu-1"]
        value["local_host"] = "star-0"
        path = self.write(value)

        self.assertEqual(load_config(path).local_host, "star-0")

        value["hosts"] = ["gpu-1"]
        path = self.write(value)
        with self.assertRaisesRegex(ConfigError, "local_host must also appear"):
            load_config(path)

        value["hosts"] = ["star-0", "gpu-1"]
        value["exclude_hosts"] = ["star-0"]
        path = self.write(value)
        with self.assertRaisesRegex(ConfigError, "local_host cannot be excluded"):
            load_config(path)

    def test_bundled_default_is_safe_and_loadable(self) -> None:
        config = load_config(BUNDLED_CONFIG_PATH)
        self.assertFalse(config.auto_discover)
        self.assertEqual(config.hosts, ())
        self.assertEqual(config.poll_interval_seconds, 5)
        self.assertEqual(config.max_output_bytes, 2_097_152)

    def test_resolves_explicit_environment_user_project_and_bundled_paths(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        explicit = root / "explicit.json"
        environment = root / "environment.json"
        user = root / "xdg" / "mocop" / "config.json"
        project = root / "project" / "config" / "mocop.json"
        for path in (explicit, environment, user, project):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(valid_config()), encoding="utf-8")

        values = {
            CONFIG_ENV_VAR: str(environment),
            "XDG_CONFIG_HOME": str(root / "xdg"),
        }
        self.assertEqual(
            resolve_config_path(explicit, environ=values, cwd=root), explicit
        )
        self.assertEqual(resolve_config_path(environ=values, cwd=root), environment)
        values.pop(CONFIG_ENV_VAR)
        self.assertEqual(resolve_config_path(environ=values, cwd=root), user)
        user.unlink()
        self.assertEqual(
            resolve_config_path(environ=values, cwd=root / "project"), project
        )
        project.unlink()
        self.assertEqual(
            resolve_config_path(environ=values, cwd=root),
            BUNDLED_CONFIG_PATH.resolve(),
        )

    def test_relative_ssh_config_is_resolved_from_monitor_config(self) -> None:
        value = valid_config()
        value["ssh_config"] = "ssh/config"
        path = self.write(value)

        config = load_config(path)

        self.assertEqual(config.ssh_config, path.parent / "ssh" / "config")


if __name__ == "__main__":
    unittest.main()
