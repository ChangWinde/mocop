"""Bounded workload projections that preserve unknowns and native metric units."""

from __future__ import annotations

import json
import math
import re
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
}
ID = re.compile(r"^[a-z0-9]{25}$")
VENDORS = {"baidu", "ctyun", "volcengine", "aliyun", "lanqi"}
SECRET = re.compile(
    r"znt_[A-Za-z0-9]+|Bearer\s+[A-Za-z0-9._-]+|(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    re.I,
)


class ReadError(Exception):
    def __init__(self, status: int = 503, retry_after: float = 0):
        self.status = status
        self.retry_after = retry_after


def number(value):
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def redact_text(value, limit=512):
    if not isinstance(value, str):
        return None
    value = value[:limit]
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.S,
    )
    value = SECRET.sub("[REDACTED]", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|secret[_-]?key|password)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )
    return value[:limit].encode("utf-8", errors="replace").decode("utf-8")


def project_job(value):
    if (
        not isinstance(value, dict)
        or not ID.fullmatch(str(value.get("id", "")))
        or not isinstance(value.get("vendor"), str)
        or value.get("vendor") not in VENDORS
    ):
        raise ReadError(502)
    result = {
        k: redact_text(value.get(k))
        for k in (
            "id",
            "vendor",
            "job_id",
            "name",
            "project_id",
            "status",
            "execution_class",
        )
    }
    for key in ("created_at", "updated_at", "started_at", "ended_at"):
        result[key] = number(value.get(key))
    count = value.get("gpu_count")
    result["gpu_count"] = (
        count
        if isinstance(count, int)
        and not isinstance(count, bool)
        and 0 <= count <= 16384
        else None
    )
    result["gpu_model"] = redact_text(value.get("gpu_model"), 120)
    caps = value.get("capabilities")
    if caps is None:
        caps = {}
    if not isinstance(caps, dict):
        raise ReadError(502)
    result["capabilities"] = {
        key: caps.get(key) is True for key in ("logs", "replicas")
    }
    result["worker_status"] = redact_text(caps.get("worker_status"), 80)
    return result


def project_replicas(data):
    rows = data.get("replicas")
    if not isinstance(rows, list):
        raise ReadError(502)
    total = data.get("total")
    if total is not None and (
        not isinstance(total, int) or isinstance(total, bool) or total < 0
    ):
        raise ReadError(502)
    result = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            raise ReadError(502)
        item = {
            key: redact_text(row.get(key), 1000)
            for key in (
                "instance_id",
                "name",
                "status",
                "secondary_status",
                "failure_message",
                "diagnosis_status",
                "diagnosis_message",
            )
        }
        for key in ("exit_code", "started_at", "ended_at"):
            item[key] = number(row.get(key))
        result.append(item)
    return {
        "replicas": result,
        "total": number(data.get("total")),
        "complete": number(data.get("total")) is not None
        and data["total"] <= len(result),
        "fetched_at": time.time() * 1000,
    }


def project_metrics(data):
    if data.get("source") == "provider":
        series = data.get("series")
        if not isinstance(series, list):
            raise ReadError(502)
        result = []
        for row in series[:128]:
            if not isinstance(row, dict):
                raise ReadError(502)
            if not isinstance(row.get("metric"), str) or row.get("metric") not in {
                "GpuUsage",
                "GpuMemoryUsage",
                "CpuUsage",
                "MemoryUsage",
            }:
                continue
            raw_points = row.get("points", [])
            if not isinstance(raw_points, list):
                raise ReadError(502)
            points = [
                {"ts": number(p.get("ts")), "value": number(p.get("value"))}
                for p in raw_points[-120:]
                if isinstance(p, dict)
            ]
            result.append(
                {
                    "replica_id": redact_text(row.get("replica_id")),
                    "metric": row["metric"],
                    "unit": "provider_native",
                    "points": points,
                }
            )
        return {
            "source": "provider",
            "fetched_at": number(data.get("fetched_at")),
            "series": result,
        }
    samples = data.get("samples")
    if not isinstance(samples, list):
        raise ReadError(502)
    result = []
    for row in samples[-120:]:
        if not isinstance(row, dict):
            continue
        item = {
            k: number(row.get(k))
            for k in ("ts", "cpu_pct", "mem_used_mb", "mem_total_mb")
        }
        try:
            gpus = json.loads(row.get("gpus_json") or "[]")
        except (ValueError, TypeError, RecursionError):
            gpus = []
        item["gpus"] = (
            [
                {
                    k: number(g.get(k))
                    for k in ("index", "util_pct", "mem_used_mb", "mem_total_mb")
                }
                for g in gpus[:64]
                if isinstance(g, dict)
            ]
            if isinstance(gpus, list)
            else []
        )
        result.append(item)
    replicas = data.get("replicas", [])
    if not isinstance(replicas, list):
        raise ReadError(502)
    return {
        "source": "stored",
        "replica": redact_text(data.get("replica")),
        "replicas": [redact_text(r) for r in replicas[:100]],
        "collection_enabled": data.get("collection_enabled") is True,
        "samples": result,
        "fetched_at": time.time() * 1000,
    }
