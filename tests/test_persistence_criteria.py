from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mocop.config import PersistenceConfig
from mocop.persistence import SqliteTelemetryPersistence
from mocop.persistence_criteria import LATEST_CRITERION_SQL
from mocop.persistence_incident_state import write_criteria
from mocop.persistence_records import _IncidentStateWrite
from mocop.persistence_schema import COMPANION_STATEMENTS, CREATE_SCHEMA_STATEMENTS

from .test_persistence import create_legacy_database, incident_event, utc_text


class PersistenceCriteriaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.sqlite3"
        self.config = PersistenceConfig(enabled=True)
        self.trace = []
        connect = SqliteTelemetryPersistence._connect

        def traced_connect(backend):
            connection = connect(backend)
            connection.set_trace_callback(self.trace.append)
            return connection

        patch = mock.patch.object(
            SqliteTelemetryPersistence, "_connect", traced_connect
        )
        patch.start()
        self.addCleanup(patch.stop)
        event = incident_event(1, utc_text(datetime.now(timezone.utc)))
        self.event = replace(event, condition=replace(event.condition, critical_at=95))
        self.host = self.event.host
        self.key = self.event.condition.key

    def backend(self):
        backend = SqliteTelemetryPersistence(self.config, self.path)
        self.addCleanup(backend.close)
        return backend

    def state(self, backend, critical, events=()):
        backend.record_incident_state(self.host, events, ((self.key, critical),))

    def criterion_writes(self):
        return [
            sql
            for sql in self.trace
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            and "incident_criteria" in sql
        ]

    def test_persistence_restore_critical_unchanged_criterion_avoids_sql(self):
        backend = self.backend()
        self.state(backend, 95, (self.event,))
        self.assertTrue(backend.flush())
        self.trace.clear()
        for _ in range(5):
            self.state(backend, 95)
        self.assertTrue(backend.flush())
        self.assertEqual(self.trace, [])

    def test_persistence_restore_critical_changed_criterion_writes_once(self):
        backend = self.backend()
        self.state(backend, 95, (self.event,))
        self.assertTrue(backend.flush())
        for critical in (0, None, 95):
            with self.subTest(critical=critical):
                self.trace.clear()
                for _ in range(5):
                    self.state(backend, critical)
                self.assertTrue(backend.flush())
                self.assertEqual(len(self.criterion_writes()), 1)
                with closing(sqlite3.connect(self.path)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT critical_at FROM incident_criteria WHERE event_id=1"
                        ).fetchone(),
                        (critical,),
                    )

    def test_persistence_restore_critical_criterion_lookup_uses_index(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            for ddl in CREATE_SCHEMA_STATEMENTS + COMPANION_STATEMENTS:
                connection.execute(ddl)
            queries = []
            connection.set_trace_callback(queries.append)
            write_criteria(connection, self.host, ((self.key, 0),))
            sql = next(sql for sql in queries if "INSERT INTO incident_criteria" in sql)
            plan = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
            self.assertTrue(
                any("incident_events_condition" in row[3] for row in plan), plan
            )
            self.assertFalse(
                any("SCAN incident_events" in row[3] for row in plan), plan
            )
            plan = connection.execute(
                "EXPLAIN QUERY PLAN " + LATEST_CRITERION_SQL, (self.host, self.key)
            ).fetchall()
            self.assertTrue(
                any("incident_events_condition" in row[3] for row in plan), plan
            )
            self.assertFalse(any("SCAN e" in row[3] for row in plan), plan)

    def test_persistence_restore_critical_legacy_migration_adds_lookup_index(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.path.unlink(missing_ok=True)
                create_legacy_database(self.path, version)
                backend = self.backend()
                self.assertTrue(backend.flush())
                backend.close()
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

    def test_persistence_restore_critical_criterion_cache_reloads_after_restart(self):
        backend = self.backend()
        self.state(backend, 95, (self.event,))
        self.assertTrue(backend.flush())
        backend.close()
        backend = self.backend()
        self.trace.clear()
        self.state(backend, 95)
        self.assertTrue(backend.flush())
        self.assertEqual(self.criterion_writes(), [])
        self.trace.clear()
        self.state(backend, 95)
        self.assertTrue(backend.flush())
        self.assertEqual(self.trace, [])
        self.state(backend, 0)
        self.assertTrue(backend.flush())
        self.assertEqual(len(self.criterion_writes()), 1)

    def test_persistence_restore_critical_criterion_cache_tracks_transitions(self):
        backend = self.backend()
        backend.record_incidents((self.event,))
        self.assertTrue(backend.flush())
        self.trace.clear()
        self.state(backend, 95)
        self.assertTrue(backend.flush())
        self.assertEqual(self.trace, [])
        resolved = replace(self.event, event_id=2, state="resolved")
        backend.record_incidents((resolved,))
        self.assertTrue(backend.flush())
        # A closed condition is never treated as an open cache entry.
        self.state(backend, 0)
        self.assertTrue(backend.flush())
        opened = replace(self.event, event_id=3)
        self.state(backend, 0, (opened,))
        self.assertTrue(backend.flush())
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 0
        )
        self.trace.clear()
        self.state(backend, 0)
        self.assertTrue(backend.flush())
        self.assertEqual(self.trace, [])

    def test_persistence_restore_critical_criterion_cache_survives_batch_rollback(self):
        backend = self.backend()
        self.state(backend, 95, (self.event,))
        self.assertTrue(backend.flush())
        original = backend._write

        def fail_after_sql(connection, item, busy):
            records = original(connection, item, busy)
            if isinstance(item, _IncidentStateWrite):
                raise sqlite3.OperationalError("injected failure after criterion SQL")
            return records

        with mock.patch.object(backend, "_write", fail_after_sql):
            self.state(backend, 0)
            self.assertFalse(backend.flush())
        self.trace.clear()
        self.state(backend, 95)
        self.assertTrue(backend.flush())
        self.assertEqual(self.criterion_writes(), [])
        self.trace.clear()
        self.state(backend, 0)
        self.assertTrue(backend.flush())
        self.assertEqual(len(self.criterion_writes()), 1)
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 0
        )

    def test_persistence_restore_critical_current_database_adds_lookup_index(self):
        backend = self.backend()
        self.state(backend, 95, (self.event,))
        self.assertTrue(backend.flush())
        backend.close()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP INDEX incident_events_condition")
        backend = self.backend()
        self.assertTrue(backend.flush())
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
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 95
        )

    def test_persistence_restore_critical_criterion_cache_isolates_conditions(self):
        backend = self.backend()
        events = (
            self.event,
            replace(self.event, event_id=2, host="gpu-02"),
            replace(
                self.event,
                event_id=3,
                condition=replace(self.event.condition, key="cpu"),
            ),
        )
        backend.record_incidents(events)
        self.assertTrue(backend.flush())
        self.trace.clear()
        self.state(backend, 0)
        backend.record_incident_state("gpu-02", (), ((self.key, 95),))
        backend.record_incident_state(self.host, (), (("cpu", 95),))
        self.assertTrue(backend.flush())
        self.assertEqual(len(self.criterion_writes()), 1)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT event_id, critical_at FROM incident_criteria ORDER BY event_id"
                ).fetchall(),
                [(1, 0), (2, 95), (3, 95)],
            )

    def test_persistence_restore_critical_criterion_cache_retries_after_full(self):
        backend = self.backend()
        self.state(backend, 95, (self.event,))
        self.assertTrue(backend.flush())
        original = backend._write
        failed = False

        def full_once(connection, item, busy):
            nonlocal failed
            records = original(connection, item, busy)
            if isinstance(item, _IncidentStateWrite) and not failed:
                failed = True
                raise sqlite3.OperationalError("database or disk is full")
            return records

        with mock.patch.object(backend, "_write", full_once):
            self.state(backend, 0)
            self.assertTrue(backend.flush())
        self.assertTrue(failed)
        self.assertEqual(backend.status()["droppedWrites"], 0)
        self.assertEqual(
            backend.load(20, 20).open_incidents[0].condition.critical_at, 0
        )
        self.trace.clear()
        self.state(backend, 0)
        self.assertTrue(backend.flush())
        self.assertEqual(self.trace, [])

    def test_persistence_restore_critical_criterion_cache_waits_for_commit(self):
        fail_commit = False

        class CommitFailureConnection(sqlite3.Connection):
            def __exit__(connection, kind, value, traceback):
                nonlocal fail_commit
                if fail_commit and kind is None and connection.in_transaction:
                    fail_commit = False
                    connection.rollback()
                    raise sqlite3.OperationalError("injected commit failure")
                return super().__exit__(kind, value, traceback)

        connect = sqlite3.connect

        def failing_connection(*args, **kwargs):
            return connect(*args, factory=CommitFailureConnection, **kwargs)

        with mock.patch("mocop.persistence.sqlite3.connect", failing_connection):
            backend = self.backend()
            self.state(backend, 95, (self.event,))
            self.assertTrue(backend.flush())
            fail_commit = True
            self.state(backend, 0)
            self.assertFalse(backend.flush())
            self.assertFalse(fail_commit)
            self.trace.clear()
            self.state(backend, 95)
            self.assertTrue(backend.flush())
            self.assertEqual(self.criterion_writes(), [])
            self.state(backend, 0)
            self.assertTrue(backend.flush())
            self.assertEqual(len(self.criterion_writes()), 1)
            self.assertEqual(
                backend.load(20, 20).open_incidents[0].condition.critical_at, 0
            )
            backend.close()

    def test_persistence_restore_critical_lookup_index_migration_is_atomic(self):
        create_legacy_database(self.path, 3)

        class InterruptedMigration(sqlite3.Connection):
            def execute(connection, sql, parameters=()):
                if sql.startswith("CREATE TABLE IF NOT EXISTS incident_criteria"):
                    raise sqlite3.OperationalError("injected migration failure")
                return super().execute(sql, parameters)

        with closing(sqlite3.connect(self.path, factory=InterruptedMigration)) as conn:
            with self.assertRaisesRegex(sqlite3.OperationalError, "migration failure"):
                SqliteTelemetryPersistence._migrate_companions(conn)
            self.assertEqual(
                conn.execute("PRAGMA index_info(incident_events_condition)").fetchall(),
                [],
            )
        backend = self.backend()
        self.assertTrue(backend.flush())
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                [
                    row[2]
                    for row in conn.execute(
                        "PRAGMA index_info(incident_events_condition)"
                    )
                ],
                ["host", "condition_key"],
            )
