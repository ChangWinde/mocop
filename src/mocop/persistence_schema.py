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
INCIDENT_STATES = frozenset({"opened", "resolved", "escalated", "deescalated"})
INCIDENT_SEVERITIES = frozenset({"warning", "critical"})

# Rows whose stored types cannot round-trip are excluded in SQL before any
# restore limit applies, so corrupt rows never displace older valid records.
NUMERIC_COLUMN_TYPES = "('integer', 'real', 'null')"
# The in-memory history point requires these three percentages, so a NULL
# written by a foreign or corrupted database must not survive the restore:
# it would crash host initialization on every service start.
REQUIRED_NUMERIC_COLUMN_TYPES = "('integer', 'real')"
REQUIRED_HISTORY_COLUMNS = frozenset(
    {"memory_usage_pct", "swap_usage_pct", "disk_usage_pct"}
)
REQUIRED_HISTORY_FIELDS = frozenset({"memoryUsagePct", "swapUsagePct", "diskUsagePct"})
HISTORY_ROW_FILTER = " AND ".join(
    (
        "typeof(host) = 'text'",
        "typeof(observed_at) = 'text'",
        *(
            f"typeof({column}) IN "
            + (
                REQUIRED_NUMERIC_COLUMN_TYPES
                if column in REQUIRED_HISTORY_COLUMNS
                else NUMERIC_COLUMN_TYPES
            )
            for column in (
                "cpu_usage_pct",
                "memory_usage_pct",
                "swap_usage_pct",
                "disk_usage_pct",
                "network_rx_bps",
                "network_tx_bps",
                "disk_read_bps",
                "disk_write_bps",
                "gpu_usage_pct",
                "gpu_memory_usage_pct",
                "gpu_temperature_c",
            )
        ),
        "transport_retried IN (0, 1)",
    )
)
INCIDENT_ROW_FILTER = " AND ".join(
    (
        "typeof(event_id) = 'integer'",
        "event_id >= 1",
        "typeof(host) = 'text'",
        "typeof(condition_key) = 'text'",
        "typeof(category) = 'text'",
        "typeof(resource) = 'text'",
        f"typeof(value) IN {NUMERIC_COLUMN_TYPES}",
        f"typeof(threshold) IN {NUMERIC_COLUMN_TYPES}",
        "typeof(condition_observed_at) = 'text'",
        "typeof(detail) IN ('text', 'null')",
        "typeof(group_key) IN ('text', 'null')",
        "typeof(observed_at) = 'text'",
    )
)
GPU_ROW_FILTER = " AND ".join(
    (
        "typeof(host) = 'text'",
        "typeof(gpu_id) = 'text'",
        "typeof(gpu_index) = 'integer'",
        "typeof(observed_at) = 'text'",
        *(
            f"typeof({column}) IN {NUMERIC_COLUMN_TYPES}"
            for column in (
                "utilization_gpu_pct",
                "memory_used_mib",
                "memory_total_mib",
                "temperature_c",
                "power_draw_w",
            )
        ),
    )
)
PROCESS_ROW_FILTER = " AND ".join(
    (
        "typeof(p.host) = 'text'",
        "typeof(p.gpu_id) = 'text'",
        "typeof(p.gpu_index) = 'integer'",
        "typeof(p.observed_at) = 'text'",
        "p.event_type IN ('started', 'stopped')",
        "typeof(p.pid) = 'integer'",
        "typeof(p.name) = 'text'",
        f"typeof(p.used_memory_mib) IN {NUMERIC_COLUMN_TYPES}",
        "typeof(p.workload_json) IN ('text', 'null')",
    )
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
