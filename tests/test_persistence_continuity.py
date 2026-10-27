from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mocop.config import IncidentConfig, PersistenceConfig, ThresholdConfig
from mocop.incidents import IncidentTracker, ThresholdIncidentPolicy
from mocop.models import ProbeResult
from mocop.persistence import PersistenceError, SqliteTelemetryPersistence
from mocop.persistence_records import _IncidentStateWrite
from mocop.persistence_retention import bootstrap_gpu_retention
from mocop.persistence_rollups import ROLLUP_STATEMENTS
from mocop.service import StateStore

from .test_incidents import system
from .test_persistence import create_legacy_database, gpu_point, utc_text


class PersistenceContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.sqlite3"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.config = PersistenceConfig(enabled=True, retention_hours=1)
        self.incidents = IncidentConfig(
            resource_open_cycles=1, resource_open_seconds=0, recovery_cycles=1
        )

    def backend(self):
        backend = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(backend.close)
        return backend

    def sample(self, moment, cpu=10, disk=10, available=80000):
        metrics = system(cpu, disk)
        filesystem = replace(
            metrics.disks[0],
            total_mib=1000000,
            used_mib=disk * 10000,
            available_mib=available,
        )
        return ProbeResult(
            "gpu-01",
            "online",
            1,
            observed_at=utc_text(moment),
            system=replace(metrics, disks=(filesystem,)),
        )

    def test_persistence_restore_critical_criterion_only_change(self):
        backend = self.backend()
        live = StateStore(20, incidents=self.incidents, persistence=backend)
        live.set_hosts(("gpu-01",))
        live.apply(self.sample(self.now, disk=97, available=1024))
        self.assertTrue(backend.flush())
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 0
        )
        live.apply(
            self.sample(self.now + timedelta(seconds=1), disk=97, available=30000)
        )
        self.assertTrue(backend.flush())
        self.assertEqual(len(live.incidents(20)["events"]), 1)
        backend.close()
        backend = self.backend()
        loaded = backend.load(20, 20)
        self.assertEqual(loaded.open_incidents[0].condition.critical_at, 95)
        resumed = StateStore(
            20, incidents=self.incidents, persistence=backend, restored=loaded
        )
        resumed.set_hosts(("gpu-01",))
        for second, value in ((2, 92), (3, 90), (4, 89)):
            sample = self.sample(self.now + timedelta(seconds=second), disk=value)
            live.apply(sample)
            resumed.apply(sample)
            self.assertEqual(
                resumed.incidents(20)["active"], live.incidents(20)["active"]
            )
        self.assertEqual(resumed.incidents(20)["active"][0]["severity"], "warning")

    def test_persistence_rollup_replace_legacy_migration_clock_rollback(self):
        stamp = utc_text(self.now - timedelta(hours=2))
        create_legacy_database(self.path, 3)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for ddl in ROLLUP_STATEMENTS:
                conn.execute(ddl)
            conn.execute(
                "INSERT INTO gpu_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", 0, stamp, 50, 1024, 8192, 60, 120),
            )
            conn.execute(
                "INSERT INTO gpu_hourly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", stamp[:13] + ":00:00Z", 1, 1, 1, 50, 1, 1024, 8192),
            )
        with mock.patch("mocop.persistence.datetime") as clock:
            clock.now.return_value = self.now
            backend = self.backend()
        self.assertEqual(backend.load(20, 20).gpu_history, {})
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT cutoff FROM gpu_retention").fetchall(),
                [(utc_text(self.now - timedelta(hours=1)),)],
            )
        backend.close()
        with mock.patch("mocop.persistence.datetime") as clock:
            clock.now.return_value = self.now - timedelta(hours=2)
            backend = self.backend()
            backend.record_gpu_telemetry("gpu-01", (gpu_point(stamp, 90),), ())
            self.assertFalse(backend.flush())
        self.assertEqual(backend.status()["droppedWrites"], 1)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT samples, utilization_sum FROM gpu_hourly"
                ).fetchall(),
                [(1, 50)],
            )
        self.assertEqual(backend.load(20, 20).gpu_history, {})

    def test_persistence_retention_open_resolved_sequence_survives_clock_rollback(self):
        tracker = IncidentTracker(
            ThresholdIncidentPolicy(ThresholdConfig(), incidents=self.incidents), 20
        )
        backend = self.backend()
        for value, moment in (
            (90, self.now - timedelta(hours=3)),
            (99, self.now),
            (20, self.now - timedelta(hours=2)),
        ):
            backend.record_incidents(tracker.update(self.sample(moment, cpu=value)))
        self.assertEqual(tracker.snapshot(20)["active"], [])
        self.assertTrue(backend.flush())
        with closing(backend._connect()) as conn, conn:
            backend._prune(conn, reclaim_pages=0)
        backend.close()
        backend = self.backend()
        loaded = backend.load(20, 20)
        self.assertEqual(loaded.open_incidents, ())
        self.assertEqual(
            [(event.event_id, event.state) for event in loaded.incident_events],
            [(2, "escalated"), (3, "resolved")],
        )
        resumed = IncidentTracker(
            tracker._policy, 20, loaded.incident_events, loaded.open_incidents
        )
        events = resumed.update(self.sample(self.now + timedelta(seconds=1), cpu=90))
        self.assertEqual(
            [(event.event_id, event.state) for event in events], [(4, "opened")]
        )

    def test_persistence_restore_critical_failed_update_retries(self):
        backend = self.backend()
        live = StateStore(20, incidents=self.incidents, persistence=backend)
        live.set_hosts(("gpu-01",))
        live.apply(self.sample(self.now, disk=97, available=1024))
        self.assertTrue(backend.flush())
        original = backend._write

        def lose_update(connection, item, busy):
            records = original(connection, item, busy)
            if isinstance(item, _IncidentStateWrite):
                raise sqlite3.OperationalError("injected criterion write failure")
            return records

        with mock.patch.object(backend, "_write", lose_update):
            live.apply(
                self.sample(self.now + timedelta(seconds=1), disk=97, available=30000)
            )
            self.assertFalse(backend.flush())
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 0
        )
        live.apply(
            self.sample(self.now + timedelta(seconds=2), disk=97, available=30000)
        )
        self.assertTrue(backend.flush())
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 95
        )
        self.assertGreater(backend.status()["droppedWrites"], 0)

    def test_persistence_restore_critical_snapshot_uses_confirmed_criterion(self):
        incidents = replace(self.incidents, resource_open_cycles=2)
        backend = self.backend()
        live = StateStore(20, incidents=incidents, persistence=backend)
        live.set_hosts(("gpu-01",))
        for second in (0, 1):
            live.apply(
                self.sample(
                    self.now + timedelta(seconds=second), disk=97, available=1024
                )
            )
        self.assertTrue(backend.flush())
        # One warning reading starts a pending severity change; the confirmed
        # critical criterion is still zero until the tracker changes it.
        live.apply(self.sample(self.now + timedelta(seconds=2), disk=92))
        self.assertTrue(backend.flush())
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 0
        )
        # A critical reading cancels that pending change and establishes 95.
        live.apply(self.sample(self.now + timedelta(seconds=3), disk=97))
        self.assertTrue(backend.flush())
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 95
        )
        for second, available, critical in ((4, 1024, 0), (5, 80000, 95)):
            live.apply(
                self.sample(
                    self.now + timedelta(seconds=second), disk=97, available=available
                )
            )
            self.assertTrue(backend.flush())
            self.assertEqual(
                backend.load(20, 20).open_incidents[0].condition.critical_at, critical
            )
        self.assertEqual(len(live.incidents(20)["events"]), 1)

    def test_persistence_rollup_replace_legacy_cutoff_rolls_back_with_expiry(self):
        create_legacy_database(self.path, 3)
        stamp = utc_text(self.now - timedelta(hours=2))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO gpu_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", 0, stamp, 50, 1024, 8192, 60, 120),
            )

        class InterruptedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql.startswith("DELETE FROM gpu_history"):
                    raise sqlite3.OperationalError("injected expiry interruption")
                return result

        original = sqlite3.connect
        with (
            mock.patch(
                "mocop.persistence.sqlite3.connect",
                lambda *args, **kwargs: original(
                    *args, **kwargs, factory=InterruptedConnection
                ),
            ),
            self.assertRaisesRegex(PersistenceError, "initialize SQLite"),
        ):
            self.backend()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM gpu_history").fetchone()[0], 1
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'gpu_retention'"
                ).fetchone()
            )
        backend = self.backend()
        self.assertEqual(backend.load(20, 20).gpu_history, {})
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM gpu_retention").fetchone()[0], 1
            )

    def test_persistence_rollup_replace_legacy_deferred_bootstrap_initializes_cutoff(
        self,
    ):
        self.config = replace(self.config, max_bytes=262144)
        create_legacy_database(self.path, 3)
        stamp = utc_text(self.now - timedelta(hours=2))
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA max_page_count = 64")
            conn.execute(
                "INSERT INTO gpu_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", 0, stamp, 50, 1024, 8192, 60, 120),
            )
            conn.commit()
            for index in range(1000):
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO process_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                "gpu-01",
                                "GPU-1",
                                0,
                                stamp,
                                "started",
                                index,
                                f"process-{index}",
                                1,
                                json.dumps({"padding": "x" * 1024}),
                            ),
                        )
                except sqlite3.OperationalError:
                    break
            else:
                self.fail("fixture did not fill the database")
            # Remove any unused budget left by the failed insert, so the
            # bootstrap root cannot fit regardless of SQLite's page layout.
            live = (
                conn.execute("PRAGMA page_count").fetchone()[0]
                - conn.execute("PRAGMA freelist_count").fetchone()[0]
            )
            size = conn.execute("PRAGMA page_size").fetchone()[0]
            self.config = replace(self.config, max_bytes=live * size)
        initialized = []

        def bootstrap(connection, cutoff, max_bytes):
            bootstrap_gpu_retention(connection, cutoff, max_bytes)
            initialized.append(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'gpu_retention'"
                ).fetchone()
                is not None
            )

        with (
            mock.patch("mocop.persistence.bootstrap_gpu_retention", bootstrap),
            mock.patch("mocop.persistence.datetime") as clock,
        ):
            clock.now.return_value = self.now
            backend = self.backend()
        self.assertEqual(initialized, [False])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT cutoff FROM gpu_retention").fetchall(),
                [(utc_text(self.now - timedelta(hours=1)),)],
            )
        backend.close()
        with mock.patch("mocop.persistence.datetime") as clock:
            clock.now.return_value = self.now - timedelta(hours=2)
            backend = self.backend()
            backend.record_gpu_telemetry("gpu-01", (gpu_point(stamp, 90),), ())
            self.assertFalse(backend.flush())
        self.assertEqual(backend.status()["droppedWrites"], 1)
        self.assertEqual(backend.load(20, 20).gpu_history, {})
        self.assertLessEqual(self.path.stat().st_size, self.config.max_bytes)
