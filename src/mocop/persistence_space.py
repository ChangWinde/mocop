"""Bounded SQLite page reclaim and startup rebuilding."""

from __future__ import annotations

import sqlite3

# Steady-state retention frees a few dozen pages a minute. The online reclaim
# after each prune is bounded in pages (8 MiB at the 4 KiB page size) and in
# pragma calls, so the writer's transaction stays short on every interpreter;
# a backlog left by a long downtime or a lowered cap is rebuilt away at startup.
_VACUUM_PAGES_PER_PRUNE = 2048
_VACUUM_CHUNK_PAGES = 1024
_VACUUM_CALLS_PER_PRUNE = 64


def _free_pages(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA freelist_count").fetchone()[0])


def _reclaim_free_pages(connection: sqlite3.Connection, page_budget: int) -> int:
    """Return up to ``page_budget`` freed pages to the filesystem online.

    ``PRAGMA incremental_vacuum`` releases pages as its statement is stepped,
    one per step, so a bare ``execute()`` reclaims a single page and the file
    keeps its high-water mark forever. Exhausting the cursor frees the rest on
    most interpreters, but CPython 3.11's ``sqlite3`` yields no rows for the
    pragma and still frees one page per call, so progress is measured on
    ``freelist_count`` and the call count is bounded: a large backlog is not
    this path's job (startup rebuilds the file instead), keeping up with the
    few dozen pages a minute that retention frees is.
    """
    reclaimed = 0
    for _call in range(_VACUUM_CALLS_PER_PRUNE):
        before = _free_pages(connection)
        chunk = min(before, page_budget - reclaimed, _VACUUM_CHUNK_PAGES)
        if chunk <= 0:
            break
        for _row in connection.execute(f"PRAGMA incremental_vacuum({chunk})"):
            pass
        freed = before - _free_pages(connection)
        if freed <= 0:
            break
        reclaimed += freed
    return reclaimed


def _rebuild_file(connection: sqlite3.Connection) -> None:
    """Return every free page at startup, preferring a one-statement rebuild.

    ``VACUUM`` needs temporary space up to the file's size; on a full disk or
    a file another process holds it fails without touching the database, so
    the online reclaim runs instead and the cap check judges what remains.
    A refusal to start would take collection and the dashboard down over a
    condition the persistence status already reports.
    """
    try:
        connection.execute("VACUUM")
    except sqlite3.OperationalError:
        with connection:
            _reclaim_free_pages(connection, _VACUUM_PAGES_PER_PRUNE)
