"""Stored row contracts shared by persistence readers."""

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
