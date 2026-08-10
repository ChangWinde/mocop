from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

OPENMETRICS_CONTENT_TYPE = "application/openmetrics-text; version=1.0.0; charset=utf-8"

Labels = Sequence[tuple[str, str]]
Sample = tuple[Labels, int | float]


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _label_set(labels: Labels) -> str:
    if not labels:
        return ""
    rendered = ",".join(f'{name}="{_escape_label(value)}"' for name, value in labels)
    return f"{{{rendered}}}"


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("OpenMetrics samples must be finite")
    if number == 0:
        return "0"
    if number.is_integer():
        return str(int(number))
    return format(number, ".15g")


def _family(
    lines: list[str],
    name: str,
    help_text: str,
    samples: Iterable[Sample],
    *,
    unit: str | None = None,
) -> None:
    values = list(samples)
    if not values:
        return
    lines.append(f"# TYPE {name} gauge")
    if unit is not None:
        lines.append(f"# UNIT {name} {unit}")
    lines.append(f"# HELP {name} {help_text}")
    lines.extend(
        f"{name}{_label_set(labels)} {_number(value)}" for labels, value in values
    )


def _metric(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mib_as_bytes(value: object) -> int | float | None:
    number = _metric(value)
    return number * 1024 * 1024 if number is not None else None


def _percentage_as_ratio(value: object) -> float | None:
    number = _metric(value)
    return number / 100 if number is not None else None


def _seconds_from_milliseconds(value: object) -> float | None:
    number = _metric(value)
    return number / 1000 if number is not None else None


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _append_optional(samples: list[Sample], labels: Labels, value: object) -> None:
    number = _metric(value)
    if number is not None:
        samples.append((labels, number))


def _append_bytes(samples: list[Sample], labels: Labels, value: object) -> None:
    number = _mib_as_bytes(value)
    if number is not None:
        samples.append((labels, number))


def _append_ratio(samples: list[Sample], labels: Labels, value: object) -> None:
    number = _percentage_as_ratio(value)
    if number is not None:
        samples.append((labels, number))


def render_openmetrics(snapshot: Mapping[str, object]) -> bytes:
    """Render one immutable state snapshot as an OpenMetrics 1.0 exposition."""
    lines: list[str] = []
    stats = snapshot.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    servers_value = snapshot.get("servers")
    servers = servers_value if isinstance(servers_value, list) else []

    version = snapshot.get("appVersion")
    _family(
        lines,
        "mocop_build_info",
        "Mocop build information.",
        [((("version", str(version)),), 1)] if version is not None else [],
    )
    _family(
        lines,
        "mocop_collection_ready",
        "Whether at least one collection cycle has completed without a collector error.",
        [
            (
                (),
                int(
                    snapshot.get("lastPollCompletedAt") is not None
                    and snapshot.get("collectorError") is None
                ),
            )
        ],
    )
    _family(
        lines,
        "mocop_collection_poll_interval_seconds",
        "Configured global collection cadence.",
        [((), value)]
        if (value := _metric(snapshot.get("pollIntervalSeconds"))) is not None
        else [],
        unit="seconds",
    )
    _family(
        lines,
        "mocop_collection_duration_seconds",
        "Duration of the most recently completed collection cycle.",
        [((), value)]
        if (value := _seconds_from_milliseconds(snapshot.get("lastPollDurationMs")))
        is not None
        else [],
        unit="seconds",
    )
    _family(
        lines,
        "mocop_collection_last_completed_timestamp_seconds",
        "Unix timestamp of the most recently completed collection cycle.",
        [((), value)]
        if (value := _timestamp(snapshot.get("lastPollCompletedAt"))) is not None
        else [],
        unit="seconds",
    )
    _family(
        lines,
        "mocop_snapshot_generated_timestamp_seconds",
        "Unix timestamp when this Mocop snapshot was generated.",
        [((), value)]
        if (value := _timestamp(snapshot.get("generatedAt"))) is not None
        else [],
        unit="seconds",
    )

    cluster_families = (
        ("mocop_cluster_servers", "Configured servers.", "servers"),
        ("mocop_cluster_servers_online", "Currently online servers.", "onlineServers"),
        (
            "mocop_cluster_servers_stale",
            "Servers exposing stale last-known data.",
            "staleServers",
        ),
        (
            "mocop_cluster_servers_maintenance",
            "Servers in a maintenance window.",
            "maintenanceServers",
        ),
        ("mocop_cluster_gpus", "GPUs on currently online servers.", "gpus"),
        ("mocop_cluster_gpus_busy", "Currently busy GPUs.", "busyGpus"),
        (
            "mocop_cluster_servers_issue",
            "Servers with any current issue.",
            "issueServers",
        ),
        (
            "mocop_cluster_servers_actionable_issue",
            "Servers with a current issue excluding maintained hosts.",
            "actionableIssueServers",
        ),
        (
            "mocop_cluster_servers_incident",
            "Servers with an active incident.",
            "incidentServers",
        ),
        (
            "mocop_cluster_servers_actionable_incident",
            "Servers with an active incident excluding maintained hosts.",
            "actionableIncidentServers",
        ),
        ("mocop_cluster_incidents_active", "Raw active incidents.", "activeIncidents"),
        (
            "mocop_cluster_incidents_critical",
            "Raw active critical incidents.",
            "criticalIncidents",
        ),
        (
            "mocop_cluster_incidents_actionable",
            "Active incidents excluding maintained hosts.",
            "actionableIncidents",
        ),
        (
            "mocop_cluster_incidents_actionable_critical",
            "Active critical incidents excluding maintained hosts.",
            "actionableCriticalIncidents",
        ),
    )
    for name, help_text, key in cluster_families:
        _family(
            lines,
            name,
            help_text,
            [((), value)] if (value := _metric(stats.get(key))) is not None else [],
        )
    for name, help_text, key in (
        (
            "mocop_cluster_gpu_memory_total_bytes",
            "Total VRAM on online GPUs.",
            "memoryTotalMiB",
        ),
        (
            "mocop_cluster_gpu_memory_used_bytes",
            "Used VRAM on online GPUs.",
            "memoryUsedMiB",
        ),
    ):
        _family(
            lines,
            name,
            help_text,
            [((), value)]
            if (value := _mib_as_bytes(stats.get(key))) is not None
            else [],
            unit="bytes",
        )

    host_samples: dict[str, list[Sample]] = {
        "info": [],
        "up": [],
        "stale": [],
        "maintenance": [],
        "polling": [],
        "failures": [],
        "incidents": [],
        "critical_incidents": [],
        "actionable_incidents": [],
        "actionable_critical_incidents": [],
        "latency": [],
        "cpu": [],
        "load": [],
        "uptime": [],
        "memory_total": [],
        "memory_used": [],
        "swap_total": [],
        "swap_used": [],
        "disk_total": [],
        "disk_used": [],
        "network_rx": [],
        "network_tx": [],
        "disk_read": [],
        "disk_write": [],
    }
    gpu_samples: dict[str, list[Sample]] = {
        "info": [],
        "utilization": [],
        "memory_total": [],
        "memory_used": [],
        "memory_free": [],
        "temperature": [],
        "power_draw": [],
        "power_limit": [],
        "processes": [],
        "processes_available": [],
        "ecc": [],
        "retired_pages": [],
        "remapped_rows": [],
        "thermal_slowdown": [],
        "power_brake": [],
    }

    for raw_server in servers:
        if not isinstance(raw_server, Mapping):
            continue
        host = raw_server.get("host")
        if not isinstance(host, str):
            continue
        host_labels = (("host", host),)
        group = raw_server.get("group")
        host_samples["info"].append(
            (
                (
                    ("host", host),
                    ("mocop_group", group if isinstance(group, str) else ""),
                ),
                1,
            )
        )
        online = raw_server.get("status") == "online"
        stale = bool(raw_server.get("stale"))
        host_samples["up"].append((host_labels, int(online)))
        host_samples["stale"].append((host_labels, int(stale)))
        host_samples["maintenance"].append(
            (host_labels, int(raw_server.get("maintenance") is not None))
        )
        host_samples["polling"].append(
            (host_labels, int(bool(raw_server.get("polling"))))
        )
        _append_optional(
            host_samples["failures"], host_labels, raw_server.get("consecutiveFailures")
        )
        incidents = raw_server.get("incidents")
        incidents = incidents if isinstance(incidents, Mapping) else {}
        for sample_key, field in (
            ("incidents", "active"),
            ("critical_incidents", "critical"),
            ("actionable_incidents", "actionable"),
            ("actionable_critical_incidents", "actionableCritical"),
        ):
            _append_optional(
                host_samples[sample_key], host_labels, incidents.get(field)
            )
        latency = _seconds_from_milliseconds(raw_server.get("latencyMs"))
        if latency is not None:
            host_samples["latency"].append((host_labels, latency))

        system = raw_server.get("system")
        if not online or stale or not isinstance(system, Mapping):
            continue
        _append_ratio(host_samples["cpu"], host_labels, system.get("cpu_usage_pct"))
        _append_optional(host_samples["load"], host_labels, system.get("load_1m"))
        _append_optional(
            host_samples["uptime"], host_labels, system.get("uptime_seconds")
        )
        for sample_key, field in (
            ("memory_total", "memory_total_mib"),
            ("memory_used", "memory_used_mib"),
            ("swap_total", "swap_total_mib"),
            ("swap_used", "swap_used_mib"),
            ("disk_total", "disk_total_mib"),
            ("disk_used", "disk_used_mib"),
        ):
            _append_bytes(host_samples[sample_key], host_labels, system.get(field))
        for sample_key, field in (
            ("network_rx", "network_rx_bps"),
            ("network_tx", "network_tx_bps"),
            ("disk_read", "disk_read_bps"),
            ("disk_write", "disk_write_bps"),
        ):
            _append_optional(host_samples[sample_key], host_labels, system.get(field))

        raw_gpus = raw_server.get("gpus")
        if not isinstance(raw_gpus, list):
            continue
        for gpu in raw_gpus:
            if not isinstance(gpu, Mapping):
                continue
            index = gpu.get("index")
            uuid = gpu.get("uuid")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not isinstance(uuid, str)
            ):
                continue
            labels = (("host", host), ("index", str(index)), ("uuid", uuid))
            health = gpu.get("health")
            health = health if isinstance(health, Mapping) else {}
            gpu_samples["info"].append(
                (
                    (
                        *labels,
                        ("model", str(gpu.get("name") or "")),
                        ("driver", str(gpu.get("driver_version") or "")),
                        ("mig_mode", str(health.get("mig_mode") or "unknown")),
                    ),
                    1,
                )
            )
            _append_ratio(
                gpu_samples["utilization"], labels, gpu.get("utilization_gpu_pct")
            )
            for sample_key, field in (
                ("memory_total", "memory_total_mib"),
                ("memory_used", "memory_used_mib"),
                ("memory_free", "memory_free_mib"),
            ):
                _append_bytes(gpu_samples[sample_key], labels, gpu.get(field))
            for sample_key, field in (
                ("temperature", "temperature_c"),
                ("power_draw", "power_draw_w"),
                ("power_limit", "power_limit_w"),
            ):
                _append_optional(gpu_samples[sample_key], labels, gpu.get(field))
            processes = gpu.get("processes")
            if isinstance(processes, list):
                gpu_samples["processes"].append((labels, len(processes)))
            gpu_samples["processes_available"].append(
                (labels, int(gpu.get("processes_available") is True))
            )
            _append_optional(
                gpu_samples["ecc"], labels, health.get("ecc_uncorrected_volatile")
            )
            for sample_key, field in (
                ("retired_pages", "retired_pages_pending"),
                ("remapped_rows", "remapped_rows_pending"),
                ("thermal_slowdown", "thermal_slowdown"),
                ("power_brake", "power_brake_slowdown"),
            ):
                value = health.get(field)
                if isinstance(value, bool):
                    gpu_samples[sample_key].append((labels, int(value)))

    host_definitions = (
        ("info", "mocop_host_info", "Host inventory metadata.", None),
        ("up", "mocop_host_up", "Whether the most recent host probe succeeded.", None),
        (
            "stale",
            "mocop_host_stale",
            "Whether host resources are last-known stale data.",
            None,
        ),
        (
            "maintenance",
            "mocop_host_maintenance",
            "Whether the host is in a maintenance window.",
            None,
        ),
        (
            "polling",
            "mocop_host_polling",
            "Whether a probe is currently running for the host.",
            None,
        ),
        (
            "failures",
            "mocop_host_consecutive_failures",
            "Consecutive failed probes for the host.",
            None,
        ),
        (
            "incidents",
            "mocop_host_incidents_active",
            "Raw active incidents for the host.",
            None,
        ),
        (
            "critical_incidents",
            "mocop_host_incidents_critical",
            "Raw active critical incidents for the host.",
            None,
        ),
        (
            "actionable_incidents",
            "mocop_host_incidents_actionable",
            "Active incidents for the host excluding maintenance silence.",
            None,
        ),
        (
            "actionable_critical_incidents",
            "mocop_host_incidents_actionable_critical",
            "Active critical incidents for the host excluding maintenance silence.",
            None,
        ),
        (
            "latency",
            "mocop_host_probe_latency_seconds",
            "Duration of the most recent host probe attempt.",
            "seconds",
        ),
        (
            "cpu",
            "mocop_host_cpu_utilization_ratio",
            "Current host CPU utilization as a ratio.",
            None,
        ),
        ("load", "mocop_host_load1", "Current one-minute host load average.", None),
        ("uptime", "mocop_host_uptime_seconds", "Current host uptime.", "seconds"),
        (
            "memory_total",
            "mocop_host_memory_total_bytes",
            "Total host memory.",
            "bytes",
        ),
        ("memory_used", "mocop_host_memory_used_bytes", "Used host memory.", "bytes"),
        ("swap_total", "mocop_host_swap_total_bytes", "Total host swap.", "bytes"),
        ("swap_used", "mocop_host_swap_used_bytes", "Used host swap.", "bytes"),
        (
            "disk_total",
            "mocop_host_disk_total_bytes",
            "Total monitored filesystem capacity.",
            "bytes",
        ),
        (
            "disk_used",
            "mocop_host_disk_used_bytes",
            "Used monitored filesystem capacity.",
            "bytes",
        ),
        (
            "network_rx",
            "mocop_host_network_receive_bytes_per_second",
            "Current aggregate network receive rate.",
            "bytes_per_second",
        ),
        (
            "network_tx",
            "mocop_host_network_transmit_bytes_per_second",
            "Current aggregate network transmit rate.",
            "bytes_per_second",
        ),
        (
            "disk_read",
            "mocop_host_disk_read_bytes_per_second",
            "Current aggregate disk read rate.",
            "bytes_per_second",
        ),
        (
            "disk_write",
            "mocop_host_disk_write_bytes_per_second",
            "Current aggregate disk write rate.",
            "bytes_per_second",
        ),
    )
    for key, name, help_text, unit in host_definitions:
        _family(lines, name, help_text, host_samples[key], unit=unit)

    gpu_definitions = (
        ("info", "mocop_gpu_info", "Current GPU inventory metadata.", None),
        (
            "utilization",
            "mocop_gpu_utilization_ratio",
            "Current GPU compute utilization as a ratio.",
            None,
        ),
        ("memory_total", "mocop_gpu_memory_total_bytes", "Total GPU memory.", "bytes"),
        ("memory_used", "mocop_gpu_memory_used_bytes", "Used GPU memory.", "bytes"),
        ("memory_free", "mocop_gpu_memory_free_bytes", "Free GPU memory.", "bytes"),
        (
            "temperature",
            "mocop_gpu_temperature_celsius",
            "Current GPU temperature.",
            "celsius",
        ),
        (
            "power_draw",
            "mocop_gpu_power_draw_watts",
            "Current GPU power draw.",
            "watts",
        ),
        (
            "power_limit",
            "mocop_gpu_power_limit_watts",
            "Configured GPU power limit.",
            "watts",
        ),
        (
            "processes",
            "mocop_gpu_processes",
            "Current visible GPU compute processes.",
            None,
        ),
        (
            "processes_available",
            "mocop_gpu_process_telemetry_available",
            "Whether GPU process telemetry is available.",
            None,
        ),
        (
            "ecc",
            "mocop_gpu_ecc_uncorrected",
            "Current volatile uncorrected GPU ECC errors.",
            None,
        ),
        (
            "retired_pages",
            "mocop_gpu_retired_pages_pending",
            "Whether GPU page retirement is pending.",
            None,
        ),
        (
            "remapped_rows",
            "mocop_gpu_remapped_rows_pending",
            "Whether GPU row remapping is pending.",
            None,
        ),
        (
            "thermal_slowdown",
            "mocop_gpu_thermal_slowdown",
            "Whether GPU thermal slowdown is active.",
            None,
        ),
        (
            "power_brake",
            "mocop_gpu_power_brake_slowdown",
            "Whether GPU power-brake slowdown is active.",
            None,
        ),
    )
    for key, name, help_text, unit in gpu_definitions:
        _family(lines, name, help_text, gpu_samples[key], unit=unit)

    lines.append("# EOF")
    return ("\n".join(lines) + "\n").encode("utf-8")
