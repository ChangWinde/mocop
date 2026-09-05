from __future__ import annotations

import re
from typing import Any

# Units by condition category: ratios are percentages, GPU temperature is
# degrees Celsius, and counters (GPU inventory, ECC errors) carry no unit.
_CATEGORY_UNITS = {
    "cpu": "%",
    "memory": "%",
    "swap": "%",
    "disk": "%",
    "pressure": "%",
    "gpu_memory": "%",
    "gpu_idle_memory": "%",
    "gpu_temperature": "°C",
}
_GPU_INDEX_IN_RESOURCE = re.compile(r"\bGPU (\d+)\b")

# The first next step for a connectivity condition follows from the sanitized
# failure classification in its detail (see models.SERVER_MESSAGES); anything
# unclassified keeps the generic batch-mode check.
_CONNECTIVITY_STEPS = {
    "SSH host key changed": (
        "Confirm the node was reinstalled or re-keyed on purpose, then update "
        "the monitor's known_hosts entry; the probe never accepts a changed key."
    ),
    "SSH host key is not trusted": (
        "Connect once interactively from the monitor to record the host key; "
        "the probe never accepts new keys on its own."
    ),
    "SSH authentication failed": (
        "Check that the monitor's key is still in the node's authorized_keys "
        "and that the alias's IdentityFile or agent still offers it."
    ),
    "SSH name resolution failed": (
        "Check the alias's HostName and the monitor's DNS resolution."
    ),
    "SSH jump host could not reach the target": (
        "From the jump host, test the target's SSH port directly and confirm "
        "its sshd permits TCP forwarding (AllowTcpForwarding, PermitOpen)."
    ),
    "SSH connection was refused": (
        "Confirm sshd is running on the node and listening on the configured port."
    ),
    "SSH connection timed out": (
        "Check routing and firewall rules between the monitor and the node's SSH port."
    ),
    "SSH network is unreachable": (
        "Check the monitor's routes and any VPN or tunnel the alias depends on."
    ),
    "SSH connection closed during key exchange": (
        "Check the node's sshd load and MaxStartups, and whether fail2ban or a "
        "proxy in front of it has banned the monitor."
    ),
    "SSH transport stopped responding": (
        "The node may have hung or lost its link; check its power, console, and uplink."
    ),
    "SSH produced no output before the collection timeout": (
        "Log in to the node and check whether it is stalled on I/O or a hung "
        "filesystem such as an unresponsive network mount."
    ),
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(float(value), 2)


def _threshold_evidence(
    condition: dict[str, object], category: str
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    unit = _CATEGORY_UNITS.get(category)
    for label, key in (("current", "value"), ("threshold", "threshold")):
        number = _number(condition.get(key))
        if number is None:
            continue
        item: dict[str, object] = {"label": label, "value": number}
        if unit is not None:
            item["unit"] = unit
        evidence.append(item)
    return evidence


def _filesystem_headroom_evidence(
    mountpoint: str, server: dict[str, object] | None
) -> list[dict[str, object]]:
    """Report the absolute headroom left on the alerting mount, in GiB."""
    system = (server or {}).get("system")
    if not isinstance(system, dict):
        return []
    for disk in system.get("disks", ()):
        if not isinstance(disk, dict) or disk.get("mountpoint") != mountpoint:
            continue
        evidence: list[dict[str, object]] = []
        for label, key in (("freeSpace", "available_mib"), ("capacity", "total_mib")):
            number = _number(disk.get(key))
            if number is None:
                continue
            evidence.append(
                {"label": label, "value": round(number / 1024, 1), "unit": "GiB"}
            )
        return evidence
    return []


def diagnose_condition(
    condition: dict[str, object],
    server: dict[str, object] | None,
) -> dict[str, object]:
    """Build bounded, deterministic diagnosis context from one active condition."""
    category = str(condition.get("category") or "unknown")
    resource = str(condition.get("resource") or "resource")
    evidence = _threshold_evidence(condition, category)
    title = "Resource condition needs attention"
    summary = f"{resource} is outside the configured operating threshold."
    next_steps = ["Confirm whether the current workload makes this state expected."]
    target_gpu_index: int | None = None

    if category == "connectivity":
        title = "Collection path unavailable"
        summary = "The fixed SSH probe did not complete, so current resource data is unavailable."
        next_steps = [
            "Verify the same OpenSSH alias in non-interactive batch mode.",
            "If several nodes failed together, inspect their shared jump host or tunnel.",
        ]
        specific = _CONNECTIVITY_STEPS.get(str(condition.get("detail") or ""))
        if specific is not None:
            next_steps.insert(0, specific)
        if server is not None:
            evidence.extend(
                (
                    {
                        "label": "consecutiveFailures",
                        "value": server.get("consecutiveFailures"),
                    },
                    {"label": "lastSuccessAt", "value": server.get("lastSuccessAt")},
                )
            )
    elif category == "disk":
        title = "Filesystem capacity is low"
        summary = f"{resource} crossed its configured used-capacity threshold."
        next_steps = [
            "Confirm whether the filesystem is expected to grow.",
            "Inspect large directories and retention policies on this mount.",
        ]
        # A percentage alone cannot be triaged: 96% of a 50 GiB root leaves
        # minutes of headroom while 99% of a 10 TiB volume leaves days.
        evidence.extend(_filesystem_headroom_evidence(resource, server))
    elif category == "swap":
        title = "Swap pressure is elevated"
        summary = "Swap use crossed the configured threshold and may indicate sustained memory pressure."
        next_steps = [
            "Compare RAM availability with active workloads.",
            "Check whether swap use is stable or continuing to grow.",
        ]
    elif category == "pressure":
        title = "Tasks are stalling on a saturated resource"
        summary = (
            f"{resource} kept tasks stalled for the reported share of the last "
            "minute (kernel PSI), even if utilization figures still look normal."
        )
        next_steps = [
            "Identify the heaviest consumers of the stalled resource on this node.",
            "For memory pressure, check reclaim and swap activity; for I/O, "
            "check checkpoint or dataset traffic on the busiest device.",
        ]
    elif category == "memory":
        title = "Memory pressure is elevated"
        summary = "RAM use crossed the configured threshold."
        next_steps = [
            "Review the largest workloads on this node.",
            "Check whether available memory is recovering between samples.",
        ]
    elif category == "cpu":
        title = "CPU utilization is elevated"
        summary = "CPU utilization crossed the configured threshold."
        next_steps = [
            "Confirm whether data loading or preprocessing is limiting GPU work.",
            "Compare CPU load with GPU utilization over the same interval.",
        ]
    elif category.startswith("gpu_"):
        gpus = server.get("gpus", []) if isinstance(server, dict) else []
        identity = str(condition.get("conditionKey") or "").rsplit(":", 1)[-1]
        # Exact UUID match first; otherwise parse the complete index out of
        # the resource label, so "GPU 10" can never resolve to GPU 1.
        gpu = next(
            (
                item
                for item in gpus
                if isinstance(item, dict) and str(item.get("uuid")) == identity
            ),
            None,
        )
        if gpu is None:
            index_match = _GPU_INDEX_IN_RESOURCE.search(resource)
            if index_match is not None:
                resource_index = int(index_match.group(1))
                gpu = next(
                    (
                        item
                        for item in gpus
                        if isinstance(item, dict)
                        and item.get("index") == resource_index
                    ),
                    None,
                )
        if isinstance(gpu, dict):
            index = gpu.get("index")
            target_gpu_index = index if isinstance(index, int) else None
            evidence.extend(
                (
                    {
                        "label": "gpuUtilizationPct",
                        "value": _number(gpu.get("utilization_gpu_pct")),
                        "unit": "%",
                    },
                    {
                        "label": "memoryUsedMiB",
                        "value": _number(gpu.get("memory_used_mib")),
                        "unit": "MiB",
                    },
                    {"label": "processCount", "value": len(gpu.get("processes", ()))},
                )
            )
        if category == "gpu_idle_memory":
            title = "VRAM is allocated while compute is idle"
            summary = "GPU memory remains allocated while compute utilization stays below the busy threshold."
            next_steps = [
                "Inspect the GPU process list and workload owner.",
                "Confirm whether the process is intentionally waiting or has stalled.",
            ]
        elif category == "gpu_temperature":
            title = "GPU temperature is elevated"
            summary = "GPU temperature crossed the configured warning threshold."
            next_steps = [
                "Check cooling, fan operation, and neighboring device temperatures.",
                "Inspect hardware slowdown indicators before continuing a long workload.",
            ]
        elif category in {"gpu_ecc", "gpu_memory_repair", "gpu_slowdown"}:
            title = "GPU hardware health requires attention"
            summary = "NVIDIA hardware telemetry reported a reliability condition."
            next_steps = [
                "Review the GPU health fields and preserve the workload context.",
                "Follow the hardware maintenance policy for this cluster.",
            ]
        elif category == "gpu_count":
            title = "GPU inventory differs from configuration"
            summary = "The observed GPU count does not match the expected count."
            next_steps = [
                "Confirm device visibility and driver initialization on this node.",
                "Update expected_gpu_counts only if the hardware inventory changed intentionally.",
            ]

    return {
        "title": title,
        "summary": summary,
        "evidence": [item for item in evidence if item.get("value") is not None][:8],
        "nextSteps": next_steps[:4],
        "targetGpuIndex": target_gpu_index,
    }


def sanitized_bundle(
    snapshot: dict[str, Any], incidents: dict[str, Any], host: str | None = None
) -> dict[str, object]:
    """Return an allowlisted diagnostic export without commands or identities."""
    selected = [
        server
        for server in snapshot.get("servers", [])
        if isinstance(server, dict) and (host is None or server.get("host") == host)
    ]
    aliases = {
        str(server.get("host")): f"node-{index:03d}"
        for index, server in enumerate(selected, start=1)
    }
    servers = []
    for server in selected:
        system = (
            server.get("system") if isinstance(server.get("system"), dict) else None
        )
        server_gpus = []
        for gpu in server.get("gpus", []):
            if not isinstance(gpu, dict):
                continue
            server_gpus.append(
                {
                    key: gpu.get(key)
                    for key in (
                        "index",
                        "name",
                        "temperature_c",
                        "utilization_gpu_pct",
                        "utilization_memory_pct",
                        "memory_total_mib",
                        "memory_used_mib",
                        "memory_free_mib",
                        "power_draw_w",
                        "power_limit_w",
                        "pstate",
                        "processes_available",
                        "processes_sampled",
                        "processes_observed_at",
                        "health",
                    )
                }
            )
        system_projection = None
        if system is not None:
            system_projection = {
                key: system.get(key)
                for key in (
                    "uptime_seconds",
                    "load_1m",
                    "load_5m",
                    "load_15m",
                    "cpu_cores",
                    "cpu_usage_pct",
                    "memory_total_mib",
                    "memory_used_mib",
                    "memory_available_mib",
                    "swap_total_mib",
                    "swap_used_mib",
                    "disk_total_mib",
                    "disk_used_mib",
                    "network_rx_bps",
                    "network_tx_bps",
                    "disk_read_bps",
                    "disk_write_bps",
                )
            }
            system_projection["diskUsagePct"] = [
                disk.get("used_pct")
                for disk in system.get("disks", [])
                if isinstance(disk, dict)
            ][:64]
        servers.append(
            {
                "node": aliases[str(server.get("host"))],
                "status": server.get("status"),
                "stale": server.get("stale"),
                "polling": server.get("polling"),
                "latencyMs": server.get("latencyMs"),
                "consecutiveFailures": server.get("consecutiveFailures"),
                "lastAttemptAt": server.get("lastAttemptAt"),
                "lastSuccessAt": server.get("lastSuccessAt"),
                "system": system_projection,
                "gpus": server_gpus,
            }
        )

    active = []
    for condition in incidents.get("active", []):
        if not isinstance(condition, dict) or str(condition.get("host")) not in aliases:
            continue
        active.append(
            {
                "node": aliases[str(condition.get("host"))],
                "category": condition.get("category"),
                "severity": condition.get("severity"),
                "value": condition.get("value"),
                "threshold": condition.get("threshold"),
                "firstObservedAt": condition.get("firstObservedAt"),
                "lastObservedAt": condition.get("lastObservedAt"),
                "action": condition.get("action"),
            }
        )
    persistence = snapshot.get("persistence", {})
    notifications = snapshot.get("notifications", {})
    notification_error_codes = sorted(
        {
            code
            for endpoint in notifications.get("endpoints", ())
            if isinstance(endpoint, dict)
            and isinstance((code := endpoint.get("lastError")), str)
        }
    )
    return {
        "schemaVersion": 1,
        "generatedAt": snapshot.get("generatedAt"),
        "appVersion": snapshot.get("appVersion"),
        "collection": {
            "pollIntervalSeconds": snapshot.get("pollIntervalSeconds"),
            "collectionStaleAfterSeconds": snapshot.get("collectionStaleAfterSeconds"),
            "lastPollCompletedAt": snapshot.get("lastPollCompletedAt"),
            "lastPollDurationMs": snapshot.get("lastPollDurationMs"),
        },
        "persistence": {
            key: persistence.get(key)
            for key in (
                "enabled",
                "backend",
                "healthy",
                "queuedWrites",
                "droppedWrites",
            )
        },
        "notifications": {
            key: notifications.get(key)
            for key in ("enabled", "healthy", "queuedDeliveries", "droppedDeliveries")
        }
        | {"errorCodes": notification_error_codes},
        "stats": snapshot.get("stats"),
        "servers": servers,
        "activeIncidents": active,
        "redaction": {
            "hostAliases": True,
            "sshErrors": True,
            "gpuUuids": True,
            "processes": True,
            "workloadIdentity": True,
            "filesystemPaths": True,
            "configuration": True,
        },
    }
