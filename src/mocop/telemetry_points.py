"""Compact in-memory history records.

Host and GPU samples are kept as struct-packed floats keyed by observation
time so a 720-point window per host or device costs a few kilobytes, and
process transitions carry the camelCase shape they are served and persisted
in. ``StateStore`` owns the deques; this module owns the record layouts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from struct import Struct

HOST_HISTORY_VALUES = Struct("<12d")
GPU_HISTORY_VALUES = Struct("<i5d")


@dataclass(frozen=True, slots=True)
class HostHistoryPoint:
    observed_at: str
    values: bytes

    @classmethod
    def create(
        cls,
        observed_at: str,
        cpu_usage_pct: float | None,
        memory_usage_pct: float,
        swap_usage_pct: float,
        disk_usage_pct: float,
        network_rx_bps: float | None,
        network_tx_bps: float | None,
        disk_read_bps: float | None,
        disk_write_bps: float | None,
        gpu_usage_pct: float | None,
        gpu_memory_usage_pct: float | None,
        gpu_temperature_c: float | None,
        transport_retried: bool = False,
    ) -> HostHistoryPoint:
        return cls(
            observed_at,
            HOST_HISTORY_VALUES.pack(
                packed_optional_float(cpu_usage_pct),
                memory_usage_pct,
                swap_usage_pct,
                disk_usage_pct,
                packed_optional_float(network_rx_bps),
                packed_optional_float(network_tx_bps),
                packed_optional_float(disk_read_bps),
                packed_optional_float(disk_write_bps),
                packed_optional_float(gpu_usage_pct),
                packed_optional_float(gpu_memory_usage_pct),
                packed_optional_float(gpu_temperature_c),
                float(transport_retried),
            ),
        )

    @classmethod
    def from_dict(cls, point: dict[str, object]) -> HostHistoryPoint:
        return cls.create(
            observed_at=str(point["observedAt"]),
            cpu_usage_pct=optional_float(point.get("cpuUsagePct")),
            memory_usage_pct=float(point["memoryUsagePct"]),
            swap_usage_pct=float(point["swapUsagePct"]),
            disk_usage_pct=float(point["diskUsagePct"]),
            network_rx_bps=optional_float(point.get("networkRxBps")),
            network_tx_bps=optional_float(point.get("networkTxBps")),
            disk_read_bps=optional_float(point.get("diskReadBps")),
            disk_write_bps=optional_float(point.get("diskWriteBps")),
            gpu_usage_pct=optional_float(point.get("gpuUsagePct")),
            gpu_memory_usage_pct=optional_float(point.get("gpuMemoryUsagePct")),
            gpu_temperature_c=optional_float(point.get("gpuTemperatureC")),
            transport_retried=bool(point.get("transportRetried")),
        )

    def to_dict(self) -> dict[str, object]:
        (
            cpu_usage_pct,
            memory_usage_pct,
            swap_usage_pct,
            disk_usage_pct,
            network_rx_bps,
            network_tx_bps,
            disk_read_bps,
            disk_write_bps,
            gpu_usage_pct,
            gpu_memory_usage_pct,
            gpu_temperature_c,
            transport_retried,
        ) = HOST_HISTORY_VALUES.unpack(self.values)
        return {
            "observedAt": self.observed_at,
            "cpuUsagePct": unpacked_optional_float(cpu_usage_pct),
            "memoryUsagePct": memory_usage_pct,
            "swapUsagePct": swap_usage_pct,
            "diskUsagePct": disk_usage_pct,
            "networkRxBps": unpacked_optional_float(network_rx_bps),
            "networkTxBps": unpacked_optional_float(network_tx_bps),
            "diskReadBps": unpacked_optional_float(disk_read_bps),
            "diskWriteBps": unpacked_optional_float(disk_write_bps),
            "gpuUsagePct": unpacked_optional_float(gpu_usage_pct),
            "gpuMemoryUsagePct": unpacked_optional_float(gpu_memory_usage_pct),
            "gpuTemperatureC": unpacked_optional_float(gpu_temperature_c),
            "transportRetried": bool(transport_retried),
        }


@dataclass(frozen=True, slots=True)
class GpuHistoryPoint:
    observed_at: str
    values: bytes

    @classmethod
    def create(
        cls,
        observed_at: str,
        index: int,
        utilization_gpu_pct: float | None,
        memory_used_mib: float | None,
        memory_total_mib: float | None,
        temperature_c: float | None,
        power_draw_w: float | None,
    ) -> GpuHistoryPoint:
        return cls(
            observed_at,
            GPU_HISTORY_VALUES.pack(
                index,
                packed_optional_float(utilization_gpu_pct),
                packed_optional_float(memory_used_mib),
                packed_optional_float(memory_total_mib),
                packed_optional_float(temperature_c),
                packed_optional_float(power_draw_w),
            ),
        )

    @classmethod
    def from_dict(cls, point: dict[str, object]) -> GpuHistoryPoint:
        return cls.create(
            observed_at=str(point["observedAt"]),
            index=int(point["index"]),
            utilization_gpu_pct=optional_float(point.get("utilizationGpuPct")),
            memory_used_mib=optional_float(point.get("memoryUsedMiB")),
            memory_total_mib=optional_float(point.get("memoryTotalMiB")),
            temperature_c=optional_float(point.get("temperatureC")),
            power_draw_w=optional_float(point.get("powerDrawW")),
        )

    def to_dict(self, gpu_id: str) -> dict[str, object]:
        (
            index,
            utilization_gpu_pct,
            memory_used_mib,
            memory_total_mib,
            temperature_c,
            power_draw_w,
        ) = GPU_HISTORY_VALUES.unpack(self.values)
        return {
            "observedAt": self.observed_at,
            "gpuId": gpu_id,
            "index": index,
            "utilizationGpuPct": unpacked_optional_float(utilization_gpu_pct),
            "memoryUsedMiB": unpacked_optional_float(memory_used_mib),
            "memoryTotalMiB": unpacked_optional_float(memory_total_mib),
            "temperatureC": unpacked_optional_float(temperature_c),
            "powerDrawW": unpacked_optional_float(power_draw_w),
        }


@dataclass(frozen=True, slots=True)
class GpuProcessTransition:
    observed_at: str
    gpu_id: str
    index: int
    event: str
    pid: int
    name: str
    used_memory_mib: float | None
    workload: dict[str, object] | None
    visible: bool = True
    # When this monitor first observed the process on the device. A stopped
    # transition therefore describes its whole run on its own, so occupancy
    # survives even when the matching started transition has left the
    # retained window.
    first_seen_at: str | None = None

    @classmethod
    def from_dict(cls, event: dict[str, object]) -> GpuProcessTransition:
        workload = event.get("workload")
        first_seen_at = event.get("firstSeenAt")
        return cls(
            observed_at=str(event["observedAt"]),
            gpu_id=str(event["gpuId"]),
            index=int(event["index"]),
            event=str(event["event"]),
            pid=int(event["pid"]),
            name=str(event["name"]),
            used_memory_mib=optional_float(event.get("usedMemoryMiB")),
            workload=dict(workload) if isinstance(workload, dict) else None,
            visible=event.get("_visible") is not False,
            first_seen_at=first_seen_at if isinstance(first_seen_at, str) else None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "observedAt": self.observed_at,
            "gpuId": self.gpu_id,
            "index": self.index,
            "event": self.event,
            "pid": self.pid,
            "name": self.name,
            "usedMemoryMiB": self.used_memory_mib,
            "workload": dict(self.workload) if self.workload is not None else None,
            "firstSeenAt": self.first_seen_at,
        }

    def persistence_dict(self) -> dict[str, object]:
        value = self.to_dict()
        value["_visible"] = self.visible
        return value


def optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def packed_optional_float(value: float | None) -> float:
    return math.nan if value is None else value


def unpacked_optional_float(value: float) -> float | None:
    return None if math.isnan(value) else value
