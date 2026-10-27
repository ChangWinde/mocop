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

from mocop.config import PersistenceConfig
from mocop.persistence import SqliteTelemetryPersistence
from mocop.persistence_gpu import write_gpu_points
from mocop.persistence_retention import bootstrap_gpu_retention
from mocop.persistence_rollups import ROLLUP_STATEMENTS
from mocop.persistence_schema import COMPANION_STATEMENTS, CREATE_SCHEMA_STATEMENTS

from .test_persistence import create_legacy_database, gpu_point, utc_text


class PersistenceIndexUpgradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.sqlite3"
        self.base = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        self.config = PersistenceConfig(enabled=True, retention_hours=1)
        self.recent = dict(
            gpu_point(utc_text(self.base + timedelta(minutes=58)), 25),
            memoryTotalMiB=4096,
        )

    def backend(self, moment):
        with mock.patch("mocop.persistence.datetime") as clock:
            clock.now.return_value = moment
            backend = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(backend.close)
        return backend

    def seed_gpu_history(self, *, full=False, partial=False):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            with connection:
                for ddl in (
                    CREATE_SCHEMA_STATEMENTS + COMPANION_STATEMENTS + ROLLUP_STATEMENTS
                ):
                    if "incident_events_condition" not in ddl:
                        connection.execute(ddl)
                connection.execute("PRAGMA user_version = 3")
                old = tuple(
                    gpu_point(utc_text(self.base + timedelta(seconds=index)), 50)
                    for index in range(100)
                )
                write_gpu_points(connection, "gpu-01", old + (self.recent,), 10)
                if partial:
                    connection.execute("DROP TABLE incident_criteria")
                    connection.execute("DROP TABLE gpu_rollup_busy")
            if not full:
                return
            connection.execute("PRAGMA max_page_count = 256")
            for index in range(2000):
                try:
                    with connection:
                        connection.execute(
                            "INSERT INTO process_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                "gpu-01",
                                "GPU-1",
                                0,
                                self.recent["observedAt"],
                                "started",
                                index,
                                f"process-{index}",
                                1,
                                json.dumps({"padding": "x" * 1024}),
                            ),
                        )
                except sqlite3.OperationalError as exc:
                    self.assertIn("full", str(exc).lower())
                    break
            else:
                self.fail("fixture did not reach the SQLite size cap")
            live = (
                connection.execute("PRAGMA page_count").fetchone()[0]
                - connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            size = connection.execute("PRAGMA page_size").fetchone()[0]
            self.config = replace(self.config, max_bytes=live * size)

    def archived(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(
                "SELECT host, gpu_id, hour, memory_total_max FROM gpu_retired_memory"
            ).fetchall()

    def expected_archive(self):
        return [("gpu-01", "GPU-1", utc_text(self.base), 8192)]

    def test_persistence_rollup_replace_index_full_retention_restart(self):
        self.seed_gpu_history(full=True)
        later = self.base + timedelta(hours=1, minutes=3)
        with self.assertLogs("mocop.persistence", level="WARNING") as logs:
            backend = self.backend(self.base + timedelta(minutes=58))
            enabled = backend._companions_enabled
            initial_status = backend.status()
            backend.close()
            with closing(sqlite3.connect(self.path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "PRAGMA index_info(incident_events_condition)"
                    ).fetchall(),
                    [],
                )
            with mock.patch("mocop.persistence.datetime") as clock:
                clock.now.return_value = later
                with closing(backend._connect()) as connection, connection:
                    backend._prune(connection, reclaim_pages=0)
            self.assertEqual(self.archived(), self.expected_archive())
            self.assertTrue(enabled)
            self.assertTrue(initial_status["healthy"], initial_status)
            self.assertIsNone(initial_status["lastError"])
            self.assertTrue(backend._rollups_enabled)
            resumed = self.backend(later)
            with closing(sqlite3.connect(self.path)) as connection:
                self.assertEqual(
                    [
                        row[2]
                        for row in connection.execute(
                            "PRAGMA index_info(incident_events_condition)"
                        )
                    ],
                    ["host", "condition_key"],
                )
            with mock.patch("mocop.persistence.datetime") as clock:
                clock.now.return_value = later
                resumed.record_gpu_telemetry(
                    "gpu-01", (dict(self.recent, memoryTotalMiB=2048),), ()
                )
                self.assertTrue(resumed.flush())
            self.assertEqual(resumed.status()["droppedWrites"], 0)
            with closing(sqlite3.connect(self.path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT samples, memory_total_max FROM gpu_hourly"
                    ).fetchall(),
                    [(101, 8192)],
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone(), ("ok",)
                )
            resumed.close()
            self.assertLessEqual(self.path.stat().st_size, self.config.max_bytes)
        self.assertTrue(any("full" in record.getMessage() for record in logs.records))
        self.assertTrue(
            any("next startup" in record.getMessage() for record in logs.records)
        )

    def test_persistence_rollup_replace_index_failure_keeps_migrated_tables(self):
        connect = sqlite3.connect
        moment = self.base + timedelta(minutes=58)
        for message in ("database or disk is full", "injected index I/O failure"):
            with self.subTest(message=message):
                self.path.unlink(missing_ok=True)
                create_legacy_database(self.path, 3)

                class FailedIndex(sqlite3.Connection):
                    def execute(connection, sql, parameters=(), message=message):
                        if sql.startswith(
                            "CREATE INDEX IF NOT EXISTS incident_events_condition"
                        ):
                            raise sqlite3.OperationalError(message)
                        return super().execute(sql, parameters)

                def fail_index(*args, **kwargs):
                    return connect(*args, factory=FailedIndex, **kwargs)

                with (
                    mock.patch("mocop.persistence.sqlite3.connect", fail_index),
                    self.assertLogs("mocop.persistence", level="WARNING") as logs,
                ):
                    backend = self.backend(moment)
                    self.assertTrue(backend._companions_enabled)
                    backend.record_gpu_telemetry("gpu-01", (self.recent,), ())
                    self.assertTrue(backend.flush())
                    self.assertTrue(backend.status()["healthy"])
                    self.assertEqual(backend.status()["droppedWrites"], 0)
                    backend.close()
                self.assertEqual(len(logs.records), 1)
                self.assertIn(message, logs.records[0].getMessage())
                resumed = self.backend(moment)
                self.assertTrue(resumed._companions_enabled)
                self.assertEqual(
                    resumed.load(20, 20).gpu_history[("gpu-01", "GPU-1")],
                    (self.recent,),
                )
                resumed.close()

    def test_persistence_rollup_replace_partial_metadata_archives_before_expiry(self):
        self.seed_gpu_history(partial=True)
        backend = self.backend(self.base + timedelta(hours=1, minutes=3))
        self.assertEqual(self.archived(), self.expected_archive())
        self.assertEqual(
            backend.load(20, 20).gpu_history[("gpu-01", "GPU-1")], (self.recent,)
        )

    def test_persistence_rollup_replace_partial_archive_failure_retains_raw(self):
        self.seed_gpu_history(partial=True)

        class FailedArchive(sqlite3.Connection):
            def executemany(connection, sql, parameters):
                if "INSERT INTO gpu_retired_memory" in sql:
                    raise sqlite3.OperationalError("injected archive failure")
                return super().executemany(sql, parameters)

        with closing(sqlite3.connect(self.path, factory=FailedArchive)) as connection:
            with self.assertRaisesRegex(sqlite3.OperationalError, "archive failure"):
                bootstrap_gpu_retention(
                    connection,
                    utc_text(self.base + timedelta(minutes=3)),
                    self.config.max_bytes,
                )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM gpu_history").fetchone(),
                (101,),
            )
            self.assertEqual(
                connection.execute("SELECT * FROM gpu_retention").fetchall(), []
            )
        self.assertEqual(self.archived(), [])
