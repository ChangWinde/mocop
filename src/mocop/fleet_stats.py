"""Fleet-wide totals for the snapshot's ``stats`` block.

A pure function over the serialized servers, the decorated active conditions,
and the hosts inside a maintenance window, so the store's lock is held only
while the inputs are copied.
"""

from __future__ import annotations

from collections.abc import Iterable


def fleet_stats(
    servers: list[dict[str, object]],
    active_conditions: list[dict[str, object]],
    maintenance_hosts: Iterable[str],
    *,
    busy_pct: float,
) -> dict[str, object]:
    """Counts and capacity aggregates across online hosts and active incidents."""
    maintenance_hosts = frozenset(maintenance_hosts)
    host_incidents: dict[str, list[dict[str, object]]] = {}
    for condition in active_conditions:
        host_incidents.setdefault(str(condition["host"]), []).append(condition)
    online = sum(server["status"] == "online" for server in servers)
    current_servers = [server for server in servers if server["status"] == "online"]
    gpus = [gpu for server in current_servers for gpu in server["gpus"]]
    systems = [server["system"] for server in current_servers if server["system"]]
    memory_total = sum(float(gpu["memory_total_mib"] or 0) for gpu in gpus)
    memory_used = sum(float(gpu["memory_used_mib"] or 0) for gpu in gpus)
    busy = sum(float(gpu["utilization_gpu_pct"] or 0) >= busy_pct for gpu in gpus)
    cpu_values = [
        float(system["cpu_usage_pct"])
        for system in systems
        if system["cpu_usage_pct"] is not None
    ]
    system_memory_total = sum(float(system["memory_total_mib"]) for system in systems)
    system_memory_used = sum(float(system["memory_used_mib"]) for system in systems)
    swap_total = sum(float(system["swap_total_mib"]) for system in systems)
    swap_used = sum(float(system["swap_used_mib"]) for system in systems)
    disk_total = sum(float(system["disk_total_mib"]) for system in systems)
    disk_used = sum(float(system["disk_used_mib"]) for system in systems)
    network_rx = sum(float(system["network_rx_bps"] or 0) for system in systems)
    network_tx = sum(float(system["network_tx_bps"] or 0) for system in systems)
    disk_read = sum(float(system["disk_read_bps"] or 0) for system in systems)
    disk_write = sum(float(system["disk_write_bps"] or 0) for system in systems)
    active_incidents = len(active_conditions)
    critical_incidents = sum(
        condition["severity"] == "critical" for condition in active_conditions
    )
    active_incident_hosts = frozenset(host_incidents)
    actionable_conditions = [
        condition for condition in active_conditions if condition["actionable"]
    ]
    actionable_incidents = len(actionable_conditions)
    actionable_critical = sum(
        condition["severity"] == "critical" for condition in actionable_conditions
    )
    actionable_incident_hosts = frozenset(
        str(condition["host"]) for condition in actionable_conditions
    )
    non_online_hosts = {
        str(server["host"]) for server in servers if server["status"] != "online"
    }
    untracked_non_online_hosts = {
        host for host in non_online_hosts if host not in active_incident_hosts
    }
    actionable_issue_hosts = (
        untracked_non_online_hosts - maintenance_hosts
    ) | actionable_incident_hosts
    return {
        "servers": len(servers),
        "onlineServers": online,
        "issueServers": len(non_online_hosts | active_incident_hosts),
        "incidentServers": len(active_incident_hosts),
        "actionableIssueServers": len(actionable_issue_hosts),
        "actionableIncidentServers": len(actionable_incident_hosts),
        "maintenanceServers": len(maintenance_hosts),
        "staleServers": sum(bool(server["stale"]) for server in servers),
        "pollingServers": sum(bool(server["polling"]) for server in servers),
        "activeIncidents": active_incidents,
        "criticalIncidents": critical_incidents,
        "actionableIncidents": actionable_incidents,
        "actionableCriticalIncidents": actionable_critical,
        "gpus": len(gpus),
        "busyGpus": busy,
        "memoryTotalMiB": round(memory_total, 1),
        "memoryUsedMiB": round(memory_used, 1),
        "cpuAveragePct": round(sum(cpu_values) / len(cpu_values), 2)
        if cpu_values
        else None,
        "cpuCores": sum(int(system["cpu_cores"]) for system in systems),
        "systemMemoryTotalMiB": round(system_memory_total, 1),
        "systemMemoryUsedMiB": round(system_memory_used, 1),
        "swapTotalMiB": round(swap_total, 1),
        "swapUsedMiB": round(swap_used, 1),
        "diskTotalMiB": round(disk_total, 1),
        "diskUsedMiB": round(disk_used, 1),
        "networkRxBps": round(network_rx, 1),
        "networkTxBps": round(network_tx, 1),
        "diskReadBps": round(disk_read, 1),
        "diskWriteBps": round(disk_write, 1),
    }
