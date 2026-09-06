from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from mocop.models import GpuProcess
from mocop.occupancy import Interval
from mocop.reports import (
    ReportInputs,
    classify_hourly,
    usage_report,
    utilization_report,
)

NOW = datetime(2026, 9, 6, 12, 30, tzinfo=timezone.utc)


def _at(minutes_before_now: int) -> str:
    moment = NOW - timedelta(minutes=minutes_before_now)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _hour(hours_before_now: int) -> str:
    moment = (NOW - timedelta(hours=hours_before_now)).replace(minute=0)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def transition(
    minutes_before_now: int,
    event: str,
    pid: int,
    owner: str | None = "alice",
    first_seen_minutes_before_now: int | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "observedAt": _at(minutes_before_now),
        "gpuId": "GPU-1",
        "index": 0,
        "event": event,
        "pid": pid,
        "name": "train.py",
        "usedMemoryMiB": 1024.0,
        "workload": {"kind": "process", "owner": owner} if owner else None,
    }
    if first_seen_minutes_before_now is not None:
        record["firstSeenAt"] = _at(first_seen_minutes_before_now)
    return record


def hourly(
    host: str,
    gpu_id: str,
    hours_before_now: int,
    utilization_samples: int,
    busy_samples: int,
    *,
    utilization_sum: float | None = None,
    memory_used_sum: float = 0.0,
    memory_total: float | None = 8192.0,
) -> tuple:
    return (
        host,
        gpu_id,
        _hour(hours_before_now),
        utilization_samples,
        utilization_samples,
        busy_samples,
        utilization_sum if utilization_sum is not None else 50.0 * utilization_samples,
        utilization_samples,
        memory_used_sum,
        memory_total,
    )


class ClassifyHourlyTests(unittest.TestCase):
    def test_applies_each_hours_busy_share_to_the_overlap(self) -> None:
        # An interval spanning two hours: the first hour is half idle, the
        # second fully busy, the third has no samples and stays unsampled.
        base = NOW.replace(minute=0).timestamp()
        interval = Interval(base + 1800, base + 3600 * 2 + 600, "alice", "process")
        classify_hourly(
            [interval],
            [(base, 720, 360), (base + 3600, 720, 720), (base + 7200, 0, 0)],
        )
        self.assertEqual(interval.sampled_seconds, 1800 + 3600)
        self.assertEqual(interval.idle_seconds, 900)
        # No buckets at all leaves the interval unclassified.
        bare = Interval(base, base + 60, None, "process")
        classify_hourly([bare], [])
        self.assertEqual((bare.sampled_seconds, bare.idle_seconds), (0.0, 0.0))


class UsageReportTests(unittest.TestCase):
    def test_reports_owner_occupancy_days_and_coverage_from_history(self) -> None:
        # A closed run, an orphan stop anchored on its first observation, and
        # a live process; the closed run crosses UTC midnight so the day
        # breakdown splits it.
        key = ("gpu-01", "GPU-1")
        inputs = ReportInputs(
            transitions={
                key: (
                    transition(15 * 60, "started", 1),  # 21:30 the day before
                    transition(11 * 60, "stopped", 1),  # 01:30 today
                    transition(60, "stopped", 2, first_seen_minutes_before_now=120),
                    transition(30, "started", 3, owner="bob"),
                )
            },
            hourly=tuple(
                hourly(
                    "gpu-01", "GPU-1", hours_before, 720, 720 if hours_before < 2 else 0
                )
                for hours_before in range(0, 16)
            ),
            retention_hours=72,
        )
        live = {
            (3, "train.py"): GpuProcess(
                3, "train.py", 1024.0, None, first_seen_at=_at(30)
            )
        }

        report = usage_report(
            now=NOW,
            window_hours=24,
            owner_limit=10,
            busy_pct=10.0,
            inputs=inputs,
            active_by_gpu={key: live},
            observed_until_by_gpu={},
        )

        self.assertEqual(report["source"], "history")
        self.assertEqual(report["resolution"], "hour")
        self.assertEqual(report["retentionHours"], 72)
        self.assertEqual(report["coveredFromAt"], report["sinceAt"])
        self.assertEqual(report["partialGpus"], 0)
        self.assertEqual(report["droppedRecords"], 0)
        by_owner = {owner["owner"]: owner for owner in report["owners"]}
        # alice: the closed run (4 h) plus the anchored orphan (1 h).
        self.assertEqual(by_owner["alice"]["gpuSeconds"], 5 * 3600.0)
        # bob: the live process from 30 minutes ago to now.
        self.assertEqual(by_owner["bob"]["gpuSeconds"], 1800.0)
        # Idle share comes from the hourly buckets: every hour before the last
        # two was idle (busy 0), so the closed run is fully idle and the
        # anchored orphan is idle for its first half hour only.
        self.assertEqual(by_owner["alice"]["idleSeconds"], 4.5 * 3600.0)
        self.assertEqual(by_owner["alice"]["sampledSeconds"], 5 * 3600.0)
        self.assertEqual(by_owner["bob"]["idleSeconds"], 0.0)
        days = {entry["day"]: entry for entry in report["days"]}
        self.assertEqual(sorted(days), ["2026-09-05", "2026-09-06"])
        self.assertEqual(days["2026-09-05"]["gpuSeconds"], 2.5 * 3600.0)
        self.assertEqual(
            days["2026-09-06"]["owners"][0],
            {"owner": "alice", "gpuSeconds": 2.5 * 3600.0},
        )

    def test_coverage_start_follows_retention_when_the_window_is_longer(self) -> None:
        inputs = ReportInputs(transitions={}, hourly=(), retention_hours=72)
        report = usage_report(
            now=NOW,
            window_hours=720,
            owner_limit=10,
            busy_pct=10.0,
            inputs=inputs,
            active_by_gpu={},
            observed_until_by_gpu={},
        )
        self.assertEqual(report["coveredFromAt"], _at(72 * 60))
        self.assertEqual(report["owners"], [])
        self.assertEqual(report["days"], [])


class UtilizationReportTests(unittest.TestCase):
    def test_series_per_host_and_per_device_with_fleet_totals(self) -> None:
        rows = (
            hourly(
                "gpu-01",
                "GPU-1",
                1,
                720,
                360,
                utilization_sum=36000.0,
                memory_used_sum=720 * 4096.0,
            ),
            hourly(
                "gpu-01",
                "GPU-2",
                1,
                720,
                720,
                utilization_sum=72000.0,
                memory_used_sum=720 * 8192.0,
            ),
            hourly(
                "gpu-02", "GPU-3", 1, 720, 0, utilization_sum=0.0, memory_used_sum=0.0
            ),
            hourly("gpu-01", "GPU-1", 30, 720, 720),  # outside a 24-hour window
        )
        report = utilization_report(now=NOW, window_hours=24, rows=rows)
        self.assertEqual(report["resolution"], "hour")
        self.assertIsNone(report["host"])
        (fleet_hour,) = report["fleet"]
        self.assertEqual(fleet_hour["hour"], _hour(1))
        self.assertEqual(fleet_hour["gpus"], 3)
        self.assertEqual(fleet_hour["busyShare"], 0.5)
        self.assertEqual(fleet_hour["utilizationAvgPct"], 50.0)
        self.assertEqual(fleet_hour["memoryTotalMiB"], 3 * 8192.0)
        hosts = {entry["host"]: entry for entry in report["hosts"]}
        self.assertEqual(sorted(hosts), ["gpu-01", "gpu-02"])
        (hour,) = hosts["gpu-01"]["hours"]
        self.assertEqual(hour["gpus"], 2)
        self.assertEqual(hour["busyShare"], 0.75)
        self.assertEqual(hour["memoryUsedAvgMiB"], 6144.0)
        self.assertEqual(hosts["gpu-02"]["hours"][0]["busyShare"], 0.0)

        per_device = utilization_report(
            now=NOW, window_hours=24, rows=rows, host="gpu-01"
        )
        self.assertEqual(per_device["host"], "gpu-01")
        self.assertEqual(
            [entry["gpuId"] for entry in per_device["gpus"]], ["GPU-1", "GPU-2"]
        )
        self.assertEqual(per_device["gpus"][0]["hours"][0]["utilizationAvgPct"], 50.0)
        self.assertEqual(per_device["fleet"][0]["gpus"], 2)

    def test_hours_without_utilization_samples_report_null_shares(self) -> None:
        row = ("gpu-01", "GPU-1", _hour(1), 12, 0, 0, 0.0, 0, 0.0, None)
        report = utilization_report(now=NOW, window_hours=24, rows=(row,))
        (hour,) = report["fleet"]
        self.assertEqual(hour["samples"], 12)
        self.assertIsNone(hour["busyShare"])
        self.assertIsNone(hour["utilizationAvgPct"])
        self.assertIsNone(hour["memoryUsedAvgMiB"])
        self.assertIsNone(hour["memoryTotalMiB"])


if __name__ == "__main__":
    unittest.main()
