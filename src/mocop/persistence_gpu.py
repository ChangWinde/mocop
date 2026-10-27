"""Raw GPU replacement, rollup deltas, and the durable retention boundary."""

from __future__ import annotations

import sqlite3

from .persistence_rollups import rollup_row, upsert_rollups


def gpu_cutoff(connection: sqlite3.Connection, cutoff: str) -> str:
    """Never move the expiry boundary backward when the wall clock does."""
    connection.execute(
        "INSERT INTO gpu_retention VALUES (1, ?) ON CONFLICT(singleton) "
        "DO UPDATE SET cutoff = max(cutoff, excluded.cutoff)",
        (cutoff,),
    )
    return connection.execute("SELECT cutoff FROM gpu_retention").fetchone()[0]


def filter_gpu_points(
    points: tuple[dict[str, object], ...], cutoff: str
) -> tuple[dict[str, object], ...]:
    return tuple(
        point
        for point in points
        if not isinstance(point.get("observedAt"), str) or point["observedAt"] >= cutoff
    )


def prune_gpu(connection: sqlite3.Connection, cutoff: str) -> None:
    # Preserve maxima from expired samples; retained samples remain replaceable.
    maxima = connection.execute(
        """
        SELECT host, gpu_id, substr(observed_at, 1, 13) || ':00:00Z',
               max(memory_total_mib)
        FROM gpu_history WHERE observed_at < ?
        GROUP BY host, gpu_id, substr(observed_at, 1, 13)
        """,
        (cutoff,),
    ).fetchall()
    # Delete first so archiving can reuse pages even at max_page_count.
    # Both steps commit together; a failure restores the raw rows too.
    connection.execute("DELETE FROM gpu_history WHERE observed_at < ?", (cutoff,))
    connection.execute("DELETE FROM gpu_rollup_busy WHERE observed_at < ?", (cutoff,))
    connection.executemany(
        """
        INSERT INTO gpu_retired_memory VALUES (?, ?, ?, ?)
        ON CONFLICT(host, gpu_id, hour) DO UPDATE SET memory_total_max =
            max(coalesce(memory_total_max, 0), coalesce(excluded.memory_total_max, 0))
        """,
        maxima,
    )


def write_gpu_points(
    connection: sqlite3.Connection,
    host: str,
    points: tuple[dict[str, object], ...],
    busy_pct: float | None,
) -> int:
    count = 0
    for point in points:
        key = (host, point.get("gpuId"), point.get("observedAt"))
        old = connection.execute(
            "SELECT utilization_gpu_pct, memory_used_mib, memory_total_mib "
            "FROM gpu_history WHERE host = ? AND gpu_id = ? AND observed_at = ?",
            key,
        ).fetchone()
        legacy = False
        if old is not None and busy_pct is not None:
            previous = dict(
                point,
                utilizationGpuPct=old[0],
                memoryUsedMiB=old[1],
                memoryTotalMiB=old[2],
            )
            row = rollup_row(host, previous, busy_pct)
            busy = connection.execute(
                "SELECT busy FROM gpu_rollup_busy "
                "WHERE host = ? AND gpu_id = ? AND observed_at = ?",
                key,
            ).fetchone()
            legacy = busy is None
            if not legacy:
                connection.execute(
                    """
                    UPDATE gpu_hourly SET samples = samples - 1,
                        utilization_samples = utilization_samples - ?,
                        busy_samples = busy_samples - ?, utilization_sum = utilization_sum - ?,
                        memory_samples = memory_samples - ?, memory_used_sum = memory_used_sum - ?
                    WHERE host = ? AND gpu_id = ? AND hour = ?
                    """,
                    (row[3], busy[0], *row[5:8], *row[:3]),
                )
        connection.execute(
            "INSERT OR REPLACE INTO gpu_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                host,
                point.get("gpuId"),
                point.get("index"),
                point.get("observedAt"),
                point.get("utilizationGpuPct"),
                point.get("memoryUsedMiB"),
                point.get("memoryTotalMiB"),
                point.get("temperatureC"),
                point.get("powerDrawW"),
            ),
        )
        count += 1
        if busy_pct is None or legacy:
            # Older aggregates have no per-sample busy threshold. Preserve
            # their contribution rather than guessing an inverse for it.
            continue
        upsert_rollups(connection, host, (point,), busy_pct)
        row = rollup_row(host, point, busy_pct)
        connection.execute(
            "INSERT OR REPLACE INTO gpu_rollup_busy VALUES (?, ?, ?, ?)", (*key, row[4])
        )
        if old is not None:
            # max() has no inverse; recompute it from retained rows plus the
            # expired maximum. Legacy aggregates keep their historical floor.
            connection.execute(
                """
                UPDATE gpu_hourly SET memory_total_max = (
                    SELECT max(value) FROM (
                        SELECT max(memory_total_mib) AS value FROM gpu_history
                        WHERE host = ? AND gpu_id = ? AND substr(observed_at, 1, 13) = ?
                        UNION ALL
                        SELECT memory_total_max FROM gpu_retired_memory
                        WHERE host = ? AND gpu_id = ? AND hour = ?
                    )
                ) WHERE host = ? AND gpu_id = ? AND hour = ?
                """,
                (*key[:2], str(key[2])[:13], *row[:3], *row[:3]),
            )
    return count
