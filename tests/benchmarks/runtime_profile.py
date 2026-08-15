"""Reproducible in-process Mocop runtime and retention profile.

This is a diagnostic benchmark, not a CI timing gate. Run it from the repository
root so results can be compared on the same host and Python build::

    .venv/bin/python -m tests.benchmarks.runtime_profile
"""

from __future__ import annotations

import argparse
import gc
import gzip
import json
import os
import platform
import statistics
import threading
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from mocop.models import (
    GpuMetrics,
    GpuProcess,
    ProbeResult,
    SystemMetrics,
    WorkloadMetadata,
)
from mocop.service import StateStore
from mocop.web import MonitorHttpServer

T = TypeVar("T")


def _measure(operation: Callable[[], T], runs: int) -> tuple[dict[str, float], T]:
    for _ in range(3):
        operation()
    samples: list[float] = []
    result: T
    for _ in range(runs):
        started = time.perf_counter()
        result = operation()
        samples.append((time.perf_counter() - started) * 1_000)
    ordered = sorted(samples)
    return (
        {
            "medianMs": round(statistics.median(ordered), 4),
            "p95Ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 4),
            "stdevMs": round(statistics.stdev(ordered), 4) if len(ordered) > 1 else 0,
        },
        result,
    )


def _system(host: str) -> SystemMetrics:
    return SystemMetrics(
        hostname=host,
        uptime_seconds=86_400,
        load_1m=4,
        load_5m=3,
        load_15m=2,
        cpu_cores=64,
        cpu_usage_pct=25,
        memory_total_mib=524_288,
        memory_used_mib=131_072,
        memory_available_mib=393_216,
        swap_total_mib=8_192,
        swap_used_mib=0,
        disk_total_mib=8_000_000,
        disk_used_mib=4_000_000,
        network_rx_bps=1_000_000,
        network_tx_bps=2_000_000,
    )


def _gpu(host_index: int, gpu_index: int, process_count: int) -> GpuMetrics:
    processes = tuple(
        GpuProcess(
            pid=host_index * 100_000 + gpu_index * 1_000 + process_index,
            name=f"/workspace/train-{process_index}.py",
            used_memory_mib=4_096 + process_index,
            workload=WorkloadMetadata(
                kind="slurm",
                workload_id=f"job-{host_index}-{process_index}",
                name=f"train-{process_index}",
                owner=f"user-{process_index % 8}",
                queue="gpu",
                command=f"python train-{process_index}.py --epochs 10",
                started_at="2026-08-15T00:00:00Z",
            ),
            first_seen_at="2026-08-15T00:00:00Z",
        )
        for process_index in range(process_count)
    )
    return GpuMetrics(
        index=gpu_index,
        uuid=f"GPU-{host_index:03d}-{gpu_index}",
        name="NVIDIA H100 80GB HBM3",
        driver_version="580.1",
        pstate="P0",
        temperature_c=65,
        utilization_gpu_pct=85,
        utilization_memory_pct=70,
        memory_total_mib=81_920,
        memory_used_mib=65_536,
        memory_free_mib=16_384,
        power_draw_w=600,
        power_limit_w=700,
        processes=processes,
        processes_observed_at="2026-08-15T01:00:00Z",
    )


def _build_store(host_count: int, gpu_count: int, process_count: int) -> StateStore:
    store = StateStore(5)
    hosts = tuple(f"gpu-{index:03d}" for index in range(host_count))
    store.set_hosts(hosts)
    for host_index, host in enumerate(hosts):
        store.apply(
            ProbeResult(
                host=host,
                status="online",
                latency_ms=20,
                gpus=tuple(
                    _gpu(host_index, gpu_index, process_count)
                    for gpu_index in range(gpu_count)
                ),
                observed_at="2026-08-15T01:00:00Z",
                system=_system(host),
            )
        )
    return store


def _retention_delta(gpu_count: int, process_count: int) -> dict[str, float | int]:
    host_count = 20
    history_points = 120
    hosts = tuple(f"soak-{index:02d}" for index in range(host_count))
    store = StateStore(5, history_points=history_points, incident_history_points=20)
    store.set_hosts(hosts)
    gpus = tuple(_gpu(0, index, process_count) for index in range(gpu_count))

    def apply_cycles(start: int, count: int) -> None:
        for cycle in range(start, start + count):
            observed_at = f"2026-08-15T{(cycle // 3_600) % 24:02d}:{(cycle // 60) % 60:02d}:{cycle % 60:02d}Z"
            for host in hosts:
                store.apply(
                    ProbeResult(
                        host,
                        "online",
                        10,
                        gpus,
                        observed_at=observed_at,
                    )
                )

    apply_cycles(0, history_points)
    stabilization_cycles = 50
    apply_cycles(history_points, stabilization_cycles)
    store.snapshot_view()
    gc.collect()
    at_capacity = tracemalloc.get_traced_memory()[0]
    extra_cycles = 200
    apply_cycles(history_points + stabilization_cycles, extra_cycles)
    store.snapshot_view()
    gc.collect()
    after_extra = tracemalloc.get_traced_memory()[0]
    return {
        "hosts": host_count,
        "gpus": host_count * gpu_count,
        "historyPoints": history_points,
        "stabilizationCycles": stabilization_cycles,
        "extraCycles": extra_cycles,
        "atCapacityMiB": round(at_capacity / 1_048_576, 3),
        "afterExtraCyclesMiB": round(after_extra / 1_048_576, 3),
        "deltaKiB": round((after_extra - at_capacity) / 1_024, 3),
    }


def _rss_mib() -> float | None:
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return round(int(line.split()[1]) / 1_024, 2)
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hosts", type=int, default=200)
    parser.add_argument("--gpus", type=int, default=8)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--runs", type=int, default=15)
    args = parser.parse_args()
    if not 1 <= args.hosts <= 1_024:
        parser.error("--hosts must be between 1 and 1024")
    if not 1 <= args.gpus <= 256:
        parser.error("--gpus must be between 1 and 256")
    if args.processes < 0 or args.gpus * args.processes > 4_096:
        parser.error("--gpus * --processes must be between 0 and 4096")
    if not 5 <= args.runs <= 100:
        parser.error("--runs must be between 5 and 100")

    tracemalloc.start()
    store = _build_store(args.hosts, args.gpus, args.processes)
    gc.collect()
    retained, build_peak = tracemalloc.get_traced_memory()
    # Allocation tracing is intentionally disabled for wall-clock samples: it
    # instruments every JSON container allocation and would distort latency.
    tracemalloc.stop()
    snapshot = store.snapshot_view()
    json_profile, payload = _measure(
        lambda: json.dumps(
            snapshot, ensure_ascii=False, separators=(",", ":")
        ).encode(),
        args.runs,
    )
    gzip_profile, compressed = _measure(
        lambda: gzip.compress(payload, compresslevel=5), args.runs
    )
    server = MonitorHttpServer(("127.0.0.1", 0), store)
    server.snapshot_payload(snapshot)
    server.metrics_payload(snapshot)
    snapshot_view_profile, _ = _measure(store.snapshot_view, args.runs)
    deep_copy_profile, _ = _measure(store.snapshot, max(5, args.runs // 2))
    cached_json_profile, _ = _measure(
        lambda: server.snapshot_payload(store.snapshot_view()), args.runs
    )
    cached_metrics_profile, metrics = _measure(
        lambda: server.metrics_payload(store.snapshot_view()), args.runs
    )

    baseline_threads = threading.active_count()
    baseline_fds = len(os.listdir("/proc/self/fd"))
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    active_threads = threading.active_count()
    active_fds = len(os.listdir("/proc/self/fd"))
    server.shutdown()
    server_thread.join()
    server.server_close()
    after_close_threads = threading.active_count()
    after_close_fds = len(os.listdir("/proc/self/fd"))

    tracemalloc.start()
    retention_soak = _retention_delta(min(args.gpus, 8), min(args.processes, 4))
    tracemalloc.stop()
    result = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu": platform.processor() or "unknown",
        },
        "shape": {
            "hosts": args.hosts,
            "gpus": args.hosts * args.gpus,
            "processes": args.hosts * args.gpus * args.processes,
        },
        "memory": {
            "storeTracedMiB": round(retained / 1_048_576, 2),
            "buildPeakMiB": round(build_peak / 1_048_576, 2),
            "processRssMiB": _rss_mib(),
        },
        "payload": {
            "snapshotBytes": len(payload),
            "metricsBytes": len(metrics),
            "gzipLevel5Bytes": len(compressed),
            "gzipRatioPct": round(len(compressed) / max(1, len(payload)) * 100, 2),
        },
        "latency": {
            "snapshotView": snapshot_view_profile,
            "snapshotDeepCopy": deep_copy_profile,
            "jsonCold": json_profile,
            "jsonCached": cached_json_profile,
            "metricsCached": cached_metrics_profile,
            "gzipLevel5": gzip_profile,
        },
        "httpLifecycle": {
            "threadsBeforeServe": baseline_threads,
            "threadsWhileServing": active_threads,
            "threadsAfterClose": after_close_threads,
            "fdsBeforeServe": baseline_fds,
            "fdsWhileServing": active_fds,
            "fdsAfterClose": after_close_fds,
        },
        "retentionSoak": retention_soak,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
