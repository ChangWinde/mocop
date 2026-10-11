"""Independent training collection and owner-revalidated diagnostic samples."""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from .training_client import TrainingClient
from .training_config import TrainingJobsConfig
from .training_projection import (
    ID,
    TERMINAL,
    ReadError,
    number,
    project_job,
    project_metrics,
    project_replicas,
    redact_text,
)


class TrainingSource(Protocol):
    def snapshot(self) -> dict[str, object]: ...
    def detail(self, job_id: str) -> dict[str, object]: ...


class TrainingMonitor:
    def __init__(self, config: TrainingJobsConfig):
        self.config = config
        self.client = TrainingClient(config)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
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

    def start(self):
        if not self.config.enabled or self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self.run, name="mocop-training-jobs", daemon=True
        )
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

    def replicas(self, job_id):
        return project_replicas(
            self.client.read(
                f"/pt/api/ai/jobs/by-id/{job_id}/replicas?page=1&page_size=100"
            )
        )

    def metrics(self, job_id):
        return project_metrics(
            self.client.read(f"/pt/api/ai/jobs/by-id/{job_id}/metrics?minutes=30")
        )

    def observations(self, job):
        result = {"id": job["id"]}
        for kind in ("replicas", "metrics"):
            if self.stop_event.is_set():
                break
            try:
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

    def collect(self):
        with self.lock:
            self.attempted_at = time.time() * 1000
        rows = []
        complete = False
        total = 0
        for page in range(1, 21):
            if self.stop_event.is_set():
                return
            data = self.client.read(f"/pt/api/ai/jobs?page={page}&page_size=100")
            if (
                not isinstance(data.get("jobs"), list)
                or not isinstance(data.get("total"), int)
                or isinstance(data["total"], bool)
                or data["total"] < 0
            ):
                raise ReadError(502)
            total = int(data["total"])
            rows.extend(project_job(row) for row in data["jobs"][:100])
            if len(rows) >= total or len(data["jobs"]) < 100:
                complete = len(rows) >= total
                break
        rows = list({row["id"]: row for row in rows}.values())
        complete = complete and len(rows) >= total
        rows.sort(key=lambda j: j["created_at"] or 0, reverse=True)
        with self.lock:
            previous = {row["id"]: row for row in self.jobs}
        active = [job for job in rows if job["status"] not in TERMINAL][:20]
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
        with self.lock:
            self.jobs, self.total, self.complete = rows, total, complete
            self.sampled_at = time.time() * 1000
            self.error_status = None
            self.failure_streak = 0

    def run(self):
        while not self.stop_event.is_set():
            retry = self.config.poll_interval_seconds
            try:
                self.collect()
            except ReadError as error:
                with self.lock:
                    self.error_status = error.status
                    self.failure_streak += 1
                    if error.status in (401, 403):
                        self.jobs = []
                        self.details.clear()
                        self.total = 0
                        self.complete = False
                        self.sampled_at = None
                    retry = max(
                        error.retry_after,
                        min(
                            300,
                            self.config.poll_interval_seconds
                            * 2 ** min(self.failure_streak - 1, 4),
                        ),
                    )
            except Exception:
                with self.lock:
                    self.error_status = 503
                    self.failure_streak += 1
                retry = 60
            self.stop_event.wait(retry)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(
                {
                    "source": "Zenith",
                    "enabled": True,
                    "poll_interval_seconds": self.config.poll_interval_seconds,
                    "sampled_at": self.sampled_at,
                    "attempted_at": self.attempted_at,
                    "stale": self.error_status is not None
                    or self.sampled_at is None
                    or time.time() * 1000 - self.sampled_at
                    > max(120_000, self.config.poll_interval_seconds * 3000),
                    "error_status": self.error_status,
                    "total": self.total,
                    "complete": self.complete,
                    "enriched_active_limit": 20,
                    "jobs": self.jobs,
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
            # Revalidate ownership before returning a cached diagnostic sample.
            try:
                actual = project_job(
                    self.client.read(f"/pt/api/ai/jobs/by-id/{job_id}").get("job")
                )
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
                    result["job"] = actual
                    for kind in ("logs", "replicas"):
                        if not actual["capabilities"][kind]:
                            result.pop(kind, None)
                            result[kind + "_status"] = 501
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
                        data = self.client.read(
                            f"/pt/api/ai/jobs/by-id/{job_id}/logs?log_num=100"
                        )
                        if not isinstance(data.get("lines"), list):
                            raise ReadError(502)
                        result[kind] = {
                            "lines": [
                                {
                                    "body": redact_text(line.get("body"), 4000),
                                    "source": redact_text(line.get("source")),
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
            with self.lock:
                self.details[job_id] = (time.monotonic(), copy.deepcopy(result))
                while len(self.details) > 32:
                    self.details.popitem(last=False)
            return result
        finally:
            self.detail_lock.release()
