"""The hourly GPU rollup table and the reads behind the long-window reports.

``gpu_hourly`` aggregates ``gpu_history`` per (host, device, UTC hour). The
writer upserts it in the same transaction as the raw points; startup creates
it after the size cap applies and backfills the raw hours it has never seen;
retention keeps it for 90 days or the raw retention, whichever is longer.
Report reads take every retained process transition (pairing needs starts
from before the window) and the rollups from the window's first hour on.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from .persistence_schema import PROCESS_ROW_FILTER
from .persistence_transitions import decode_process_rows, emission_order
from .reports import ReportInputs

# Hourly rollups of gpu_history, maintained by the writer in the same
# transaction as the raw points and backfilled from the raw table at startup.
# Busy samples are counted at the busy threshold in effect when written.
ROLLUP_RETENTION_HOURS = 24 * 90
ROLLUP_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS gpu_hourly (
        host TEXT NOT NULL,
        gpu_id TEXT NOT NULL,
        hour TEXT NOT NULL,
        samples INTEGER NOT NULL,
        utilization_samples INTEGER NOT NULL,
        busy_samples INTEGER NOT NULL,
        utilization_sum REAL NOT NULL,
        memory_samples INTEGER NOT NULL,
        memory_used_sum REAL NOT NULL,
        memory_total_max REAL,
        PRIMARY KEY (host, gpu_id, hour)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS gpu_hourly_hour ON gpu_hourly(hour)",
)
ROLLUP_UPSERT = """
    INSERT INTO gpu_hourly VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(host, gpu_id, hour) DO UPDATE SET
        samples = samples + 1,
        utilization_samples = utilization_samples + excluded.utilization_samples,
        busy_samples = busy_samples + excluded.busy_samples,
        utilization_sum = utilization_sum + excluded.utilization_sum,
        memory_samples = memory_samples + excluded.memory_samples,
        memory_used_sum = memory_used_sum + excluded.memory_used_sum,
        memory_total_max = max(
            coalesce(memory_total_max, 0), coalesce(excluded.memory_total_max, 0)
        )
"""
# Hours already rolled up are left alone (INSERT OR IGNORE), so a partial
# current hour from an earlier run keeps its counts and only the raw rows of
# hours the table has never seen are aggregated.
ROLLUP_BACKFILL = """
    INSERT OR IGNORE INTO gpu_hourly
    SELECT host, gpu_id, substr(observed_at, 1, 13) || ':00:00Z',
           count(*), count(utilization_gpu_pct),
           coalesce(sum(utilization_gpu_pct >= ?), 0),
           coalesce(sum(utilization_gpu_pct), 0),
           count(memory_used_mib), coalesce(sum(memory_used_mib), 0),
           max(memory_total_mib)
    FROM gpu_history
    WHERE observed_at >= ? AND typeof(observed_at) = 'text'
    GROUP BY host, gpu_id, substr(observed_at, 1, 13)
"""


def ensure_rollups(connection: sqlite3.Connection, busy_pct: float) -> str | None:
    """Create the hourly rollup table and aggregate raw hours it lacks.

    Returns the persistence status message when the table cannot be created.

    This runs after the size cap is applied, so a database already at its
    cap keeps starting exactly as before: the rollups then wait for
    retention to free pages and are retried at the next start, and the
    writer skips them meanwhile. The whole retained raw table aggregates
    in well under a second per million rows; later starts only read from
    the last rolled-up hour onward.
    """
    try:
        with connection:
            for statement in ROLLUP_STATEMENTS:
                connection.execute(statement)
            latest = connection.execute("SELECT max(hour) FROM gpu_hourly").fetchone()[
                0
            ]
            since = latest if isinstance(latest, str) else ""
            connection.execute(ROLLUP_BACKFILL, (busy_pct, since))
    except sqlite3.OperationalError:
        return "hourly rollups wait for retention to free space"
    return None


def rollup_row(
    host: str, point: dict[str, object], busy_pct: float
) -> tuple[object, ...]:
    utilization = point.get("utilizationGpuPct")
    memory_used = point.get("memoryUsedMiB")
    has_utilization = isinstance(utilization, int | float) and not isinstance(
        utilization, bool
    )
    has_memory = isinstance(memory_used, int | float) and not isinstance(
        memory_used, bool
    )
    return (
        host,
        point.get("gpuId"),
        str(point["observedAt"])[:13] + ":00:00Z",
        1 if has_utilization else 0,
        1 if has_utilization and float(utilization) >= busy_pct else 0,  # type: ignore[arg-type]
        float(utilization) if has_utilization else 0.0,  # type: ignore[arg-type]
        1 if has_memory else 0,
        float(memory_used) if has_memory else 0.0,  # type: ignore[arg-type]
        point.get("memoryTotalMiB"),
    )


def upsert_rollups(
    connection: sqlite3.Connection,
    host: str,
    points: Sequence[dict[str, object]],
    busy_pct: float,
) -> None:
    connection.executemany(
        ROLLUP_UPSERT,
        (
            rollup_row(host, point, busy_pct)
            for point in points
            if isinstance(point.get("observedAt"), str)
        ),
    )


def prune_rollups(connection: sqlite3.Connection, retention_hours: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=max(retention_hours, ROLLUP_RETENTION_HOURS)
    )
    connection.execute(
        "DELETE FROM gpu_hourly WHERE hour < ?",
        (cutoff.isoformat(timespec="seconds").replace("+00:00", "Z"),),
    )


def load_report_inputs(
    connection: sqlite3.Connection, retention_hours: int, since_hour: str
) -> ReportInputs:
    """Read what a long-window report needs: every retained process transition
    (pairing needs starts from before the window) and the hourly rollups from
    ``since_hour`` onward. Both tables are small; neither read holds the
    writer up for long."""
    process_rows = connection.execute(
        f"""
        SELECT host, gpu_id, gpu_index, observed_at, event_type,
               pid, name, used_memory_mib, workload_json
        FROM process_events AS p
        WHERE {PROCESS_ROW_FILTER}
        """
    ).fetchall()
    hourly_rows = connection.execute(
        """
        SELECT host, gpu_id, hour, samples, utilization_samples, busy_samples,
               utilization_sum, memory_samples, memory_used_sum, memory_total_max
        FROM gpu_hourly WHERE hour >= ? ORDER BY host, gpu_id, hour
        """,
        (since_hour,),
    ).fetchall()
    transitions = {
        key: tuple(emission_order(items))
        for key, items in decode_process_rows(process_rows).items()
    }
    return ReportInputs(
        transitions=transitions,
        hourly=tuple(
            row
            for row in hourly_rows
            if isinstance(row[0], str)
            and isinstance(row[1], str)
            and isinstance(row[2], str)
        ),
        retention_hours=retention_hours,
    )
