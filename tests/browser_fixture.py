from __future__ import annotations

import sys
import threading
import time

from mocop.models import DiskMetrics, GpuMetrics, ProbeResult, SystemMetrics
from mocop.service import StateStore
from mocop.web import MonitorHttpServer


def gpu(
    host: str,
    index: int,
    utilization: float,
    memory_used: float,
    temperature: float,
) -> GpuMetrics:
    return GpuMetrics(
        index=index,
        uuid=f"GPU-DEMO-{host}-{index:02d}",
        name="NVIDIA H100 80GB HBM3",
        driver_version="550.90.07",
        pstate="P0" if utilization >= 10 else "P8",
        temperature_c=temperature,
        utilization_gpu_pct=utilization,
        utilization_memory_pct=min(100, utilization + 8),
        memory_total_mib=81_920,
        memory_used_mib=memory_used,
        memory_free_mib=81_920 - memory_used,
        power_draw_w=round(75 + utilization * 4.5, 1),
        power_limit_w=700,
    )


def system(hostname: str, cpu: float, memory_used: float) -> SystemMetrics:
    disk = DiskMetrics(
        device="/dev/nvme0n1p2",
        filesystem_type="ext4",
        mountpoint="/",
        total_mib=1_907_348,
        used_mib=812_442,
        available_mib=1_094_906,
        used_pct=42.6,
    )
    return SystemMetrics(
        hostname=hostname,
        uptime_seconds=1_428_320,
        load_1m=8.4,
        load_5m=7.8,
        load_15m=7.2,
        cpu_cores=128,
        cpu_usage_pct=cpu,
        memory_total_mib=1_048_576,
        memory_used_mib=memory_used,
        memory_available_mib=1_048_576 - memory_used,
        swap_total_mib=16_384,
        swap_used_mib=512,
        disk_total_mib=disk.total_mib,
        disk_used_mib=disk.used_mib,
        network_rx_bps=186_646_528,
        network_tx_bps=92_274_688,
        disk_read_bps=47_185_920,
        disk_write_bps=18_874_368,
        disks=(disk,),
    )


def demo_state() -> StateStore:
    state = StateStore(5)
    state.set_hosts(("atlas-01", "atlas-02", "atlas-03"))
    state.apply(
        ProbeResult(
            host="atlas-01",
            status="online",
            latency_ms=38,
            gpus=(
                gpu("atlas-01", 0, 96, 72_704, 74),
                gpu("atlas-01", 1, 91, 70_912, 72),
                gpu("atlas-01", 2, 14, 38_400, 57),
                gpu("atlas-01", 3, 2, 2_048, 35),
            ),
            observed_at="2026-08-09T09:30:00Z",
            system=system("atlas-01", 68, 712_704),
        )
    )
    state.apply(
        ProbeResult(
            host="atlas-02",
            status="online",
            latency_ms=44,
            gpus=(
                gpu("atlas-02", 0, 88, 68_096, 70),
                gpu("atlas-02", 1, 76, 61_440, 67),
                gpu("atlas-02", 2, 8, 12_288, 42),
                gpu("atlas-02", 3, 0, 1_024, 32),
            ),
            observed_at="2026-08-09T09:30:00Z",
            system=system("atlas-02", 53, 601_088),
        )
    )
    state.apply(
        ProbeResult(
            host="atlas-03",
            status="unreachable",
            latency_ms=5_000,
            message="SSH connection timed out",
            observed_at="2026-08-09T09:30:00Z",
        )
    )
    state.record_poll_cycle(0.42)
    return state


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: browser_fixture.py PORT")
    state = demo_state()

    def publish_poll_completions() -> None:
        while True:
            time.sleep(1)
            state.record_poll_cycle(0.42)

    threading.Thread(
        target=publish_poll_completions,
        name="mocop-browser-fixture",
        daemon=True,
    ).start()
    server = MonitorHttpServer(("127.0.0.1", int(sys.argv[1])), state)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
