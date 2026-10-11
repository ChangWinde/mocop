"""Fictional provider observations for the real-browser training journey."""

from __future__ import annotations

import copy
import time

from mocop.training_projection import project_job, project_metrics, project_replicas


class DemoTraining:
    def __init__(self):
        now = time.time() * 1000
        self.jobs = []
        for index, status in enumerate(("RUNNING", "RUNNING", "FAILED")):
            self.jobs.append(
                project_job(
                    {
                        "id": chr(97 + index) * 25,
                        "vendor": "baidu" if index != 1 else "volcengine",
                        "job_id": f"demo-{index}",
                        "name": "Demo <img src=x onerror=trainingInjected=1>"
                        if index == 0
                        else f"Demo training {index}",
                        "status": status,
                        "execution_class": "standard",
                        "created_at": now - 7200000 - index * 1000,
                        "started_at": now - 5400000 if index == 0 else None,
                        "gpu_count": 8 if index == 0 else None,
                        "gpu_model": "Example GPU" if index == 0 else None,
                        "updated_at": now,
                        "capabilities": {"logs": True, "replicas": True},
                    }
                )
            )
        self.provider_metrics = project_metrics(
            {
                "source": "provider",
                "fetched_at": now,
                "series": [
                    {
                        "metric": metric,
                        "replica_id": "worker-0",
                        "points": [
                            {"ts": now - 1000, "value": None},
                            {"ts": now, "value": 0 if metric == "GpuUsage" else None},
                        ],
                    }
                    for metric in ("GpuUsage", "GpuMemoryUsage")
                ],
            }
        )
        self.stored_metrics = project_metrics(
            {
                "samples": [
                    {
                        "ts": now,
                        "cpu_pct": 0,
                        "gpus_json": "["
                        + ",".join(
                            f'{{"index":{index},"util_pct":0,"mem_used_mb":0,"mem_total_mb":8192}}'
                            for index in range(4)
                        )
                        + "]",
                    }
                ],
                "replica": "worker-0",
                "replicas": ["worker-0"],
                "collection_enabled": True,
            }
        )
        self.replicas = project_replicas(
            {
                "replicas": [
                    {
                        "name": "worker-0",
                        "status": "RUNNING",
                        "exit_code": None,
                        "started_at": now - 1800000,
                    }
                ],
                "total": 1,
            }
        )
        for index, row in enumerate(self.jobs[:2]):
            row.update(
                metrics=self.provider_metrics if index == 0 else self.stored_metrics,
                metrics_status=200,
                replicas=self.replicas,
                replicas_status=200,
            )

    def snapshot(self):
        return copy.deepcopy(
            {
                "enabled": True,
                "source": "Zenith",
                "jobs": self.jobs,
                "total": 3,
                "complete": True,
                "stale": False,
                "sampled_at": time.time() * 1000,
                "poll_interval_seconds": 60,
                "error_status": None,
            }
        )

    def detail(self, job_id):
        row = next(row for row in self.jobs if row["id"] == job_id)
        return copy.deepcopy(
            {
                "job": row,
                "fetched_at": time.time() * 1000,
                "replicas": self.replicas,
                "replicas_status": 200,
                "metrics": self.stored_metrics
                if row["vendor"] == "volcengine"
                else self.provider_metrics,
                "metrics_status": 200,
                "logs_status": 200,
                "logs": {
                    "lines": [
                        {
                            "body": "<script>trainingInjected=1</script> example step",
                            "source": "worker-0",
                        }
                    ],
                    "pending": False,
                    "truncated": True,
                },
            }
        )
