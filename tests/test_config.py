from __future__ import annotations

import json
import tempfile
import unittest
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

    def test_loads_valid_config(self) -> None:
        config = load_config(self.write(valid_config()))
        self.assertTrue(config.auto_discover)
        self.assertEqual(config.max_workers, 8)
        self.assertEqual(config.listen_port, 8787)
        self.assertEqual(config.history_points, 720)
        self.assertEqual(config.incident_history_points, 500)
        self.assertEqual(config.collection_stale_cycles, 3)
        self.assertEqual(config.expected_gpu_counts, ())
        self.assertEqual(config.host_overrides, ())
        self.assertEqual(config.maintenance_windows, ())
        self.assertEqual(config.incidents.resource_open_cycles, 2)
        self.assertEqual(config.incidents.recovery_cycles, 2)
        self.assertEqual(config.incidents.gpu_idle_memory_cycles, 12)

    def test_rejects_unknown_keys(self) -> None:
        value = valid_config()
        value["surprise"] = True
        with self.assertRaisesRegex(ConfigError, "unknown config keys"):
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
            {"gpu-1": {"until": "2030-06-15T12:30:00Z", "extra": True}},
            {"gpu-1": {}},
        )
        for invalid in invalid_windows:
            with self.subTest(invalid=invalid):
                value["maintenance_windows"] = invalid
                with self.assertRaises(ConfigError):
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

    def test_example_uses_an_explicit_host_whitelist(self) -> None:
        example = (
            Path(__file__).resolve().parents[1] / "examples" / "mocop.example.json"
        )
        config = load_config(example)

        self.assertFalse(config.auto_discover)
        self.assertEqual(config.hosts, ("gpu-node-01", "gpu-node-02"))
        self.assertEqual(config.exclude_hosts, frozenset())
        self.assertEqual(config.poll_interval_seconds, 5)
        self.assertIsNone(config.local_host)

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
