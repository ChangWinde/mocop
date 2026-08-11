from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

ProbeStatus = Literal["online", "unreachable", "no_nvidia_smi", "error"]
WorkloadKind = Literal["process", "slurm", "kubernetes"]


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
class WorkloadMetadata:
    kind: WorkloadKind
    workload_id: str | None = None
    name: str | None = None
    owner: str | None = None
    queue: str | None = None
    namespace: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "workload_id": self.workload_id,
            "name": self.name,
            "owner": self.owner,
            "queue": self.queue,
            "namespace": self.namespace,
        }


@dataclass(frozen=True, slots=True)
class GpuProcess:
    pid: int
    name: str
    used_memory_mib: float | None
    workload: WorkloadMetadata | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "name": self.name,
            "used_memory_mib": self.used_memory_mib,
            "workload": self.workload.to_dict() if self.workload else None,
        }


@dataclass(frozen=True, slots=True)
class GpuHealthMetrics:
    ecc_uncorrected_volatile: int | None
    retired_pages_pending: bool | None
    remapped_rows_pending: bool | None
    thermal_slowdown: bool | None
    power_brake_slowdown: bool | None
    mig_mode: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "ecc_uncorrected_volatile": self.ecc_uncorrected_volatile,
            "retired_pages_pending": self.retired_pages_pending,
            "remapped_rows_pending": self.remapped_rows_pending,
            "thermal_slowdown": self.thermal_slowdown,
            "power_brake_slowdown": self.power_brake_slowdown,
            "mig_mode": self.mig_mode,
        }


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
    processes_sampled: bool = True
    processes_observed_at: str | None = None
    health: GpuHealthMetrics | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "uuid": self.uuid,
            "name": self.name,
            "driver_version": self.driver_version,
            "pstate": self.pstate,
            "temperature_c": self.temperature_c,
            "utilization_gpu_pct": self.utilization_gpu_pct,
            "utilization_memory_pct": self.utilization_memory_pct,
            "memory_total_mib": self.memory_total_mib,
            "memory_used_mib": self.memory_used_mib,
            "memory_free_mib": self.memory_free_mib,
            "power_draw_w": self.power_draw_w,
            "power_limit_w": self.power_limit_w,
            "processes": [process.to_dict() for process in self.processes],
            "processes_available": self.processes_available,
            "processes_sampled": self.processes_sampled,
            "processes_observed_at": self.processes_observed_at,
            "health": self.health.to_dict() if self.health else None,
        }


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
        return {
            "device": self.device,
            "filesystem_type": self.filesystem_type,
            "mountpoint": self.mountpoint,
            "total_mib": self.total_mib,
            "used_mib": self.used_mib,
            "available_mib": self.available_mib,
            "used_pct": self.used_pct,
        }


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
        return {
            "hostname": self.hostname,
            "uptime_seconds": self.uptime_seconds,
            "load_1m": self.load_1m,
            "load_5m": self.load_5m,
            "load_15m": self.load_15m,
            "cpu_cores": self.cpu_cores,
            "cpu_usage_pct": self.cpu_usage_pct,
            "memory_total_mib": self.memory_total_mib,
            "memory_used_mib": self.memory_used_mib,
            "memory_available_mib": self.memory_available_mib,
            "swap_total_mib": self.swap_total_mib,
            "swap_used_mib": self.swap_used_mib,
            "disk_total_mib": self.disk_total_mib,
            "disk_used_mib": self.disk_used_mib,
            "network_rx_bps": self.network_rx_bps,
            "network_tx_bps": self.network_tx_bps,
            "disk_read_bps": self.disk_read_bps,
            "disk_write_bps": self.disk_write_bps,
            "disks": [disk.to_dict() for disk in self.disks],
        }


@dataclass(frozen=True, slots=True)
class ProbeResult:
    host: str
    status: ProbeStatus
    latency_ms: int
    gpus: tuple[GpuMetrics, ...] = ()
    message: str | None = None
    observed_at: str = field(default_factory=utc_now)
    system: SystemMetrics | None = None
    transport_retries: int = 0


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
    transport_retried: bool = False

    def apply(self, result: ProbeResult, next_retry_at: str | None = None) -> None:
        self.status = result.status
        self.polling = False
        self.latency_ms = result.latency_ms
        self.message = result.message
        self.last_attempt_at = result.observed_at
        self.transport_retried = result.transport_retries > 0
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
            "transportRetried": self.transport_retried,
            "system": self.system.to_dict() if self.system else None,
            "gpus": [gpu.to_dict() for gpu in self.gpus],
        }
