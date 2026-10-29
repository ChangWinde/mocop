"""Merge short-lived, non-secret projections from the user's logged-in browser."""

import copy
import json
import os
import stat
import time
from pathlib import Path

from .training_resources import gpu_resources

OBSERVATIONS = Path.home() / ".local/state/mocop/training-browser.json"


def merge_browser_observations(jobs, *, path=OBSERVATIONS, now=None):
    now = time.time() * 1000 if now is None else now
    result = copy.deepcopy(jobs)
    for job in result:
        if job.get("gpu_count_source") == "browser":
            job["gpu_count"] = job["gpu_model"] = job["gpu_count_source"] = None
        if job.pop("_browser_timing", False):
            job["started_at"] = job["runtime_seconds"] = None
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 2 * 1024**2
        ):
            return result
        data = json.loads(path.read_text())
        sampled = data["sampled_at"]
        if (
            not isinstance(sampled, float | int)
            or not 0 <= now - sampled <= 600000
            or data.get("status") != "ok"
        ):
            return result
        observations = {
            (r["vendor"], r["job_id"]): r
            for r in data["jobs"][:2000]
            if isinstance(r, dict)
            and isinstance(r.get("vendor"), str)
            and isinstance(r.get("job_id"), str)
        }
        for job in result:
            row = observations.get((job["vendor"], job["job_id"]))
            if row is None:
                continue
            count, model, _ = gpu_resources(row)
            if job.get("gpu_count") is None and count is not None:
                job["gpu_count"] = count
                job["gpu_model"] = model
                job["gpu_count_source"] = "browser"
                job["spec_sampled_at"] = sampled
            for key in ("started_at", "runtime_seconds"):
                value = row.get(key)
                if (
                    job.get(key) is None
                    and type(value) in (float, int)
                    and 0 <= value < 10**14
                ):
                    job[key] = value
                    job["_browser_timing"] = True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


def browser_status():
    path = OBSERVATIONS.with_name("training-browser-status.json")
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 4096
        ):
            return {"status": "unavailable"}
        data = json.loads(path.read_text())
        return {k: data.get(k) for k in ("status", "sampled_at", "jobs", "complete")}
    except (OSError, ValueError):
        return {"status": "not_configured"}
