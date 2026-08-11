from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mocop.config import PersistenceConfig
from mocop.incidents import IncidentCondition, IncidentEvent
from mocop.models import GpuMetrics, ProbeResult, SystemMetrics
from mocop.persistence import (
    LoadedTelemetry,
    SqliteTelemetryPersistence,
    user_state_path,
)
from mocop.service import StateStore


def history_point(observed_at: str, cpu: float) -> dict[str, object]:
    return {
        "observedAt": observed_at,
        "cpuUsagePct": cpu,
        "memoryUsagePct": 20.0,
        "swapUsagePct": 0.0,
        "diskUsagePct": 40.0,
        "networkRxBps": 100.0,
        "networkTxBps": 200.0,
        "diskReadBps": 300.0,
        "diskWriteBps": 400.0,
        "gpuUsagePct": 50.0,
        "gpuMemoryUsagePct": 60.0,
        "gpuTemperatureC": 70.0,
    }


def incident_event(event_id: int, observed_at: str) -> IncidentEvent:
    return IncidentEvent(
        event_id=event_id,
        host="gpu-01",
        condition=IncidentCondition(
            key="connectivity",
            category="connectivity",
            resource="SSH",
            severity="critical",
            value=None,
            threshold=None,
            observed_at=observed_at,
            detail="SSH connection timed out",
        ),
        state="opened",
        observed_at=observed_at,
    )


class SqliteTelemetryPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / "history.sqlite3"
        self.config = PersistenceConfig(
            enabled=True,
            retention_hours=24,
            max_bytes=8_388_608,
        )

    def test_roundtrips_bounded_history_and_incident_context(self) -> None:
        store = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(store.close)
        store.record_history("gpu-01", history_point("2026-08-10T00:00:00Z", 10))
        store.record_history("gpu-01", history_point("2026-08-10T00:00:05Z", 20))
        store.record_incidents((incident_event(7, "2026-08-10T00:00:06Z"),))
        self.assertTrue(store.flush())

        loaded = store.load(history_points=1, incident_points=10)

        self.assertEqual(len(loaded.history["gpu-01"]), 1)
        self.assertEqual(loaded.history["gpu-01"][0]["cpuUsagePct"], 20)
        self.assertEqual(loaded.incident_events[0].event_id, 7)
        self.assertEqual(
            loaded.incident_events[0].condition.detail, "SSH connection timed out"
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        status = store.status()
        self.assertTrue(status["healthy"])
        self.assertEqual(status["droppedWrites"], 0)

    def test_roundtrips_gpu_samples_and_process_transitions_as_one_batch(self) -> None:
        store = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(store.close)
        store.record_gpu_telemetry(
            "gpu-01",
            (
                {
                    "observedAt": "2026-08-10T00:00:00Z",
                    "gpuId": "GPU-1",
                    "index": 0,
                    "utilizationGpuPct": 50,
                    "memoryUsedMiB": 1024,
                    "memoryTotalMiB": 8192,
                    "temperatureC": 60,
                    "powerDrawW": 120,
                },
                {
                    "observedAt": "2026-08-10T00:00:05Z",
                    "gpuId": "GPU-1",
                    "index": 0,
                    "utilizationGpuPct": 55,
                    "memoryUsedMiB": 2048,
                    "memoryTotalMiB": 8192,
                    "temperatureC": 61,
                    "powerDrawW": 125,
                },
            ),
            (
                {
                    "observedAt": "2026-08-10T00:00:01Z",
                    "gpuId": "GPU-1",
                    "index": 0,
                    "event": "started",
                    "pid": 42,
                    "name": "train.py",
                    "usedMemoryMiB": 1024,
                    "workload": {"kind": "slurm", "workload_id": "7"},
                },
                {
                    "observedAt": "2026-08-10T00:00:06Z",
                    "gpuId": "GPU-1",
                    "index": 0,
                    "event": "stopped",
                    "pid": 42,
                    "name": "train.py",
                    "usedMemoryMiB": 1024,
                    "workload": {"kind": "slurm", "workload_id": "7"},
                },
            ),
        )
        self.assertTrue(store.flush())

        loaded = store.load(history_points=10, incident_points=10)

        points = loaded.gpu_history[("gpu-01", "GPU-1")]
        events = loaded.process_events[("gpu-01", "GPU-1")]
        self.assertEqual(
            [point["utilizationGpuPct"] for point in points],
            [50, 55],
        )
        self.assertEqual([event["event"] for event in events], ["started", "stopped"])
        self.assertEqual(events[0]["workload"]["workload_id"], "7")
        self.assertEqual(store.status()["writtenRecords"], 4)

    def test_migrates_the_v1_database_without_losing_existing_history(self) -> None:
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            SqliteTelemetryPersistence._create_schema(connection)
            connection.execute("DROP TABLE gpu_history")
            connection.execute("DROP TABLE process_events")
            connection.execute("PRAGMA user_version = 1")

        store = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(store.close)

        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(version, 2)
        self.assertIn("gpu_history", tables)
        self.assertIn("process_events", tables)

    def test_prunes_records_older_than_the_retention_window(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        old_text = old.isoformat(timespec="seconds").replace("+00:00", "Z")
        recent_text = recent.isoformat(timespec="seconds").replace("+00:00", "Z")
        config = PersistenceConfig(
            enabled=True,
            retention_hours=1,
            max_bytes=8_388_608,
        )
        store = SqliteTelemetryPersistence(config, self.path)
        store.record_history("gpu-01", history_point(old_text, 10))
        store.record_history("gpu-01", history_point(recent_text, 20))
        store.record_incidents(
            (incident_event(1, old_text), incident_event(2, recent_text))
        )
        self.assertTrue(store.flush())
        store.close()

        reopened = SqliteTelemetryPersistence(config, self.path)
        self.addCleanup(reopened.close)
        loaded = reopened.load(history_points=10, incident_points=10)

        self.assertEqual(
            [point["observedAt"] for point in loaded.history["gpu-01"]],
            [recent_text],
        )
        self.assertEqual(
            [event.event_id for event in loaded.incident_events],
            [2],
        )

    def test_state_path_honors_the_xdg_state_directory(self) -> None:
        expected = self.path.parent / "mocop" / "history.sqlite3"

        resolved = user_state_path({"XDG_STATE_HOME": str(self.path.parent)})

        self.assertEqual(resolved, expected)

    def test_state_path_prefers_the_systemd_managed_directory(self) -> None:
        resolved = user_state_path(
            {
                "STATE_DIRECTORY": str(self.path.parent),
                "XDG_STATE_HOME": "/ignored",
            }
        )

        self.assertEqual(resolved, self.path.parent / "history.sqlite3")

    def test_state_store_restores_trends_and_transition_context(self) -> None:
        persistence = SqliteTelemetryPersistence(self.config, self.path)
        state = StateStore(
            5,
            persistence=persistence,
            restored=persistence.load(20, 20),
        )
        state.set_hosts(("gpu-01",))
        system = SystemMetrics(
            hostname="gpu-01",
            uptime_seconds=100,
            load_1m=1,
            load_5m=1,
            load_15m=1,
            cpu_cores=8,
            cpu_usage_pct=25,
            memory_total_mib=100,
            memory_used_mib=20,
            memory_available_mib=80,
            swap_total_mib=0,
            swap_used_mib=0,
            disk_total_mib=100,
            disk_used_mib=10,
            network_rx_bps=1,
            network_tx_bps=2,
        )
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
        )
        state.apply(ProbeResult("gpu-01", "online", 1, (gpu,), system=system))
        state.apply(ProbeResult("gpu-01", "unreachable", 1))
        self.assertTrue(persistence.flush())
        persistence.close()

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        restored = reopened.load(20, 20)
        restarted_state = StateStore(5, persistence=reopened, restored=restored)
        restarted_state.set_hosts(("gpu-01",))

        self.assertEqual(len(restarted_state.history("gpu-01", 20)["points"]), 1)
        self.assertEqual(
            restarted_state.gpu_history("gpu-01", "GPU-1", 20)["points"][0][
                "utilizationGpuPct"
            ],
            50,
        )
        self.assertEqual(
            restarted_state.incidents(20)["events"][0]["category"],
            "connectivity",
        )

    def test_state_store_discards_restored_history_for_unconfigured_hosts(self) -> None:
        gpu_point = {
            "observedAt": "2026-08-10T00:00:00Z",
            "gpuId": "GPU-retired",
            "index": 0,
            "utilizationGpuPct": 20.0,
            "memoryUsedMiB": 100.0,
            "memoryTotalMiB": 1000.0,
            "temperatureC": 50.0,
            "powerDrawW": 80.0,
        }
        restored = LoadedTelemetry(
            history={"retired": (history_point("2026-08-10T00:00:00Z", 20),)},
            incident_events=(),
            gpu_history={("retired", "GPU-retired"): (gpu_point,)},
        )
        state = StateStore(5, restored=restored)

        state.set_hosts(("active",))
        state.set_hosts(("active", "retired"))

        self.assertEqual(state.history("retired", 10)["points"], [])
        self.assertIsNone(state.gpu_history("retired", "GPU-retired", 10))

    def test_load_skips_corrupt_dynamic_types_without_losing_valid_rows(self) -> None:
        store = SqliteTelemetryPersistence(self.config, self.path)
        store.record_history("gpu-01", history_point("2026-08-10T00:00:00Z", 10))
        store.record_incidents((incident_event(1, "2026-08-10T00:00:01Z"),))
        self.assertTrue(store.flush())
        store.close()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE history SET cpu_usage_pct = 'invalid' WHERE host = 'gpu-01'"
            )
            connection.execute(
                "UPDATE incident_events SET value = 'invalid' WHERE event_id = 1"
            )

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        loaded = reopened.load(20, 20)

        self.assertEqual(loaded.history, {})
        self.assertEqual(loaded.incident_events, ())


if __name__ == "__main__":
    unittest.main()
