"""Situation brief: the operator's morning scan as one document.

``GET /api/brief`` and ``mocop brief`` answer, in reading order, what needs a
person now, what changed over the window, where free capacity is, and who is
holding GPUs idle: the questions an operator otherwise answers by reading the
attention panel, the incident log, the heatmap, and the usage report in turn.
Every number is taken from a projection the dashboard already shows (the
snapshot, the incident feed, the capacity matcher, the usage and utilization
reports), so the brief cannot disagree with the screen; what it adds is the
ordering and the two judgments people make by eye: which conditions keep
coming back, and whose reservation sits idle.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .capacity import ANY_MODEL, CapacityRequest, match_capacity
from .models import epoch_seconds, utc_now

if TYPE_CHECKING:
    from .service import StateStore

ITEM_LIMIT = 10
"""Attention items, recurring conditions, and owners listed in full; the
counts beside them cover the rest."""

RECURRING_OPENINGS = 3
"""Openings within the window after which a condition counts as recurring:
two can be one bad hour, three is a pattern worth a root cause."""

IDLE_HEAVY_SHARE = 0.5
IDLE_HEAVY_GPU_HOURS = 8.0
"""An owner whose reservation over the window sat idle at least half the time,
for at least a working day of GPU-hours, is worth a conversation."""

CAPACITY_HOST_LIMIT = 5

EVENT_FEED_LIMIT = 5_000
"""The configuration ceiling of ``incident_history_points``: asking for this
many events returns the whole ring, whatever size it was given."""


def brief_from_state(state: StateStore, window_hours: int) -> dict[str, object]:
    """Assemble the brief from the store exactly as the dashboard would see it."""
    snapshot = state.snapshot_view()
    incidents = state.incidents(EVENT_FEED_LIMIT)
    thresholds = snapshot["thresholds"]
    assert isinstance(thresholds, dict)
    capacity = match_capacity(
        snapshot["servers"],  # type: ignore[arg-type]
        incidents["active"],  # type: ignore[arg-type]
        CapacityRequest(1, 0, ANY_MODEL),
        busy_pct=float(thresholds["gpu_busy_pct"]),
        temperature_c=float(thresholds["gpu_temperature_warning_c"]),
    )
    return build_brief(
        snapshot,
        incidents,
        capacity=capacity,
        usage=(
            state.usage_report(window_hours, ITEM_LIMIT)
            or state.usage(window_hours, ITEM_LIMIT)
        ),
        utilization=state.utilization_report(window_hours, None),
        window_hours=window_hours,
        generated_at=utc_now(),
    )


def build_brief(
    snapshot: dict[str, Any],
    incidents: dict[str, Any],
    *,
    capacity: dict[str, Any],
    usage: dict[str, Any],
    utilization: dict[str, Any] | None,
    window_hours: int,
    generated_at: str,
) -> dict[str, object]:
    """Compose the brief from projections the dashboard already serves.

    ``incidents`` is the decorated feed (``StateStore.incidents``), whose
    ``active`` list is already ordered actionable-first and critical-first;
    ``capacity`` is the matcher's answer for one GPU of any model; ``usage``
    is the history report, or the in-memory timeline without a history
    database; ``utilization`` is the history report or ``None`` without one.
    """
    since_at = _shift(generated_at, -window_hours * 3600)
    stats = snapshot["stats"]
    servers = snapshot["servers"]
    return {
        "generatedAt": generated_at,
        "sinceAt": since_at,
        "windowHours": window_hours,
        "status": fleet_status(stats),
        "fleet": _fleet(stats, servers),
        "attention": _attention(incidents["active"], incidents["correlations"]),
        "changes": _changes(
            incidents["events"], int(incidents["eventCapacity"]), since_at
        ),
        "capacity": _capacity(capacity),
        "usage": _usage(usage, utilization),
        "maintenance": _maintenance(servers),
    }


def fleet_status(stats: dict[str, Any]) -> str:
    """The summary card's badge, decided by the same rule the dashboard uses."""
    if int(stats["servers"]) == 0:
        return "unconfigured"
    if int(stats["actionableCriticalIncidents"]) > 0:
        return "critical"
    if int(stats["actionableIssueServers"]) > 0:
        return "attention"
    if int(stats["maintenanceServers"]) > 0:
        return "maintenance"
    return "healthy"


def _fleet(stats: dict[str, Any], servers: list[dict[str, Any]]) -> dict[str, object]:
    memory_total = float(stats["memoryTotalMiB"])
    return {
        "hosts": stats["servers"],
        "online": stats["onlineServers"],
        "offline": sorted(
            str(server["host"]) for server in servers if server["status"] != "online"
        ),
        "stale": stats["staleServers"],
        "maintenance": stats["maintenanceServers"],
        "gpus": stats["gpus"],
        "busyGpus": stats["busyGpus"],
        "idleGpus": int(stats["gpus"]) - int(stats["busyGpus"]),
        "gpuMemoryUsedPct": (
            round(float(stats["memoryUsedMiB"]) / memory_total * 100, 1)
            if memory_total > 0
            else None
        ),
    }


def _attention(
    active: list[dict[str, Any]], correlations: list[dict[str, Any]]
) -> dict[str, object]:
    actionable = [item for item in active if item["actionable"]]
    entries = _attention_entries(actionable)
    return {
        "active": len(active),
        "critical": sum(item["severity"] == "critical" for item in active),
        "actionable": len(actionable),
        "actionableCritical": sum(
            item["severity"] == "critical" for item in actionable
        ),
        "silenced": len(active) - len(actionable),
        "hosts": len({str(item["host"]) for item in actionable}),
        "byCategory": dict(
            Counter(str(item["category"]) for item in actionable).most_common()
        ),
        "correlations": [
            {
                "kind": correlation["kind"],
                "anchor": correlation["anchor"],
                "hosts": list(correlation["hosts"]),
                "detail": correlation["detail"],
            }
            for correlation in correlations
        ],
        "entries": len(entries),
        "items": entries[:ITEM_LIMIT],
    }


def _attention_entries(actionable: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per condition, except that conditions sharing a ``groupKey``
    (one network filesystem mounted on several hosts) collapse into a single
    entry, as they do on the dashboard: the export is full once, not five
    times. A group keeps its worst reading and its earliest opening and takes
    the position of its first member, so the actionable-first, critical-first
    order of the feed still holds."""
    entries: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    for item in actionable:
        entry = _attention_item(item)
        key = item.get("groupKey")
        group = groups.get(key) if key is not None else None
        if group is None:
            entries.append(entry)
            if key is not None:
                groups[key] = entry
            continue
        group["hosts"] = sorted({*group["hosts"], str(item["host"])})
        group["conditionKey"] = None
        group["detail"] = None
        group["belowThreshold"] = group["belowThreshold"] and entry["belowThreshold"]
        value = item["value"]
        if isinstance(value, int | float) and (
            group["value"] is None or value > group["value"]
        ):
            group["value"] = value
        group["firstObservedAt"] = min(
            str(group["firstObservedAt"]), str(item["firstObservedAt"])
        )
    return entries


def _attention_item(item: dict[str, Any]) -> dict[str, Any]:
    diagnosis = item.get("diagnosis")
    return {
        "hosts": [item["host"]],
        "conditionKey": item["conditionKey"],
        "groupKey": item.get("groupKey"),
        "category": item["category"],
        "resource": item["resource"],
        "severity": item["severity"],
        "value": item["value"],
        "threshold": item["threshold"],
        "detail": item["detail"],
        "belowThreshold": bool(item.get("belowThreshold", False)),
        "firstObservedAt": item["firstObservedAt"],
        "title": diagnosis["title"] if isinstance(diagnosis, dict) else None,
    }


def _changes(
    events: list[dict[str, Any]], event_capacity: int, since_at: str
) -> dict[str, object]:
    """Openings and recoveries inside the window.

    The feed is a bounded ring, so on a busy fleet it may not reach back the
    whole window; ``coveredFromAt`` says how far it did, exactly as the usage
    report does, rather than presenting a partial count as the total. The ring
    covers the window when it still has room or when its oldest entry predates
    the window.
    """
    since = epoch_seconds(since_at) or 0.0
    inside = [
        event
        for event in events
        if (epoch_seconds(event["observedAt"]) or 0.0) >= since
    ]
    covered_from = since_at
    if events and len(events) >= event_capacity and len(inside) == len(events):
        covered_from = min(str(event["observedAt"]) for event in events)
    openings: Counter[tuple[str, str]] = Counter(
        (str(event["host"]), str(event["conditionKey"]))
        for event in inside
        if event["state"] == "opened"
    )
    latest = {
        (str(event["host"]), str(event["conditionKey"])): event
        for event in reversed(inside)
    }
    recurring = [
        {
            "host": host,
            "conditionKey": key,
            "category": latest[(host, key)]["category"],
            "resource": latest[(host, key)]["resource"],
            "openings": count,
            "lastState": latest[(host, key)]["state"],
        }
        for (host, key), count in openings.most_common()
        if count >= RECURRING_OPENINGS
    ]
    return {
        "coveredFromAt": covered_from,
        "opened": sum(openings.values()),
        "resolved": sum(event["state"] == "resolved" for event in inside),
        "escalated": sum(event["state"] == "escalated" for event in inside),
        "recurring": recurring[:ITEM_LIMIT],
    }


def _capacity(capacity: dict[str, Any]) -> dict[str, object]:
    candidates = sorted(
        (candidate for candidate in capacity["candidates"] if candidate["available"]),
        key=lambda candidate: (-len(candidate["available"]), str(candidate["host"])),
    )
    return {
        "idleGpus": sum(len(candidate["available"]) for candidate in candidates),
        "hosts": len(candidates),
        "excludedMaintenance": capacity["excludedMaintenance"],
        "excludedHealth": capacity["excludedHealth"],
        "topHosts": [
            {
                "host": candidate["host"],
                "model": candidate["model"],
                "available": len(candidate["available"]),
                "total": candidate["total"],
                "minFreeVramGiB": round(
                    min(float(gpu["freeVramMiB"]) for gpu in candidate["available"])
                    / 1024,
                    1,
                ),
            }
            for candidate in candidates[:CAPACITY_HOST_LIMIT]
        ],
    }


def _usage(
    usage: dict[str, Any], utilization: dict[str, Any] | None
) -> dict[str, object]:
    busy_share = None
    if utilization is not None:
        hours = utilization["fleet"]
        samples = sum(int(hour["samples"]) for hour in hours)
        if samples > 0:
            busy_share = round(
                sum(float(hour["busyShare"]) * int(hour["samples"]) for hour in hours)
                / samples
                * 100,
                1,
            )
    owners = []
    for owner in usage["owners"][:ITEM_LIMIT]:
        gpu_hours = float(owner["gpuSeconds"]) / 3600
        idle_share = float(owner["idleShare"])
        owners.append(
            {
                "owner": owner["owner"],
                "gpuHours": round(gpu_hours, 1),
                "idleSharePct": round(idle_share * 100, 1),
                "gpus": owner["gpus"],
                "hosts": len(owner["hosts"]),
                "idleHeavy": (
                    idle_share >= IDLE_HEAVY_SHARE and gpu_hours >= IDLE_HEAVY_GPU_HOURS
                ),
            }
        )
    # The in-memory timeline carries no source or coverage marker: it is the
    # fallback without a history database and covers what it has sampled.
    return {
        "source": usage.get("source", "memory"),
        "coveredFromAt": usage.get("coveredFromAt", usage["sinceAt"]),
        "busySharePct": busy_share,
        "totalGpuHours": round(float(usage["totalGpuSeconds"]) / 3600, 1),
        "owners": owners,
        "idleHeavyOwners": [owner["owner"] for owner in owners if owner["idleHeavy"]],
    }


def _maintenance(servers: list[dict[str, Any]]) -> list[dict[str, object]]:
    return [
        {
            "host": server["host"],
            "reason": server["maintenance"]["reason"],
            "until": server["maintenance"]["until"],
        }
        for server in sorted(servers, key=lambda server: str(server["host"]))
        if server.get("maintenance")
    ]


def _shift(timestamp: str, seconds: float) -> str:
    base = epoch_seconds(timestamp)
    assert base is not None, timestamp
    moment = datetime.fromtimestamp(base, tz=timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _age(brief_at: str, since: object) -> str:
    start = epoch_seconds(since)
    end = epoch_seconds(brief_at)
    if start is None or end is None:
        return "?"
    hours = max(0.0, end - start) / 3600
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def _count(value: object, noun: str) -> str:
    return f"{value} {noun}{'' if value == 1 else 's'}"


def _owner_label(owner: object) -> str:
    return "(unattributed)" if owner is None else str(owner)


def render_brief(brief: dict[str, Any]) -> str:
    """The same document as plain text: one screen, worst first."""
    fleet = brief["fleet"]
    attention = brief["attention"]
    changes = brief["changes"]
    capacity = brief["capacity"]
    usage = brief["usage"]
    lines = [
        f"mocop brief {brief['generatedAt']} · last {brief['windowHours']}h "
        f"· fleet {brief['status'].upper()}",
        (
            f"hosts {fleet['online']}/{fleet['hosts']} online"
            + (f", offline: {', '.join(fleet['offline'])}" if fleet["offline"] else "")
            + (
                f", {fleet['maintenance']} in maintenance"
                if fleet["maintenance"]
                else ""
            )
            + f" · gpus {fleet['busyGpus']}/{fleet['gpus']} busy"
            + (
                f" · vram {fleet['gpuMemoryUsedPct']:g}% used"
                if fleet["gpuMemoryUsedPct"] is not None
                else ""
            )
        ),
        "",
        (
            f"attention: {attention['actionable']} actionable "
            f"({attention['actionableCritical']} critical) on "
            f"{_count(attention['hosts'], 'host')}"
            + (f", {attention['silenced']} silenced" if attention["silenced"] else "")
        ),
    ]
    for correlation in attention["correlations"]:
        lines.append(f"  !! {correlation['detail']}")
    for item in attention["items"]:
        measure = (
            f" {item['value']:g}"
            + (f"/{item['threshold']:g}" if item["threshold"] is not None else "")
            if isinstance(item["value"], int | float)
            else ""
        )
        hosts = item["hosts"]
        where = (
            hosts[0]
            if len(hosts) == 1
            else f"{_count(len(hosts), 'host')} ({' '.join(hosts)})"
        )
        lines.append(
            f"  {'!!' if item['severity'] == 'critical' else ' !'} "
            f"{where} {item['category']} {item['resource']}{measure}"
            f" · {_age(brief['generatedAt'], item['firstObservedAt'])}"
            + (" · below threshold" if item["belowThreshold"] else "")
            + (f" · {item['detail']}" if item["detail"] else "")
        )
    hidden = attention["entries"] - len(attention["items"])
    if hidden > 0:
        lines.append(f"  … {hidden} more in /api/incidents")
    lines.append("")
    lines.append(
        f"changes since {changes['coveredFromAt']}: {changes['opened']} opened, "
        f"{changes['resolved']} resolved, {changes['escalated']} escalated"
    )
    for item in changes["recurring"]:
        lines.append(
            f"  ~ {item['host']} {item['category']} {item['resource']} opened "
            f"{item['openings']}x, now {item['lastState']}"
        )
    lines.append("")
    lines.append(
        f"capacity: {_count(capacity['idleGpus'], 'idle GPU')} on "
        f"{_count(capacity['hosts'], 'host')}"
        + (
            f" ({_count(capacity['excludedMaintenance'], 'host')} in maintenance "
            "excluded)"
            if capacity["excludedMaintenance"]
            else ""
        )
    )
    for host in capacity["topHosts"]:
        lines.append(
            f"  {host['host']}: {host['available']}/{host['total']} {host['model']}"
            f" · ≥{host['minFreeVramGiB']:g} GiB free each"
        )
    lines.append("")
    lines.append(
        f"usage ({usage['source']}): {usage['totalGpuHours']:g} GPU-hours"
        + (
            f", fleet busy {usage['busySharePct']:g}% of the window"
            if usage["busySharePct"] is not None
            else ""
        )
    )
    for owner in usage["owners"]:
        lines.append(
            f"  {'*' if owner['idleHeavy'] else ' '} "
            f"{_owner_label(owner['owner'])}: {owner['gpuHours']:g} GPU-h, "
            f"idle {owner['idleSharePct']:g}%, {_count(owner['gpus'], 'GPU')} on "
            f"{_count(owner['hosts'], 'host')}"
        )
    if usage["idleHeavyOwners"]:
        lines.append(
            "  * reservation idle at least half the window: "
            + ", ".join(_owner_label(owner) for owner in usage["idleHeavyOwners"])
        )
    if brief["maintenance"]:
        lines.append("")
        lines.append("maintenance:")
        for window in brief["maintenance"]:
            lines.append(
                f"  {window['host']} until {window['until']}: {window['reason']}"
            )
    return "\n".join(lines) + "\n"
