from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

ProbeStatus = Literal["online", "unreachable", "no_nvidia_smi", "error"]


def utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def utc_after(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds)))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class GpuProcess:
    pid: int
    name: str
    used_memory_mib: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GpuMetrics:
    index: int
    uuid: str
    name: str
    driver_version: str
    pstate: str | None
    temperature_c: float | None
    utilization_gpu_pct: float | None
    utilization_memory_pct: float | None
    memory_total_mib: float | None
    memory_used_mib: float | None
    memory_free_mib: float | None
    power_draw_w: float | None
    power_limit_w: float | None
    processes: tuple[GpuProcess, ...] = ()
    processes_available: bool = True

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["processes"] = [process.to_dict() for process in self.processes]
        return value


@dataclass(frozen=True, slots=True)
class DiskMetrics:
    device: str
    filesystem_type: str
    mountpoint: str
    total_mib: float
    used_mib: float
    available_mib: float
    used_pct: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    hostname: str
    uptime_seconds: float
    load_1m: float
    load_5m: float
    load_15m: float
    cpu_cores: int
    cpu_usage_pct: float | None
    memory_total_mib: float
    memory_used_mib: float
    memory_available_mib: float
    swap_total_mib: float
    swap_used_mib: float
    disk_total_mib: float
    disk_used_mib: float
    network_rx_bps: float | None
    network_tx_bps: float | None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None
    disks: tuple[DiskMetrics, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["disks"] = [disk.to_dict() for disk in self.disks]
        return value


@dataclass(frozen=True, slots=True)
class ProbeResult:
    host: str
    status: ProbeStatus
    latency_ms: int
    gpus: tuple[GpuMetrics, ...] = ()
    message: str | None = None
    observed_at: str = field(default_factory=utc_now)
    system: SystemMetrics | None = None


@dataclass(slots=True)
class ServerState:
    host: str
    status: str = "pending"
    polling: bool = False
    latency_ms: int | None = None
    gpus: tuple[GpuMetrics, ...] = ()
    system: SystemMetrics | None = None
    message: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    next_retry_at: str | None = None
    consecutive_failures: int = 0

    def apply(self, result: ProbeResult, next_retry_at: str | None = None) -> None:
        self.status = result.status
        self.polling = False
        self.latency_ms = result.latency_ms
        self.message = result.message
        self.last_attempt_at = result.observed_at
        if result.status == "online":
            self.gpus = result.gpus
            self.system = result.system
            self.last_success_at = result.observed_at
            self.next_retry_at = None
            self.consecutive_failures = 0
        else:
            self.next_retry_at = next_retry_at
            self.consecutive_failures += 1

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "status": self.status,
            "polling": self.polling,
            "latencyMs": self.latency_ms,
            "message": self.message,
            "lastAttemptAt": self.last_attempt_at,
            "lastSuccessAt": self.last_success_at,
            "nextRetryAt": self.next_retry_at,
            "stale": self.status != "online" and self.last_success_at is not None,
            "consecutiveFailures": self.consecutive_failures,
            "system": self.system.to_dict() if self.system else None,
            "gpus": [gpu.to_dict() for gpu in self.gpus],
        }
