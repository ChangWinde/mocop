"""Bootstrap legacy GPU expiry without losing its durable cutoff."""

from __future__ import annotations

import sqlite3

from .persistence_gpu import gpu_cutoff, prune_gpu
from .persistence_schema import GPU_RETENTION_STATEMENT


def companion_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('incident_criteria', 'gpu_retention', 'gpu_rollup_busy', 'gpu_retired_memory')"
        )
    }


def bootstrap_gpu_retention(
    connection: sqlite3.Connection, cutoff: str, max_bytes: int
) -> None:
    """Keep the watermark and deletion atomic, including deferred metadata.

    Startup has not applied max_page_count yet: expiry can free pages needed by
    the one companion root. If the retained data plus that root does not fit,
    roll back this unit and retain the raw rows. The backend rejects GPU writes
    until all companions fit, so a deferred upgrade cannot admit a replay.
    """
    connection.execute("SAVEPOINT gpu_retention_bootstrap")
    try:
        connection.execute(GPU_RETENTION_STATEMENT)
        boundary = gpu_cutoff(connection, cutoff)
        tables = companion_tables(connection)
        if "gpu_retired_memory" in tables:
            prune_gpu(connection, boundary, busy_metadata="gpu_rollup_busy" in tables)
        else:
            connection.execute(
                "DELETE FROM gpu_history WHERE observed_at < ?", (boundary,)
            )
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        free = connection.execute("PRAGMA freelist_count").fetchone()[0]
        size = connection.execute("PRAGMA page_size").fetchone()[0]
        if pages - free > max(1, max_bytes // size):
            connection.execute("ROLLBACK TO gpu_retention_bootstrap")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK TO gpu_retention_bootstrap")
            connection.execute("RELEASE gpu_retention_bootstrap")
        raise
    else:
        connection.execute("RELEASE gpu_retention_bootstrap")
