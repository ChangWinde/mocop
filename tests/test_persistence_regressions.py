from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mocop.config import IncidentConfig, PersistenceConfig, ThresholdConfig
from mocop.incidents import IncidentTracker, ThresholdIncidentPolicy
from mocop.models import ProbeResult
from mocop.persistence import SqliteTelemetryPersistence, _Flush
from mocop.persistence_gpu import write_gpu_points
from mocop.persistence_rollups import ROLLUP_STATEMENTS

from .test_incidents import gpu, system
from .test_persistence import (
    create_legacy_database,
    gpu_point,
    history_point,
    incident_event,
    utc_text,
)


class PersistenceRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.sqlite3"
        self.config = PersistenceConfig(enabled=True, retention_hours=1)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.policy = ThresholdIncidentPolicy(
            ThresholdConfig(disk_min_free_gib=0),
            incidents=IncidentConfig(resource_open_seconds=0, recovery_margin=5),
        )

    def store(self, **kwargs) -> SqliteTelemetryPersistence:
        store = SqliteTelemetryPersistence(self.config, self.path, **kwargs)
        self.addCleanup(store.close)
        return store

    def sample(self, seconds: int, value: float, category: str = "cpu") -> ProbeResult:
        return ProbeResult(
            "gpu-01",
            "online",
            1,
            (gpu(value),) if category == "gpu_temperature" else (),
            system=system(
                value if category == "cpu" else 10, value if category == "disk" else 10
            ),
            observed_at=utc_text(self.now + timedelta(seconds=seconds)),
        )

    def assert_restart_hysteresis(self, category: str, *, legacy: bool = False) -> None:
        uninterrupted = IncidentTracker(self.policy, 20)
        critical = 85 if category == "gpu_temperature" else 95
        events = ()
        for seconds in (0, 1):
            events += uninterrupted.update(self.sample(seconds, critical + 2, category))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].condition.critical_at, critical)
        if legacy:
            create_legacy_database(self.path, 3)
            event = events[0]
            c = event.condition
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute(
                    "INSERT INTO incident_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.host,
                        c.key,
                        c.category,
                        c.resource,
                        c.severity,
                        c.value,
                        c.threshold,
                        c.observed_at,
                        c.detail,
                        c.group_key,
                        event.state,
                        event.observed_at,
                    ),
                )
        else:
            store = self.store()
            store.record_incidents(events)
            self.assertTrue(store.flush())
            store.close()
        restarted_store = self.store()
        loaded = restarted_store.load(20, 20)
        self.assertEqual(loaded.open_incidents[0].condition.critical_at, critical)
        restarted = IncidentTracker(
            self.policy, 20, loaded.incident_events, loaded.open_incidents
        )
        for seconds, value in enumerate(
            (critical - 1, critical - 5, critical - 2, critical - 6, critical - 6), 2
        ):
            sample = self.sample(seconds, value, category)
            expected = uninterrupted.update(sample)
            actual = restarted.update(sample)
            self.assertEqual(actual, expected)
            self.assertEqual(restarted.snapshot(20), uninterrupted.snapshot(20))
            active = restarted.snapshot(20)["active"]
            self.assertEqual(
                active[0]["severity"], "warning" if seconds == 6 else "critical"
            )
        self.assertEqual(actual[0].state, "deescalated")

    def test_persistence_restore_critical_matches_uninterrupted_cpu(self) -> None:
        self.assert_restart_hysteresis("cpu")

    def test_persistence_restore_critical_matches_uninterrupted_temperature(
        self,
    ) -> None:
        self.assert_restart_hysteresis("gpu_temperature")

    def test_persistence_restore_critical_legacy_disk_margin(self) -> None:
        self.assert_restart_hysteresis("disk", legacy=True)

    def test_persistence_restore_critical_zero_and_recovery_counter_reset(self) -> None:
        # Absolute disk headroom uses zero as its critical line, not a time.
        policy = ThresholdIncidentPolicy(
            ThresholdConfig(), incidents=IncidentConfig(resource_open_seconds=0)
        )
        tracker = IncidentTracker(policy, 20)
        events = tracker.update(self.sample(0, 99, "disk"))
        events += tracker.update(self.sample(1, 99, "disk"))
        self.assertEqual(events[0].condition.critical_at, 0)
        store = self.store()
        store.record_incidents(events)
        self.assertTrue(store.flush())
        # One healthy reading before a crash must not count after the restart.
        healthy = self.sample(2, 10, "disk")
        self.assertEqual(tracker.update(healthy), ())
        store.close()
        loaded = self.store().load(20, 20)
        self.assertEqual(loaded.open_incidents[0].condition.critical_at, 0)
        restarted = IncidentTracker(
            policy, 20, loaded.incident_events, loaded.open_incidents
        )
        self.assertEqual(restarted.update(self.sample(3, 10, "disk")), ())
        self.assertEqual(
            restarted.update(self.sample(4, 10, "disk"))[0].state, "resolved"
        )

    def test_persistence_restore_critical_legacy_condition_thresholds(self) -> None:
        create_legacy_database(self.path, 2)
        categories = (
            ("cpu", 80, 95),
            ("memory", 80, 95),
            ("disk", 85, 95),
            ("gpu_memory", 80, 95),
            ("swap", 20, 90),
            ("pressure", 30, 60),
            ("pressure", 70, 100),
            ("gpu_temperature", 72, 77),
            ("connectivity", None, None),
        )
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for i, (category, threshold, _) in enumerate(categories, 1):
                conn.execute(
                    "INSERT INTO incident_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        i,
                        "gpu-01",
                        str(i),
                        category,
                        "resource",
                        "critical",
                        99,
                        threshold,
                        utc_text(self.now),
                        None,
                        None,
                        "opened",
                        utc_text(self.now),
                    ),
                )
            conn.execute(
                "INSERT INTO incident_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    20,
                    "gpu-01",
                    "warning",
                    "disk",
                    "/",
                    "warning",
                    90,
                    85,
                    utc_text(self.now),
                    None,
                    None,
                    "opened",
                    utc_text(self.now),
                ),
            )
        loaded = self.store().load(20, 20)
        self.assertEqual(
            [e.condition.critical_at for e in loaded.incident_events],
            [item[2] for item in categories] + [None],
        )

    def hourly(self) -> list[tuple]:
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(
                "SELECT samples, utilization_samples, busy_samples, utilization_sum, "
                "memory_samples, memory_used_sum, memory_total_max FROM gpu_hourly ORDER BY hour"
            ).fetchall()

    def prune_at(self, store, moment: datetime) -> None:
        with mock.patch("mocop.persistence.datetime") as clock:
            clock.now.return_value = moment
            with closing(store._connect()) as conn, conn:
                store._prune(conn, reclaim_pages=0)

    def test_persistence_rollup_replace_repeat_and_restart(self) -> None:
        store = self.store(busy_pct=20)
        stamp = utc_text(self.now)
        other = dict(
            gpu_point(utc_text(self.now + timedelta(seconds=1)), 10),
            memoryUsedMiB=None,
            memoryTotalMiB=4096,
        )
        store.record_gpu_telemetry("gpu-01", (gpu_point(stamp, 80), other), ())
        self.assertTrue(store.flush())
        store.close()
        # Busy contribution must use the original threshold when it is removed.
        store = self.store(busy_pct=90)
        replacement = dict(
            gpu_point(stamp, 50), memoryUsedMiB=2048, memoryTotalMiB=2048
        )
        store.record_gpu_telemetry("gpu-01", (replacement,), ())
        self.assertTrue(store.flush())
        self.assertEqual(self.hourly(), [(2, 2, 0, 60, 1, 2048, 4096)])
        points = store.load(20, 20).gpu_history[("gpu-01", "GPU-1")]
        self.assertEqual(points[0], replacement)
        store.record_gpu_telemetry(
            "gpu-01",
            (
                dict(
                    replacement,
                    utilizationGpuPct=None,
                    memoryUsedMiB=None,
                    memoryTotalMiB=None,
                ),
            ),
            (),
        )
        self.assertTrue(store.flush())
        self.assertEqual(self.hourly(), [(2, 1, 0, 10, 0, 0, 4096)])
        self.assertEqual(store.status()["droppedWrites"], 0)

    def test_persistence_rollup_replace_same_queued_write(self) -> None:
        store = self.store()
        stamp = utc_text(self.now)
        store.record_gpu_telemetry(
            "gpu-01", (gpu_point(stamp, 10), gpu_point(stamp, 90)), ()
        )
        self.assertTrue(store.flush())
        self.assertEqual(self.hourly(), [(1, 1, 1, 90, 1, 1024, 8192)])

    def test_persistence_rollup_replace_late_after_expiry(self) -> None:
        store = self.store()
        point = gpu_point(utc_text(self.now - timedelta(minutes=59)), 80)
        store.record_gpu_telemetry("gpu-01", (point,), ())
        self.assertTrue(store.flush())
        expected = self.hourly()
        self.prune_at(store, self.now + timedelta(minutes=2))
        self.assertEqual(store.load(20, 20).gpu_history, {})
        store.record_gpu_telemetry("gpu-01", (dict(point, utilizationGpuPct=10),), ())
        self.assertFalse(store.flush())
        self.assertEqual(self.hourly(), expected)
        self.assertEqual(store.status()["droppedWrites"], 1)
        self.assertEqual(store.load(20, 20).gpu_history, {})

    def test_persistence_rollup_replace_clock_step_back_and_restart(self) -> None:
        store = self.store()
        point = gpu_point(utc_text(self.now), 70)
        store.record_gpu_telemetry("gpu-01", (point,), ())
        self.assertTrue(store.flush())
        self.prune_at(store, self.now + timedelta(hours=2))
        expected = self.hourly()
        store.close()
        store = self.store()
        for p in (point, gpu_point(utc_text(self.now - timedelta(minutes=30)), 20)):
            store.record_gpu_telemetry("gpu-01", (p,), ())
            self.assertFalse(store.flush())
        self.assertEqual(self.hourly(), expected)
        self.assertEqual(store.status()["droppedWrites"], 2)
        self.assertEqual(store.load(20, 20).gpu_history, {})

    def test_persistence_rollup_replace_old_unseen_and_partial_write(self) -> None:
        store = self.store()
        points = (
            gpu_point(utc_text(self.now - timedelta(hours=2)), 80),
            gpu_point(utc_text(self.now), 20),
        )
        store.record_gpu_telemetry("gpu-01", points, ())
        self.assertFalse(store.flush())
        self.assertEqual(store.status()["droppedWrites"], 1)
        self.assertEqual(self.hourly(), [(1, 1, 1, 20, 1, 1024, 8192)])
        self.assertEqual(
            store.load(20, 20).gpu_history[("gpu-01", "GPU-1")], (points[1],)
        )

    def test_persistence_rollup_replace_legacy_aggregates_unchanged(self) -> None:
        create_legacy_database(self.path, 3)
        hour = utc_text(self.now.replace(minute=0, second=0))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for statement in ROLLUP_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO gpu_hourly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", hour, 8, 8, 6, 300, 8, 5000, 8192),
            )
        expected = self.hourly()
        self.store().close()
        self.store().close()
        self.assertEqual(self.hourly(), expected)

    def test_persistence_rollup_replace_keeps_expired_memory_maximum(self) -> None:
        # Both points share an hour even after the first expires.
        now = self.now.replace(minute=58)
        old = now.replace(minute=0)
        store = self.store()
        points = (
            gpu_point(utc_text(old), 80),
            dict(gpu_point(utc_text(now), 20), memoryTotalMiB=4096),
        )
        store.record_gpu_telemetry("gpu-01", points, ())
        self.assertTrue(store.flush())
        self.prune_at(store, old + timedelta(hours=1, minutes=1))
        store.record_gpu_telemetry(
            "gpu-01", (dict(points[1], memoryTotalMiB=2048),), ()
        )
        self.assertTrue(store.flush())
        self.assertEqual(self.hourly()[0][-1], 8192)

    def test_persistence_retention_open_generation_survives_transitions(self) -> None:
        tracker = IncidentTracker(self.policy, 20)
        store = self.store()
        initial = None
        for seconds, value in enumerate((90, 90, 99, 99, 89, 89, 99, 99)):
            sample = replace(
                self.sample(seconds, value),
                observed_at=utc_text(
                    self.now - timedelta(hours=3) + timedelta(seconds=seconds)
                ),
            )
            events = tracker.update(sample)
            store.record_incidents(events)
            if initial is None and events:
                initial = events[0].observed_at
        self.assertTrue(store.flush())
        self.prune_at(store, self.now)
        loaded = store.load(20, 1)
        self.assertEqual(loaded.open_incidents[0].opened_at, initial)
        self.assertEqual(loaded.open_incidents[0].condition.severity, "critical")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT event_id FROM incident_events ORDER BY event_id"
                ).fetchall(),
                [(1,), (4,)],
            )
        store.close()
        loaded = self.store().load(20, 1)
        restarted = IncidentTracker(
            self.policy, 20, loaded.incident_events, loaded.open_incidents
        )
        self.assertEqual(
            restarted.snapshot(20)["active"][0]["firstObservedAt"], initial
        )
        self.assertEqual(restarted.update(self.sample(10, 99)), ())

    def test_persistence_retention_open_latest_generation_only_until_resolved(
        self,
    ) -> None:
        old = utc_text(self.now - timedelta(hours=2))
        store = self.store()
        base = incident_event(1, old)
        store.record_incidents(
            tuple(
                replace(base, event_id=i, state=state)
                for i, state in enumerate(
                    ("opened", "resolved", "opened", "escalated"), 1
                )
            )
        )
        self.assertTrue(store.flush())
        self.prune_at(store, self.now)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT event_id FROM incident_events ORDER BY event_id"
                ).fetchall(),
                [(3,), (4,)],
            )
        store.record_incidents((replace(base, event_id=5, state="resolved"),))
        self.assertTrue(store.flush())
        self.prune_at(store, self.now)
        self.assertEqual(store.load(20, 20).open_incidents, ())
        self.assertEqual(store.load(20, 20).incident_events, ())
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM incident_criteria").fetchone()[0], 0
            )

    def gated_writer(self, *, fail: bool = False):
        entered, release = threading.Event(), threading.Event()
        connect = SqliteTelemetryPersistence._connect

        def gated_connect(instance):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test writer gate timed out")
            if fail:
                raise sqlite3.OperationalError("injected connection failure")
            return connect(instance)

        patch = mock.patch.object(SqliteTelemetryPersistence, "_connect", gated_connect)
        patch.start()
        self.addCleanup(patch.stop)
        store = self.store()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))
        return store, release

    def queue_flush(self, store):
        queued = threading.Event()
        real_put = store._queue.put_nowait

        def put(item):
            real_put(item)
            if isinstance(item, _Flush):
                queued.set()

        patch = mock.patch.object(store._queue, "put_nowait", put)
        patch.start()
        self.addCleanup(patch.stop)
        results = []
        flusher = threading.Thread(target=lambda: results.append(store.flush(5)))
        flusher.start()
        self.addCleanup(flusher.join, 5)
        self.assertTrue(queued.wait(5))
        return flusher, results

    def test_persistence_drops_writer_connection_failure_counts_admitted(self) -> None:
        store, release = self.gated_writer(fail=True)
        store.record_history("gpu-01", history_point(utc_text(self.now), 10))
        store.record_incidents((incident_event(1, utc_text(self.now)),))
        store.record_gpu_telemetry("gpu-01", (gpu_point(utc_text(self.now), 10),), ())
        flusher, results = self.queue_flush(store)
        release.set()
        store._writer.join(5)
        flusher.join(5)
        self.assertEqual(results, [False])
        self.assertEqual(store.status()["droppedWrites"], 3)
        self.assertEqual(store.status()["queuedWrites"], 0)
        self.assertEqual(store._queue.unfinished_tasks, 0)
        self.assertEqual(
            store.status()["lastError"], "history writer could not open the database"
        )
        self.assertFalse(store.flush(0.1))
        store.record_history("gpu-01", history_point(utc_text(self.now), 20))
        self.assertEqual(store.status()["droppedWrites"], 4)

    def test_persistence_drops_flush_reports_loss_completed_before_call(self) -> None:
        store = self.store()
        committed = threading.Event()
        real_commit = store._commit_batch

        def commit(conn, writes):
            result = real_commit(conn, writes)
            committed.set()
            return result

        with (
            mock.patch.object(store, "_commit_batch", commit),
            mock.patch.object(
                store,
                "_write",
                side_effect=sqlite3.OperationalError("injected write failure"),
            ),
        ):
            store.record_history("gpu-01", history_point(utc_text(self.now), 10))
            self.assertTrue(committed.wait(5))
            self.assertEqual(store.status()["droppedWrites"], 1)
            self.assertFalse(store.flush())
        store.record_history("gpu-01", history_point(utc_text(self.now), 20))
        self.assertTrue(store.flush())
        self.assertEqual(store.load(20, 20).history["gpu-01"][0]["cpuUsagePct"], 20)

    def test_persistence_drops_failed_batch_counts_writes_and_rolls_back(self) -> None:
        store, release = self.gated_writer()
        real_write = store._write
        calls = 0

        def write(conn, item, busy):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise sqlite3.OperationalError("injected third write failure")
            return real_write(conn, item, busy)

        with mock.patch.object(store, "_write", write):
            for i in range(4):
                store.record_history(
                    "gpu-01",
                    history_point(utc_text(self.now + timedelta(seconds=i)), i),
                )
            flusher, results = self.queue_flush(store)
            release.set()
            flusher.join(5)
        self.assertEqual(results, [False])
        self.assertEqual(store.status()["droppedWrites"], 4)
        self.assertEqual(store.status()["writtenRecords"], 0)
        self.assertEqual(store.load(20, 20).history, {})
        self.assertTrue(store.flush())

    def test_persistence_drops_unexpected_crash_drains_current_and_pending(
        self,
    ) -> None:
        store, release = self.gated_writer()
        with mock.patch.object(
            store, "_write", side_effect=TypeError("injected corrupt record")
        ):
            for i in range(4):
                store.record_history(
                    "gpu-01",
                    history_point(utc_text(self.now + timedelta(seconds=i)), i),
                )
            flusher, results = self.queue_flush(store)
            release.set()
            store._writer.join(5)
            flusher.join(5)
        self.assertEqual(results, [False])
        self.assertEqual(store.status()["droppedWrites"], 4)
        self.assertEqual(store._queue.unfinished_tasks, 0)
        self.assertEqual(store.status()["writtenRecords"], 0)

    def test_persistence_drops_timeout_does_not_acknowledge_later_loss(self) -> None:
        store, release = self.gated_writer()
        committed = threading.Event()
        real_commit = store._commit_batch

        def commit(conn, writes):
            result = real_commit(conn, writes)
            committed.set()
            return result

        with (
            mock.patch.object(store, "_commit_batch", commit),
            mock.patch.object(
                store, "_write", side_effect=sqlite3.OperationalError("injected loss")
            ),
        ):
            store.record_history("gpu-01", history_point(utc_text(self.now), 10))
            self.assertFalse(store.flush(0))
            release.set()
            self.assertTrue(committed.wait(5))
            self.assertFalse(store.flush())
        self.assertEqual(store.status()["droppedWrites"], 1)
        self.assertTrue(store.flush())

    def test_persistence_restore_critical_interrupted_metadata_migration(self) -> None:
        create_legacy_database(self.path, 3)

        class InterruptedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("CREATE TABLE IF NOT EXISTS gpu_rollup_busy"):
                    raise sqlite3.OperationalError("injected migration interruption")
                return super().execute(sql, parameters)

        with closing(sqlite3.connect(self.path, factory=InterruptedConnection)) as conn:
            with self.assertRaisesRegex(sqlite3.OperationalError, "interruption"):
                SqliteTelemetryPersistence._migrate_companions(conn)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertEqual(
                tables, {"history", "incident_events", "gpu_history", "process_events"}
            )
        store = self.store()
        event = incident_event(1, utc_text(self.now))
        event = replace(event, condition=replace(event.condition, critical_at=93))
        store.record_incidents((event,))
        self.assertTrue(store.flush())
        self.assertEqual(store.load(20, 20).open_incidents[0].condition.critical_at, 93)

    def test_persistence_rollup_replace_legacy_raw_contribution_stays_frozen(
        self,
    ) -> None:
        # The legacy busy threshold is unknowable. Guessing the current one
        # can subtract a busy sample that the older writer never counted.
        create_legacy_database(self.path, 3)
        stamp = utc_text(self.now)
        hour = utc_text(self.now.replace(minute=0, second=0))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for statement in ROLLUP_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO gpu_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", 0, stamp, 50, 1024, 8192, 60, 120),
            )
            conn.execute(
                "INSERT INTO gpu_hourly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("gpu-01", "GPU-1", hour, 1, 1, 0, 50, 1, 1024, 8192),
            )
        expected = self.hourly()
        store = self.store(busy_pct=10)
        for value in (5, 95):
            store.record_gpu_telemetry("gpu-01", (gpu_point(stamp, value),), ())
            self.assertTrue(store.flush())
            self.assertEqual(self.hourly(), expected)
        fresh = gpu_point(utc_text(self.now + timedelta(seconds=1)), 80)
        store.record_gpu_telemetry("gpu-01", (fresh,), ())
        self.assertTrue(store.flush())
        store.record_gpu_telemetry("gpu-01", (dict(fresh, utilizationGpuPct=5),), ())
        self.assertTrue(store.flush())
        self.assertEqual(self.hourly(), [(2, 2, 0, 55, 2, 2048, 8192)])

    @unittest.skipUnless(
        hasattr(signal, "SIGKILL"), "requires abrupt process termination"
    )
    def test_persistence_rollup_replace_killed_transaction_restores_committed_rows(
        self,
    ) -> None:
        script = textwrap.dedent("""
            import os
            import signal
            import sys
            from dataclasses import replace
            from pathlib import Path
            import tests
            from tests.test_persistence import gpu_point, incident_event
            from mocop.config import PersistenceConfig
            from mocop.persistence import SqliteTelemetryPersistence

            store = SqliteTelemetryPersistence(
                PersistenceConfig(enabled=True, retention_hours=1), Path(sys.argv[1])
            )
            event = incident_event(1, sys.argv[2])
            event = replace(event, condition=replace(event.condition, critical_at=93))
            store.record_incidents((event,))
            store.record_gpu_telemetry("gpu-01", (gpu_point(sys.argv[2], 90),), ())
            assert store.flush()
            original = store._write

            def kill_mid_transaction(connection, item, busy):
                original(connection, item, busy)
                os.kill(os.getpid(), signal.SIGKILL)

            store._write = kill_mid_transaction
            store.record_gpu_telemetry("gpu-01", (gpu_point(sys.argv[2], 5),), ())
            store.record_incidents((replace(event, event_id=2),))
            store.flush(10)
            raise AssertionError("writer did not terminate the process")
        """)
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.path), utc_text(self.now)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        store = self.store()
        loaded = store.load(20, 20)
        self.assertEqual(
            loaded.gpu_history[("gpu-01", "GPU-1")][0]["utilizationGpuPct"], 90
        )
        self.assertEqual(self.hourly(), [(1, 1, 1, 90, 1, 1024, 8192)])
        self.assertEqual(len(loaded.incident_events), 1)
        self.assertEqual(loaded.open_incidents[0].condition.critical_at, 93)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_persistence_rollup_replace_unavailable_rollups_rejects_raw_change(
        self,
    ) -> None:
        store = self.store()
        point = gpu_point(utc_text(self.now), 90)
        store.record_gpu_telemetry("gpu-01", (point,), ())
        self.assertTrue(store.flush())
        expected = self.hourly()
        store.close()
        with mock.patch(
            "mocop.persistence.ensure_rollups",
            return_value="hourly rollups wait for retention to free space",
        ):
            store = self.store()
        store.record_gpu_telemetry("gpu-01", (dict(point, utilizationGpuPct=5),), ())
        self.assertFalse(store.flush())
        self.assertEqual(store.status()["droppedWrites"], 1)
        self.assertEqual(self.hourly(), expected)
        self.assertEqual(store.load(20, 20).gpu_history[("gpu-01", "GPU-1")], (point,))

    def test_persistence_rollup_replace_retention_frees_pages_before_archiving(
        self,
    ) -> None:
        self.config = replace(self.config, max_bytes=131072)
        store = self.store()
        store.close()
        committed = 0
        with closing(store._connect()) as conn:
            for i in range(1000):
                point = dict(
                    gpu_point(utc_text(self.now), 90), gpuId=f"GPU-{i:04d}-" + "x" * 96
                )
                try:
                    with conn:
                        write_gpu_points(conn, "gpu-01", (point,), 10)
                except sqlite3.OperationalError as error:
                    self.assertIn("full", str(error).lower())
                    break
                committed += 1
            else:
                self.fail("fixture did not fill the database")
        self.assertGreater(committed, 1)
        self.prune_at(store, self.now + timedelta(hours=2))
        with closing(store._connect()) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM gpu_history").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM gpu_retired_memory").fetchone()[0],
                committed,
            )
        self.assertLessEqual(self.path.stat().st_size, self.config.max_bytes)
