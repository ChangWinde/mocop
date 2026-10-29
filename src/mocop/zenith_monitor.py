from __future__ import annotations

import contextlib
import copy
import json
import math
import re
import stat
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .api_manifest import QUERY_SCHEMAS
from .api_schema import QueryError, parse_query
from .platform_origin import ORIGIN
from .training_browser import browser_status, merge_browser_observations
from .training_execution import execution
from .training_insights import (
    capacity_projection,
    card_hours_projection,
    overview,
    utilization,
)
from .training_resources import gpu_resources

TOKEN_PATH = Path.home() / ".config/mocop/zenith/token"
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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def number(value):
    try:
        return (
            value
            if isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            else None
        )
    except OverflowError:
        return None


class TrainingMonitor:
    def __init__(self, token_path=TOKEN_PATH, *, start=True):
        self.token_path = Path(token_path)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.requests = deque()
        self.cooldown_until = 0.0
        self.details = OrderedDict()
        self.detail_lock = threading.Lock()
        self.jobs = []
        self.total = 0
        self.complete = False
        self.sampled_at = None
        self.attempted_at = None
        self.error_status = None
        self.failure_streak = 0
        self.thread = None
        self.list_collected_at = 0
        self.list_sampled_at = None
        self.platform_collected_at = float("-inf")
        self.platform = {}
        if start:
            self.thread = threading.Thread(
                target=self.run, name="zenith-training-monitor", daemon=True
            )
            self.thread.start()

    def token(self):
        try:
            info = self.token_path.stat()
            if (
                info.st_uid != __import__("os").getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ReadError(401)
            token = self.token_path.read_text().strip()
            if not re.fullmatch(r"znt_[A-Za-z0-9]{20,200}", token):
                raise ReadError(401)
            return token
        except (OSError, UnicodeError):
            raise ReadError(401) from None

    def text(self, value, limit=512):
        if not isinstance(value, str):
            return None
        value = value[:limit]
        with contextlib.suppress(ReadError):
            value = value.replace(self.token(), "[REDACTED]")
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
        return value

    def read(self, path):
        if not path.startswith("/pt/api/ai/jobs") and path not in {
            "/pt/api/me/card-hours",
            "/pt/api/compute-offerings",
        }:
            raise ReadError(400)
        token = self.token()
        if ORIGIN is None:
            raise ReadError(503)
        now = time.monotonic()
        with self.lock:
            while self.requests and self.requests[0] <= now - 60:
                self.requests.popleft()
            if now < self.cooldown_until:
                raise ReadError(429, self.cooldown_until - now)
            if len(self.requests) >= 60:
                raise ReadError(429, max(1, 60 - (now - self.requests[0])))
            self.requests.append(now)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        req = Request(
            ORIGIN + path,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        )
        try:
            with opener.open(req, timeout=10) as response:
                payload = response.read(2_000_001)
                if len(payload) > 2_000_000:
                    raise ReadError(502)
                result = json.loads(payload, parse_constant=lambda _: None)
                if not isinstance(result, dict):
                    raise ReadError(502)
                return result
        except HTTPError as error:
            retry = 0
            if error.code == 429:
                try:
                    retry = min(
                        600, max(30, float(error.headers.get("Retry-After", "30")))
                    )
                except ValueError:
                    retry = 30
                with self.lock:
                    self.cooldown_until = time.monotonic() + retry
            raise ReadError(error.code, retry) from None
        except (URLError, TimeoutError, OSError, ValueError, RecursionError):
            raise ReadError(503) from None

    def job(self, value):
        if (
            not isinstance(value, dict)
            or not ID.fullmatch(str(value.get("id", "")))
            or value.get("vendor") not in VENDORS
        ):
            raise ReadError(502)
        result = {
            k: self.text(value.get(k))
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
        count, model, source = gpu_resources(value)
        result["gpu_count"], result["gpu_count_source"] = count, source
        result["gpu_model"] = self.text(model, 120)
        runtime = number(value.get("runtime_seconds"))
        result["runtime_seconds"] = (
            runtime if runtime is not None and 0 <= runtime <= 3_153_600_000 else None
        )
        caps = value.get("capabilities") or {}
        if not isinstance(caps, dict):
            raise ReadError(502)
        result["capabilities"] = {
            key: caps.get(key) is True for key in ("logs", "replicas")
        }
        result["worker_status"] = self.text(caps.get("worker_status"), 80)
        return result

    def replicas(self, job_id):
        data = self.read(
            f"/pt/api/ai/jobs/by-id/{job_id}/replicas?page=1&page_size=100"
        )
        rows = data.get("replicas")
        if not isinstance(rows, list):
            raise ReadError(502)
        result = []
        for row in rows[:100]:
            if not isinstance(row, dict):
                raise ReadError(502)
            item = {
                key: self.text(row.get(key), 1000)
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

    def metrics(self, job_id):
        data = self.read(f"/pt/api/ai/jobs/by-id/{job_id}/metrics?minutes=30")
        if data.get("source") == "provider":
            series = data.get("series")
            if not isinstance(series, list):
                raise ReadError(502)
            result = []
            for row in series[:128]:
                if not isinstance(row, dict):
                    raise ReadError(502)
                if row.get("metric") not in {
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
                        "replica_id": self.text(row.get("replica_id")),
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
        return {
            "source": "stored",
            "replica": self.text(data.get("replica")),
            "replicas": [self.text(r) for r in data.get("replicas", [])[:100]],
            "collection_enabled": data.get("collection_enabled") is True,
            "samples": result,
            "fetched_at": time.time() * 1000,
        }

    def observations(self, job):
        result = {"id": job["id"]}
        for kind in ("replicas", "metrics"):
            if self.stop.is_set():
                break
            try:
                cached = job.get(kind)
                maximum_age = 55000
                if (
                    cached
                    and time.time() * 1000 - (cached.get("fetched_at") or 0)
                    < maximum_age
                ):
                    continue
                if kind == "replicas" and not job["capabilities"]["replicas"]:
                    result[kind + "_status"] = 501
                    continue
                result[kind] = getattr(self, kind)(job["id"])
                result[kind + "_status"] = 200
            except ReadError as error:
                if error.status in (401, 403):
                    raise
                result[kind + "_status"] = error.status
        return result

    def collect_platform(self):
        started = time.monotonic()
        if started - self.platform_collected_at < 60:
            return
        retry = 60
        for name, endpoint, projector in (
            ("card_hours", "/pt/api/me/card-hours", card_hours_projection),
            ("capacity", "/pt/api/compute-offerings", capacity_projection),
        ):
            try:
                result = {
                    **projector(self.read(endpoint)),
                    "fetched_at": time.time() * 1000,
                    "error_status": None,
                }
            except ReadError as error:
                if error.status == 401:
                    raise
                with self.lock:
                    result = {
                        **self.platform.get(name, {}),
                        "error_status": error.status,
                    }
                if error.status not in (401, 403):
                    retry = min(retry, max(30, error.retry_after))
            with self.lock:
                self.platform[name] = result
        self.platform_collected_at = started - 60 + retry

    def collect(self):
        started = time.monotonic()
        with self.lock:
            self.attempted_at = time.time() * 1000
        self.collect_platform()
        refresh_list = not self.jobs or started - self.list_collected_at >= 60
        if refresh_list:
            rows = []
            complete = False
            total = 0
            for page in range(1, 21):
                if self.stop.is_set():
                    return
                data = self.read(f"/pt/api/ai/jobs?page={page}&page_size=100")
                if (
                    not isinstance(data.get("jobs"), list)
                    or number(data.get("total")) is None
                ):
                    raise ReadError(502)
                total = int(data["total"])
                rows.extend(self.job(row) for row in data["jobs"][:100])
                if len(rows) >= total or len(data["jobs"]) < 100:
                    complete = len(rows) >= total
                    break
            self.list_collected_at = started
            list_sampled_at = time.time() * 1000
        else:
            with self.lock:
                rows, complete, total = (
                    copy.deepcopy(self.jobs),
                    self.complete,
                    self.total,
                )
                list_sampled_at = self.list_sampled_at
        rows = list({row["id"]: row for row in rows}.values())
        complete = complete and len(rows) >= total
        rows.sort(key=lambda j: j["created_at"] or 0, reverse=True)
        with self.lock:
            previous = {row["id"]: row for row in self.jobs}
        active = [job for job in rows if job["status"] not in TERMINAL]
        active.sort(
            key=lambda j: previous.get(j["id"], {}).get("metrics", {}).get("fetched_at")
            or 0
        )
        active = active[:8]
        for job in active:
            old = previous.get(job["id"], {})
            for kind in ("metrics", "replicas"):
                if kind in old:
                    job[kind] = old[kind]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.observations, active))
        observations = {r.pop("id"): r for r in results}
        for row in rows:
            old = previous.get(row["id"], {})
            for kind in ("replicas", "metrics"):
                if kind in old:
                    row[kind] = old[kind]
                if kind + "_status" in old:
                    row[kind + "_status"] = old[kind + "_status"]
            row.update(observations.get(row["id"], {}))
        rows = merge_browser_observations(rows)
        for row in rows:
            row["gpu_utilization"] = utilization(row)
        with self.lock:
            self.jobs, self.total, self.complete = rows, total, complete
            self.list_sampled_at = list_sampled_at
            self.sampled_at = time.time() * 1000
            self.error_status = None
            self.failure_streak = 0

    def run(self):
        while not self.stop.is_set():
            cycle_started = time.monotonic()
            retry = 30
            try:
                self.collect()
            except ReadError as error:
                with self.lock:
                    self.error_status = error.status
                    self.failure_streak += 1
                    if error.status in (401, 403):
                        self.jobs = []
                        self.platform = {}
                        self.list_collected_at = 0
                        self.details.clear()
                        self.total = 0
                        self.complete = False
                        self.sampled_at = None
                    retry = max(
                        error.retry_after,
                        min(300, 30 * 2 ** min(self.failure_streak - 1, 4)),
                    )
            except Exception:
                with self.lock:
                    self.error_status = 503
                    self.failure_streak += 1
                retry = 60
            self.stop.wait(max(1, retry - (time.monotonic() - cycle_started)))

    def snapshot(self):
        with self.lock:
            now = time.time() * 1000
            jobs = [{**job, "execution": execution(job, now)} for job in self.jobs]
            return copy.deepcopy(
                {
                    "source": "Zenith",
                    "enabled": True,
                    "poll_interval_seconds": 30,
                    "sampled_at": self.sampled_at,
                    "attempted_at": self.attempted_at,
                    "stale": self.error_status is not None
                    or self.sampled_at is None
                    or time.time() * 1000 - self.sampled_at > 120_000,
                    "error_status": self.error_status,
                    "total": self.total,
                    "complete": self.complete,
                    "enriched_active_limit": 8,
                    "list_poll_interval_seconds": 60,
                    "platform_poll_interval_seconds": 60,
                    "list_sampled_at": self.list_sampled_at,
                    "overview": overview(jobs, now),
                    "browser": browser_status(),
                    "platform": self.platform,
                    "jobs": jobs,
                }
            )

    def detail(self, job_id):
        if not ID.fullmatch(job_id):
            raise ReadError(400)
        with self.lock:
            job = next((copy.deepcopy(j) for j in self.jobs if j["id"] == job_id), None)
            if job is None:
                raise ReadError(404)
        if not self.detail_lock.acquire(blocking=False):
            raise ReadError(429, 2)
        try:
            # Revalidate ownership/scopes even for cached logs, so revoked access is not replayed.
            try:
                actual = merge_browser_observations(
                    [self.job(self.read(f"/pt/api/ai/jobs/by-id/{job_id}").get("job"))]
                )[0]
                if actual["id"] != job_id:
                    raise ReadError(502)
            except ReadError as error:
                if error.status in (401, 403, 404):
                    with self.lock:
                        self.details.pop(job_id, None)
                raise
            with self.lock:
                cached = self.details.get(job_id)
                if cached and time.monotonic() - cached[0] < 30:
                    self.details.move_to_end(job_id)
                    result = copy.deepcopy(cached[1])
                    actual["execution"] = execution(
                        {**actual, **{k: v for k, v in result.items() if k != "job"}}
                    )
                    result["job"] = actual
                    return result
            result = {"job": actual, "fetched_at": time.time() * 1000}
            for kind in ("replicas", "metrics", "logs"):
                try:
                    if (
                        kind in ("replicas", "logs")
                        and not actual["capabilities"][kind]
                    ):
                        result[kind + "_status"] = 501
                        continue
                    if kind == "logs":
                        data = self.read(
                            f"/pt/api/ai/jobs/by-id/{job_id}/logs?log_num=100"
                        )
                        if not isinstance(data.get("lines"), list):
                            raise ReadError(502)
                        result[kind] = {
                            "lines": [
                                {
                                    "body": self.text(line.get("body"), 4000),
                                    "source": self.text(line.get("source")),
                                    "timestamp_us": number(line.get("timestamp_us")),
                                }
                                for line in data["lines"][:100]
                                if isinstance(line, dict)
                            ],
                            "pending": data.get("pending") is True,
                            "truncated": data.get("truncated") is True,
                            "returned": number(data.get("returned")),
                        }
                    elif (
                        kind in job
                        and job.get(kind + "_status") == 200
                        and time.time() * 1000 - (job[kind].get("fetched_at") or 0)
                        < 45_000
                    ):
                        result[kind] = job[kind]
                    else:
                        result[kind] = getattr(self, kind)(job_id)
                    result[kind + "_status"] = 200
                except ReadError as error:
                    if error.status in (401, 403, 404):
                        with self.lock:
                            self.details.pop(job_id, None)
                        raise
                    result[kind + "_status"] = error.status
            actual["execution"] = execution(
                {**actual, **{k: v for k, v in result.items() if k != "job"}}
            )
            with self.lock:
                self.details[job_id] = (time.monotonic(), copy.deepcopy(result))
                while len(self.details) > 32:
                    self.details.popitem(last=False)
            return result
        finally:
            self.detail_lock.release()


def handle_read(handler, request_url):
    path = request_url.path
    if path not in {"/api/training-jobs", "/api/training-job-detail"}:
        return False
    monitor = handler.monitor_server.training_monitor
    try:
        if path == "/api/training-jobs":
            if request_url.query:
                raise ReadError(400)
            handler._send_json(monitor.snapshot())
        else:
            values = parse_query(QUERY_SCHEMAS[path], request_url.query)
            handler._send_json(monitor.detail(values["id"]))
    except QueryError as error:
        handler._send_error(str(error), 400, code=error.code)
    except (ReadError, ValueError) as error:
        status = error.status if isinstance(error, ReadError) else 400
        handler._send_json(
            {
                "error": "training observation unavailable",
                "code": {
                    400: "TRAINING_INVALID_ID",
                    401: "TRAINING_AUTH_REQUIRED",
                    403: "TRAINING_ACCESS_DENIED",
                    404: "TRAINING_NOT_FOUND",
                    429: "TRAINING_RATE_LIMITED",
                }.get(status, "TRAINING_READ_FAILED"),
                "status": status,
            },
            status if status in (400, 401, 403, 404, 429) else 503,
        )
    return True
