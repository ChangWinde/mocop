"""The history database's schema: DDL, column contracts, and row validity.

``persistence.py`` owns connections, the writer thread, retention, and
restore; this module owns what the tables look like and which stored rows are
allowed to round-trip. The row filters run in SQL before any restore limit
applies, so a corrupt row never displaces an older valid record.
"""

from __future__ import annotations

SCHEMA_VERSION = 3

HISTORY_FIELDS = (
    "cpuUsagePct",
    "memoryUsagePct",
    "swapUsagePct",
    "diskUsagePct",
    "networkRxBps",
    "networkTxBps",
    "diskReadBps",
    "diskWriteBps",
    "gpuUsagePct",
    "gpuMemoryUsagePct",
    "gpuTemperatureC",
)

GPU_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS gpu_history (
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
    "CREATE INDEX IF NOT EXISTS gpu_history_observed_at ON gpu_history(observed_at)",
    """
    CREATE TABLE IF NOT EXISTS process_events (
        host TEXT NOT NULL,
        gpu_id TEXT NOT NULL,
        gpu_index INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN ('started', 'stopped')),
        pid INTEGER NOT NULL,
        name TEXT NOT NULL,
        used_memory_mib REAL,
        workload_json TEXT,
        PRIMARY KEY (
            host, gpu_id, observed_at, event_type, pid, name
        )
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS process_events_observed_at"
    " ON process_events(observed_at)",
)
CREATE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS history (
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
        transport_retried INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (host, observed_at)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS history_observed_at ON history(observed_at)",
    """
    CREATE TABLE IF NOT EXISTS incident_events (
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
    "CREATE INDEX IF NOT EXISTS incident_events_observed_at"
    " ON incident_events(observed_at)",
) + GPU_TABLE_STATEMENTS

# Additive companions preserve the released positional INSERT contracts.
GPU_RETENTION_STATEMENT = (
    "CREATE TABLE IF NOT EXISTS gpu_retention ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), cutoff TEXT NOT NULL)"
)
COMPANION_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS incident_criteria ("
    "event_id INTEGER PRIMARY KEY, critical_at REAL)",
    GPU_RETENTION_STATEMENT,
    "CREATE TABLE IF NOT EXISTS gpu_rollup_busy ("
    "host TEXT NOT NULL, gpu_id TEXT NOT NULL, observed_at TEXT NOT NULL,"
    "busy INTEGER NOT NULL, PRIMARY KEY(host, gpu_id, observed_at)) WITHOUT ROWID",
    "CREATE INDEX IF NOT EXISTS gpu_rollup_busy_observed_at ON gpu_rollup_busy(observed_at)",
    "CREATE TABLE IF NOT EXISTS gpu_retired_memory ("
    "host TEXT NOT NULL, gpu_id TEXT NOT NULL, hour TEXT NOT NULL,"
    "memory_total_max REAL, PRIMARY KEY(host, gpu_id, hour)) WITHOUT ROWID",
    "CREATE INDEX IF NOT EXISTS gpu_retired_memory_hour ON gpu_retired_memory(hour)",
)
LEGACY_MEMORY_BASELINE = """
    INSERT OR IGNORE INTO gpu_retired_memory
    SELECT host, gpu_id, hour, memory_total_max FROM gpu_hourly
"""
