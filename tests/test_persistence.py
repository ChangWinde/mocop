from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mocop.config import PersistenceConfig
from mocop.incidents import IncidentCondition, IncidentEvent
from mocop.models import GpuMetrics, ProbeResult, SystemMetrics
from mocop.persistence import (
    LoadedTelemetry,
    SqliteTelemetryPersistence,
    user_state_path,
)
from mocop.service import StateStore


def utc_text(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def history_point(
    observed_at: str, cpu: float, transport_retried: bool = False
) -> dict[str, object]:
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
        "transportRetried": transport_retried,
    }


def gpu_point(observed_at: str, utilization: float) -> dict[str, object]:
    return {
        "observedAt": observed_at,
        "gpuId": "GPU-1",
        "index": 0,
        "utilizationGpuPct": utilization,
        "memoryUsedMiB": 1024.0,
        "memoryTotalMiB": 8192.0,
        "temperatureC": 60.0,
        "powerDrawW": 120.0,
    }


def process_event(
    observed_at: str, gpu_id: str, pid: int, event: str = "started"
) -> dict[str, object]:
    return {
        "observedAt": observed_at,
        "gpuId": gpu_id,
        "index": 0,
        "event": event,
        "pid": pid,
        "name": "train.py",
        "usedMemoryMiB": 512.0,
        "workload": None,
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


# Schemas exactly as historical releases created them, so migrations are
# exercised against real legacy layouts instead of the current DDL.
_LEGACY_TABLES: dict[str, tuple[str, ...]] = {
    "history": (
        """
        CREATE TABLE history (
            host TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            cpu_usage_pct REAL,
            memory_usage_pct REAL,
            swap_usage_pct REAL,
            disk_usage_pct REAL,
            network_rx_bps REAL,
            network_tx_bps REAL,
            disk_read_bps REAL,
            disk_write_bps REAL,
            gpu_usage_pct REAL,
            gpu_memory_usage_pct REAL,
            gpu_temperature_c REAL,
            PRIMARY KEY (host, observed_at)
        ) WITHOUT ROWID
        """,
        "CREATE INDEX history_observed_at ON history(observed_at)",
    ),
    "incident_events": (
        """
        CREATE TABLE incident_events (
            event_id INTEGER PRIMARY KEY,
            host TEXT NOT NULL,
            condition_key TEXT NOT NULL,
            category TEXT NOT NULL,
            resource TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
            value REAL,
            threshold REAL,
            condition_observed_at TEXT NOT NULL,
            detail TEXT,
            group_key TEXT,
            state TEXT NOT NULL CHECK (
                state IN ('opened', 'resolved', 'escalated', 'deescalated')
            ),
            observed_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX incident_events_observed_at ON incident_events(observed_at)",
    ),
    "gpu_history": (
        """
        CREATE TABLE gpu_history (
            host TEXT NOT NULL,
            gpu_id TEXT NOT NULL,
            gpu_index INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            utilization_gpu_pct REAL,
            memory_used_mib REAL,
            memory_total_mib REAL,
            temperature_c REAL,
            power_draw_w REAL,
            PRIMARY KEY (host, gpu_id, observed_at)
        ) WITHOUT ROWID
        """,
        "CREATE INDEX gpu_history_observed_at ON gpu_history(observed_at)",
    ),
    "process_events": (
        """
        CREATE TABLE process_events (
            host TEXT NOT NULL,
            gpu_id TEXT NOT NULL,
            gpu_index INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (
                event_type IN ('started', 'stopped')
            ),
            pid INTEGER NOT NULL,
            name TEXT NOT NULL,
            used_memory_mib REAL,
            workload_json TEXT,
            PRIMARY KEY (
                host, gpu_id, observed_at, event_type, pid, name
            )
        ) WITHOUT ROWID
        """,
        "CREATE INDEX process_events_observed_at ON process_events(observed_at)",
    ),
}
_LEGACY_VERSION_TABLES = {
    0: (),
    1: ("history", "incident_events"),
    2: ("history", "incident_events", "gpu_history", "process_events"),
    3: ("history", "incident_events", "gpu_history", "process_events"),
}


def create_legacy_database(
    path: Path, version: int, tables: tuple[str, ...] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    selected = tables if tables is not None else _LEGACY_VERSION_TABLES[version]
    with closing(sqlite3.connect(path)) as connection, connection:
        for table in selected:
            for statement in _LEGACY_TABLES[table]:
                connection.execute(statement)
        if version >= 3 and "history" in selected:
            connection.execute(
                "ALTER TABLE history"
                " ADD COLUMN transport_retried INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(f"PRAGMA user_version = {version}")


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

    def test_preserves_same_timestamp_process_transition_order(self) -> None:
        observed_at = utc_text(datetime.now(timezone.utc) - timedelta(minutes=5))
        store = SqliteTelemetryPersistence(self.config, self.path)
        store.record_gpu_telemetry(
            "gpu-01",
            (),
            (
                process_event(observed_at, "GPU-1", 42, "stopped"),
                process_event(observed_at, "GPU-1", 42, "started"),
            ),
        )
        self.assertTrue(store.flush())
        store.close()

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        events = reopened.load(10, 10).process_events[("gpu-01", "GPU-1")]

        self.assertEqual([event["event"] for event in events], ["stopped", "started"])

    def test_roundtrips_hidden_usage_anchors_without_exposing_physical_hosts(
        self,
    ) -> None:
        started_at = utc_text(datetime.now(timezone.utc) - timedelta(minutes=10))
        stopped_at = utc_text(datetime.now(timezone.utc) - timedelta(minutes=5))
        hidden_start = process_event(started_at, "GPU-1", 42, "started")
        hidden_start["_visible"] = False
        store = SqliteTelemetryPersistence(self.config, self.path)
        store.record_gpu_telemetry(
            "gpu-01",
            (),
            (hidden_start, process_event(stopped_at, "GPU-1", 42, "stopped")),
        )
        self.assertTrue(store.flush())
        store.close()

        with closing(sqlite3.connect(self.path)) as connection:
            physical_hosts = {
                row[0]
                for row in connection.execute(
                    "SELECT host FROM process_events WHERE event_type = 'started'"
                )
            }
        self.assertNotIn("gpu-01", physical_hosts)
        self.assertTrue(all(host.startswith("\x00") for host in physical_hosts))

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        events = reopened.load(10, 10).process_events[("gpu-01", "GPU-1")]
        self.assertEqual([event["event"] for event in events], ["started", "stopped"])
        self.assertFalse(events[0]["_visible"])
        self.assertNotIn("_visible", events[1])

    def test_v3_migration_assigns_deterministic_process_event_sequence(self) -> None:
        create_legacy_database(self.path, 3)
        observed_at = utc_text(datetime.now(timezone.utc) - timedelta(minutes=5))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany(
                "INSERT INTO process_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        "gpu-01",
                        "GPU-1",
                        0,
                        observed_at,
                        state,
                        42,
                        "train.py",
                        512,
                        None,
                    )
                    for state in ("stopped", "started")
                ),
            )

        store = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(store.close)
        events = store.load(10, 10).process_events[("gpu-01", "GPU-1")]

        self.assertEqual([event["event"] for event in events], ["stopped", "started"])
        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(process_events)")
            }
        self.assertEqual(version, 3)
        self.assertNotIn("sequence", columns)

        # Released v3 code uses a positional nine-value insert.  Preserve that
        # physical contract, not merely the logical column names.
        store.close()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO process_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "gpu-01",
                    "GPU-1",
                    0,
                    observed_at,
                    "started",
                    43,
                    "rollback.py",
                    128,
                    None,
                ),
            )
            legacy_rows = connection.execute(
                """
                SELECT host, gpu_id, observed_at, event_type, pid, name
                FROM process_events ORDER BY observed_at, event_type, pid, name
                """
            ).fetchall()
        self.assertEqual(len(legacy_rows), 3)

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        restored = reopened.load(10, 10).process_events[("gpu-01", "GPU-1")]
        self.assertEqual([event["pid"] for event in restored], [42, 42, 43])

    def test_released_v3_database_at_its_size_cap_needs_no_upgrade_headroom(
        self,
    ) -> None:
        config = PersistenceConfig(enabled=True, retention_hours=24, max_bytes=131_072)
        create_legacy_database(self.path, 3)
        with closing(sqlite3.connect(self.path)) as connection:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            max_pages = config.max_bytes // page_size
            connection.execute(f"PRAGMA max_page_count = {max_pages}")
            index = 0
            while True:
                observed_at = utc_text(
                    datetime.now(timezone.utc) + timedelta(seconds=index)
                )
                try:
                    connection.execute(
                        "INSERT INTO process_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "gpu-01",
                            "GPU-1",
                            0,
                            observed_at,
                            "started",
                            index,
                            f"process-{index}",
                            1,
                            json.dumps({"padding": "x" * 1024, "index": index}),
                        ),
                    )
                    connection.commit()
                    index += 1
                except sqlite3.OperationalError as exc:
                    connection.rollback()
                    self.assertIn("full", str(exc).lower())
                    break
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        self.assertGreaterEqual(page_count, max_pages - 1)

        store = SqliteTelemetryPersistence(config, self.path)
        store.close()
        reopened = SqliteTelemetryPersistence(config, self.path)
        reopened.close()
        self.assertLessEqual(self.path.stat().st_size, config.max_bytes)

    def test_removes_transient_companion_triggers_before_retention_pruning(
        self,
    ) -> None:
        create_legacy_database(self.path, 3)
        stale = "2000-01-01T00:00:00Z"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE process_event_order (host TEXT, observed_at TEXT)"
            )
            connection.execute(
                "CREATE TABLE process_usage_events (host TEXT, observed_at TEXT)"
            )
            connection.execute(
                """
                CREATE TRIGGER process_events_prune_order
                AFTER DELETE ON process_events BEGIN
                    DELETE FROM process_event_order WHERE host = OLD.host;
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER gpu_history_prune_usage_events
                AFTER DELETE ON gpu_history BEGIN
                    DELETE FROM process_usage_events WHERE host = OLD.host;
                END
                """
            )
            connection.execute(
                "INSERT INTO gpu_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", 0, stale, 1, 1, 1, 1, 1),
            )
            connection.execute(
                "INSERT INTO process_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", 0, stale, "started", 1, "p", 1, None),
            )

        store = SqliteTelemetryPersistence(self.config, self.path)
        store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            leftovers = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE name IN (
                    'process_event_order', 'process_usage_events',
                    'process_events_prune_order',
                    'gpu_history_prune_usage_events'
                )
                """
            ).fetchall()
        self.assertEqual(leftovers, [])

    def test_roundtrips_the_transport_retried_flag_across_restart(self) -> None:
        now = datetime.now(timezone.utc)
        first = utc_text(now - timedelta(minutes=10))
        second = utc_text(now - timedelta(minutes=5))
        store = SqliteTelemetryPersistence(self.config, self.path)
        store.record_history("gpu-01", history_point(first, 10))
        store.record_history(
            "gpu-01", history_point(second, 20, transport_retried=True)
        )
        self.assertTrue(store.flush())
        store.close()

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        points = reopened.load(10, 10).history["gpu-01"]

        self.assertEqual([point["transportRetried"] for point in points], [False, True])

    def test_migrates_the_v1_database_without_losing_existing_history(self) -> None:
        create_legacy_database(self.path, 1)
        observed_at = utc_text(datetime.now(timezone.utc) - timedelta(minutes=5))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "gpu-01",
                    observed_at,
                    10.0,
                    20.0,
                    0.0,
                    40.0,
                    100.0,
                    200.0,
                    300.0,
                    400.0,
                    50.0,
                    60.0,
                    70.0,
                ),
            )

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
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(history)")
            }
        self.assertEqual(version, 3)
        self.assertIn("gpu_history", tables)
        self.assertIn("process_events", tables)
        self.assertIn("transport_retried", columns)
        points = store.load(10, 10).history["gpu-01"]
        self.assertEqual(
            [(point["observedAt"], point["transportRetried"]) for point in points],
            [(observed_at, False)],
        )

    def test_migrates_a_v2_database_preserving_existing_history(self) -> None:
        create_legacy_database(self.path, 2)
        now = datetime.now(timezone.utc)
        old_observed_at = utc_text(now - timedelta(minutes=10))
        new_observed_at = utc_text(now - timedelta(minutes=5))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "gpu-01",
                    old_observed_at,
                    10.0,
                    20.0,
                    0.0,
                    40.0,
                    100.0,
                    200.0,
                    300.0,
                    400.0,
                    50.0,
                    60.0,
                    70.0,
                ),
            )

        store = SqliteTelemetryPersistence(self.config, self.path)
        store.record_history(
            "gpu-01", history_point(new_observed_at, 20, transport_retried=True)
        )
        self.assertTrue(store.flush())
        store.close()

        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(history)")
            }
        self.assertEqual(version, 3)
        self.assertIn("transport_retried", columns)

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        points = reopened.load(10, 10).history["gpu-01"]
        self.assertEqual([point["transportRetried"] for point in points], [False, True])

    def test_recovers_from_a_partially_created_schema(self) -> None:
        # As if the initial schema creation crashed after the first table and
        # before user_version was stamped.
        create_legacy_database(self.path, 0, tables=("history",))
        observed_at = utc_text(datetime.now(timezone.utc) - timedelta(minutes=5))

        store = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(store.close)
        store.record_history("gpu-01", history_point(observed_at, 10))
        self.assertTrue(store.flush())

        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(version, 3)
        self.assertLessEqual(
            {"history", "incident_events", "gpu_history", "process_events"}, tables
        )
        self.assertEqual(len(store.load(10, 10).history["gpu-01"]), 1)

    def test_recovers_from_a_partially_migrated_v1_database(self) -> None:
        # As if the v1 migration crashed between the two table creations.
        create_legacy_database(
            self.path, 1, tables=("history", "incident_events", "gpu_history")
        )

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
        self.assertEqual(version, 3)
        self.assertIn("process_events", tables)
        self.assertTrue(store.status()["healthy"])

    def test_enforces_the_size_limit_for_runtime_writes(self) -> None:
        config = PersistenceConfig(enabled=True, retention_hours=24, max_bytes=131_072)
        store = SqliteTelemetryPersistence(config, self.path)
        self.addCleanup(store.close)
        base = datetime.now(timezone.utc) + timedelta(hours=1)

        for index in range(4000):
            observed_at = utc_text(base + timedelta(seconds=index))
            store.record_history("gpu-01", history_point(observed_at, 10.0))
        store.flush(30.0)

        status = store.status()
        self.assertGreater(status["droppedWrites"], 0)
        self.assertLessEqual(self.path.stat().st_size, config.max_bytes)
        store.close()

        reopened = SqliteTelemetryPersistence(config, self.path)
        self.addCleanup(reopened.close)
        retained = reopened.load(10, 10).history.get("gpu-01", ())
        self.assertGreater(len(retained), 0)

    def test_prunes_and_retries_when_the_database_reports_full(self) -> None:
        store = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(store.close)
        stale_observed_at = utc_text(datetime.now(timezone.utc) - timedelta(hours=48))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "gpu-01",
                    stale_observed_at,
                    10.0,
                    20.0,
                    0.0,
                    40.0,
                    100.0,
                    200.0,
                    300.0,
                    400.0,
                    50.0,
                    60.0,
                    70.0,
                    0,
                ),
            )
        recent_observed_at = utc_text(datetime.now(timezone.utc))
        original_write = SqliteTelemetryPersistence._write
        failed_once = []

        def full_once(connection: sqlite3.Connection, item: object) -> int:
            if not failed_once:
                failed_once.append(True)
                raise sqlite3.OperationalError("database or disk is full")
            return original_write(connection, item)

        with mock.patch.object(
            SqliteTelemetryPersistence, "_write", staticmethod(full_once)
        ):
            store.record_history("gpu-01", history_point(recent_observed_at, 20.0))
            self.assertTrue(store.flush())

        loaded = store.load(10, 10)
        self.assertEqual(
            [point["observedAt"] for point in loaded.history["gpu-01"]],
            [recent_observed_at],
        )
        status = store.status()
        self.assertEqual(status["droppedWrites"], 0)
        self.assertTrue(status["healthy"])

    def test_prunes_stale_records_during_idle_periods(self) -> None:
        config = PersistenceConfig(enabled=True, retention_hours=1, max_bytes=8_388_608)
        with mock.patch("mocop.persistence._PRUNE_INTERVAL_SECONDS", 0.2):
            store = SqliteTelemetryPersistence(config, self.path)
            self.addCleanup(store.close)
            nearly_stale = utc_text(
                datetime.now(timezone.utc) - timedelta(hours=1) + timedelta(seconds=1)
            )
            store.record_history("gpu-01", history_point(nearly_stale, 10.0))
            self.assertTrue(store.flush())

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and store.load(10, 10).history:
                time.sleep(0.1)
            self.assertEqual(store.load(10, 10).history, {})

    def test_flush_reports_batches_dropped_by_write_failures(self) -> None:
        store = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(store.close)
        entered = threading.Event()
        release = threading.Event()

        def failing_write(connection: sqlite3.Connection, item: object) -> int:
            entered.set()
            release.wait(30.0)
            raise sqlite3.OperationalError("no such table: injected")

        with mock.patch.object(
            SqliteTelemetryPersistence, "_write", staticmethod(failing_write)
        ):
            store.record_history("gpu-01", history_point("2026-08-10T00:00:00Z", 10.0))
            self.assertTrue(entered.wait(10.0))
            # The writer is now blocked inside the first batch, so the next
            # write and the flush barrier are guaranteed to share a batch.
            store.record_history("gpu-01", history_point("2026-08-10T00:00:05Z", 20.0))
            results: list[bool] = []
            flusher = threading.Thread(target=lambda: results.append(store.flush(15.0)))
            flusher.start()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and store.status()["queuedWrites"] < 2:
                time.sleep(0.01)
            self.assertGreaterEqual(store.status()["queuedWrites"], 2)
            release.set()
            flusher.join(20.0)

        self.assertFalse(flusher.is_alive())
        self.assertEqual(results, [False])
        status = store.status()
        self.assertEqual(status["droppedWrites"], 2)
        self.assertFalse(status["healthy"])

    def test_restores_process_events_for_every_gpu(self) -> None:
        store = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(store.close)
        now = datetime.now(timezone.utc)
        busy = tuple(
            process_event(
                utc_text(now - timedelta(minutes=5 - index)),
                "GPU-busy",
                101 + index,
            )
            for index in range(5)
        )
        quiet = (process_event(utc_text(now - timedelta(minutes=30)), "GPU-quiet", 7),)
        store.record_gpu_telemetry("gpu-01", (), busy + quiet)
        self.assertTrue(store.flush())

        loaded = store.load(history_points=10, incident_points=2)

        self.assertEqual(
            [event["pid"] for event in loaded.process_events[("gpu-01", "GPU-quiet")]],
            [7],
        )
        self.assertEqual(
            [event["pid"] for event in loaded.process_events[("gpu-01", "GPU-busy")]],
            [104, 105],
        )

    def test_new_incident_events_replace_corrupt_id_placeholders(self) -> None:
        now = datetime.now(timezone.utc)
        store = SqliteTelemetryPersistence(self.config, self.path)
        store.record_incidents(
            (
                incident_event(1, utc_text(now - timedelta(minutes=10))),
                incident_event(2, utc_text(now - timedelta(minutes=9))),
            )
        )
        self.assertTrue(store.flush())
        store.close()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE incident_events SET value = 'invalid' WHERE event_id = 2"
            )

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        restored = reopened.load(20, 20)
        self.assertEqual([event.event_id for event in restored.incident_events], [1])

        # A restarted tracker resumes numbering right after the last valid
        # event, so its next event reuses the id held by the corrupt row.
        replacement_observed_at = utc_text(now - timedelta(minutes=1))
        reopened.record_incidents((incident_event(2, replacement_observed_at),))
        self.assertTrue(reopened.flush())
        reopened.close()

        final = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(final.close)
        events = final.load(20, 20).incident_events
        self.assertEqual([event.event_id for event in events], [1, 2])
        self.assertEqual(events[1].observed_at, replacement_observed_at)

    def test_corrupt_rows_do_not_displace_older_valid_records(self) -> None:
        now = datetime.now(timezone.utc)
        old_observed_at = utc_text(now - timedelta(minutes=10))
        new_observed_at = utc_text(now - timedelta(minutes=5))
        store = SqliteTelemetryPersistence(self.config, self.path)
        store.record_history("gpu-01", history_point(old_observed_at, 10.0))
        store.record_history("gpu-01", history_point(new_observed_at, 20.0))
        store.record_incidents(
            (
                incident_event(1, old_observed_at),
                incident_event(2, new_observed_at),
            )
        )
        store.record_gpu_telemetry(
            "gpu-01",
            (gpu_point(old_observed_at, 50.0), gpu_point(new_observed_at, 60.0)),
            (
                process_event(old_observed_at, "GPU-1", 41),
                process_event(new_observed_at, "GPU-1", 42),
            ),
        )
        self.assertTrue(store.flush())
        store.close()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE history SET cpu_usage_pct = 'invalid' WHERE observed_at = ?",
                (new_observed_at,),
            )
            connection.execute(
                "UPDATE incident_events SET value = 'invalid' WHERE event_id = 2"
            )
            connection.execute(
                "UPDATE gpu_history SET utilization_gpu_pct = 'invalid'"
                " WHERE observed_at = ?",
                (new_observed_at,),
            )
            connection.execute(
                "UPDATE process_events SET gpu_index = 'invalid' WHERE observed_at = ?",
                (new_observed_at,),
            )

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        loaded = reopened.load(history_points=1, incident_points=1)

        self.assertEqual(
            [point["observedAt"] for point in loaded.history["gpu-01"]],
            [old_observed_at],
        )
        self.assertEqual([event.event_id for event in loaded.incident_events], [1])
        self.assertEqual(
            [point["observedAt"] for point in loaded.gpu_history[("gpu-01", "GPU-1")]],
            [old_observed_at],
        )
        self.assertEqual(
            [event["pid"] for event in loaded.process_events[("gpu-01", "GPU-1")]],
            [41],
        )

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
        state.apply(
            ProbeResult(
                "gpu-01", "online", 1, (gpu,), system=system, transport_retries=1
            )
        )
        state.apply(ProbeResult("gpu-01", "unreachable", 1))
        self.assertTrue(persistence.flush())
        persistence.close()

        reopened = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(reopened.close)
        restored = reopened.load(20, 20)
        restarted_state = StateStore(5, persistence=reopened, restored=restored)
        restarted_state.set_hosts(("gpu-01",))

        restored_points = restarted_state.history("gpu-01", 20)["points"]
        self.assertEqual(len(restored_points), 1)
        self.assertTrue(restored_points[0]["transportRetried"])
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
