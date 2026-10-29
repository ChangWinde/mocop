"""Execution phases from explicit task and fresh, complete replica observations."""

import time

TERMINAL = {
    "SUCCEEDED",
    "FAILED",
    "STOPPED",
    "CANCELED",
    "CANCELLED",
    "TERMINATED",
    "COMPLETED",
    "SUCCESS",
    "STARTUP_FAILED",
}
STARTING = {
    "STARTING",
    "INITIALIZING",
    "PROVISIONING",
    "CONTAINERCREATING",
    "PODINITIALIZING",
}
QUEUED = {"CREATED", "PENDING", "QUEUED", "SCHEDULING"}
START_ERRORS = {
    "IMAGEPULLBACKOFF",
    "ERRIMAGEPULL",
    "INVALIDIMAGENAME",
    "CREATECONTAINERCONFIGERROR",
    "CREATECONTAINERERROR",
    "CONTAINERCANNOTRUN",
    "DEPLOYMENTFAILED",
    "PROVISIONINGFAILED",
    "STARTERROR",
    "STARTFAILED",
    "SCHEDULINGFAILED",
}


def normalized(value):
    return (
        str(value or "UNKNOWN")
        .upper()
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
    )


def execution(job, now=None):
    now = time.time() * 1000 if now is None else now
    status = job.get("status", "UNKNOWN")
    source = "platform"
    if status not in TERMINAL:
        observed = job.get("browser_status_sampled_at", 0)
        if 0 <= now - observed <= 120000 and job.get("browser_execution_status"):
            status = job["browser_execution_status"]
            source = "browser"
    # A ledger terminal state wins over replicas from an earlier execution phase.
    if status not in TERMINAL and status != "STOPPING":
        replicas = job.get("replicas") or {}
        rows = replicas.get("replicas") or []
        fresh = 0 <= now - (replicas.get("fetched_at") or 0) <= 120000
        complete = replicas.get("complete") and replicas.get("total") == len(rows)
        if job.get("replicas_status") == 200 and fresh and complete and rows:
            phases = set()
            for row in rows:
                state = normalized(row.get("status"))
                secondary = normalized(row.get("secondary_status"))
                if secondary in START_ERRORS or state in START_ERRORS:
                    phases.add("STARTUP_FAILED")
                elif state in {"FAILED", "ERROR"}:
                    phases.add(
                        "STARTUP_FAILED" if status in STARTING | QUEUED else "FAILED"
                    )
                elif state in STARTING or secondary in STARTING:
                    phases.add("STARTING")
                elif state in QUEUED:
                    phases.add("QUEUED")
                elif state == "RUNNING":
                    phases.add("RUNNING")
                else:
                    phases.add("UNKNOWN")
            status = next(iter(phases)) if len(phases) == 1 else "MIXED"
            source = "replicas"
    if status in STARTING:
        status = "STARTING"
    elif status in QUEUED:
        status = "QUEUED"
    # Zero run time must be explicit; absent timing never proves a startup failure.
    if (
        status == "FAILED"
        and type(job.get("runtime_seconds")) in (int, float)
        and job["runtime_seconds"] == 0
        and not job.get("started_at")
    ):
        status = "STARTUP_FAILED"
    return {"status": status, "source": source}
