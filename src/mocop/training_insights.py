"""Small, explicit projections for training utilization and platform accounting."""

import math
import time

from .training_execution import TERMINAL, execution


def finite(value):
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def utilization(job, now=None):
    now = time.time() * 1000 if now is None else now
    metrics = job.get("metrics") or {}
    observations = []
    if metrics.get("source") == "provider":
        for series in metrics.get("series", []):
            if series.get("metric") != "GpuUsage":
                continue
            points = [p for p in series.get("points", []) if finite(p.get("ts"))]
            if points:
                point = max(points, key=lambda p: p["ts"])
                observations.append(
                    {
                        "device": series.get("replica_id"),
                        "value": point.get("value"),
                        "ts": point["ts"],
                        "scope": "replica_mean",
                    }
                )
    else:
        samples = [p for p in metrics.get("samples", []) if finite(p.get("ts"))]
        if samples:
            sample = max(samples, key=lambda p: p["ts"])
            observations = [
                {
                    "device": str(g.get("index")),
                    "value": g.get("util_pct"),
                    "ts": sample["ts"],
                    "scope": "device",
                }
                for g in sample.get("gpus", [])
            ]
    valid = [
        p
        for p in observations
        if finite(p.get("value"))
        and 0 <= p["value"] <= 100
        and -60000 <= now - p["ts"] <= 180000
    ]
    return {
        "pct": sum(p["value"] for p in valid) / len(valid) if valid else None,
        "observed": len(valid),
        "reported": len(observations),
        "sampled_at": min((p["ts"] for p in valid), default=None),
        "scope": "replica_mean" if metrics.get("source") == "provider" else "device",
        "partial": len(valid) != len(observations),
        "observations": observations[:128],
    }


def capacity_projection(data):
    rows = []
    seen = set()
    for offering in data.get("offerings", [])[:256]:
        model = offering.get("gpu_model")
        if not isinstance(model, str) or not model.strip():
            continue
        for zone, availability in list(
            offering.get("availability_by_resource_zone", {}).items()
        )[:64]:
            for key, kind in (
                ("job_capacity", "training"),
                ("capacity", "development"),
            ):
                cap = availability.get(key) or {}
                signature = (
                    zone,
                    model,
                    kind,
                    cap.get("total_gpus"),
                    cap.get("free_gpus"),
                    cap.get("selection"),
                )
                if signature in seen:
                    continue
                seen.add(signature)
                total, free = cap.get("total_gpus"), cap.get("free_gpus")
                valid = (
                    all(finite(n) and n >= 0 and int(n) == n for n in (total, free))
                    and free <= total
                )

                starting, running = [
                    int(value)
                    if valid
                    and finite(value)
                    and 0 <= value <= total - free
                    and int(value) == value
                    else None
                    for value in (cap.get("starting_gpus"), cap.get("running_gpus"))
                ]
                if (
                    starting is not None
                    and running is not None
                    and starting + running > total - free
                ):
                    starting = running = None
                rows.append(
                    {
                        "zone": str(zone)[:64],
                        "vendor": str(availability.get("vendor", ""))[:64],
                        "model": model[:120],
                        "kind": kind,
                        "total": int(total) if valid else None,
                        "free": int(free) if valid else None,
                        "starting": starting,
                        "running": running,
                        "allocated": int(total - free) if valid else None,
                        "sampled_at": cap.get("observed_at")
                        if finite(cap.get("observed_at"))
                        else None,
                        "selection": str(cap.get("selection", "unknown"))[:64],
                        "candidate_count": cap.get("candidate_count"),
                        "status": str(cap.get("status", "unknown"))[:64],
                    }
                )
    return {
        "rows": rows[:512],
        "scope": "authorized_offering_candidates",
        "additive": False,
    }


def card_hours_projection(data):
    personal = data.get("personal")
    keys = (
        "raw_used_card_hours",
        "used_card_hours",
        "used_weighted_card_hours",
        "remaining_card_hours",
        "effective_allowance_card_hours",
        "current_period_start_at",
        "current_period_end_at",
    )
    return {
        "personal": {
            k: personal.get(k) if finite(personal.get(k)) else None for k in keys
        }
        if isinstance(personal, dict)
        else None,
        "cycle_type": str(personal.get("cycle_type", ""))[:32]
        if isinstance(personal, dict)
        else None,
        "timezone": str(personal.get("timezone", ""))[:64]
        if isinstance(personal, dict)
        else None,
        "scope": "platform_personal_allowance",
        "source": "platform_ledger",
    }


def overview(jobs, now=None):
    now = time.time() * 1000 if now is None else now
    groups = {}
    for job in jobs:
        phase = execution(job, now)["status"]
        if phase == "STARTUP_FAILED":
            timestamp = (
                job.get("ended_at") or job.get("updated_at") or job.get("created_at")
            )
            # Active ledger failures stay visible; terminal failures use a 24-hour window.
            if job.get("status") in TERMINAL and (
                not finite(timestamp) or not 0 <= now - timestamp <= 86400000
            ):
                continue
        groups.setdefault(phase, []).append(job)

    def summary(rows):
        known = [
            j
            for j in rows
            if type(j.get("gpu_count")) is int and 0 <= j["gpu_count"] <= 16384
        ]
        models = {}
        for job in known:
            model = job.get("gpu_model") or "型号未提供"
            models[model] = models.get(model, 0) + job["gpu_count"]
        return {
            "jobs": len(rows),
            "cards": sum(j["gpu_count"] for j in known),
            "unknown_card_jobs": len(rows) - len(known),
            "models": models,
            "cached_spec_jobs": sum(j.get("gpu_spec_stale") is True for j in known),
        }

    states = {
        key.lower(): summary(groups.get(key, []))
        for key in (
            "RUNNING",
            "STARTING",
            "STARTUP_FAILED",
            "QUEUED",
            "MIXED",
            "UNKNOWN",
        )
    }
    running = states["running"]
    active = [
        j for phase, rows in groups.items() if phase not in TERMINAL for j in rows
    ]
    active_summary = summary(active)
    return {
        "states": states,
        "startup_failure_window_hours": 24,
        "running_jobs": running["jobs"],
        "known_running_cards": running["cards"],
        "unknown_card_jobs": running["unknown_card_jobs"],
        "models": running["models"],
        "known_active_cards": active_summary["cards"],
        "unknown_active_card_jobs": active_summary["unknown_card_jobs"],
        "known_starting_cards": states["starting"]["cards"],
        "known_queued_cards": states["queued"]["cards"],
        "utilization_jobs": sum(
            utilization(j, now)["pct"] is not None for j in groups.get("RUNNING", [])
        ),
    }
