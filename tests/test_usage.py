from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from mocop.models import GpuProcess, WorkloadMetadata
from mocop.usage import aggregate_usage

NOW = datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)
GPU = ("gpu-1", "GPU-abc")


def _at(minutes_before_now: int) -> str:
    moment = NOW - timedelta(minutes=minutes_before_now)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Transition:
    """Any record with the six fields the aggregator reads is accepted."""

    observed_at: str
    event: str
    pid: int
    name: str
    workload: dict[str, object] | None = None
    first_seen_at: str | None = None


class AggregateUsageTests(unittest.TestCase):
    """The pure rollup behind StateStore.usage(); the store tests cover the
    live timeline, this pins the module boundary and the accounting rules."""

    def test_live_processes_of_a_failing_host_count_up_to_its_last_sample(self) -> None:
        # While a host is failing its probes the live table is a blind spot;
        # an active process occupies the device up to the last confirmed
        # sample, never through the gap to now. Both the unmatched-start and
        # the seeded-process anchors honour it.
        started = Transition(_at(50), "started", 1, "train", {"owner": "alice"})
        seeded = GpuProcess(2, "serve", 100, None, first_seen_at=_at(40))
        usage = aggregate_usage(
            now=NOW,
            window_hours=1,
            owner_limit=10,
            busy_pct=20.0,
            events_by_gpu={GPU: [started]},
            active_by_gpu={
                GPU: {(1, "train"): GpuProcess(1, "train", 100), (2, "serve"): seeded}
            },
            utilization_by_gpu={},
            observed_until_by_gpu={GPU: _at(20)},
        )
        by_owner = {entry["owner"]: entry for entry in usage["owners"]}
        self.assertEqual(by_owner["alice"]["gpuSeconds"], 30 * 60.0)
        self.assertEqual(by_owner[None]["gpuSeconds"], 20 * 60.0)
        # An online host (no entry) still counts to now.
        online = aggregate_usage(
            now=NOW,
            window_hours=1,
            owner_limit=10,
            busy_pct=20.0,
            events_by_gpu={GPU: [started]},
            active_by_gpu={GPU: {(1, "train"): GpuProcess(1, "train", 100)}},
            utilization_by_gpu={},
            observed_until_by_gpu={},
        )
        self.assertEqual(online["owners"][0]["gpuSeconds"], 50 * 60.0)

    def test_reports_devices_whose_retained_timeline_starts_inside_the_window(
        self,
    ) -> None:
        # earliestDataAt is the earliest record across all devices, so a quiet
        # device can make a report look complete while a busy one's full
        # timeline only reaches back an hour. partialGpus counts the latter.
        quiet = [Transition(_at(23 * 60), "started", 1, "notebook", {"owner": "a"})]
        busy = [
            Transition(
                _at(60 - index),
                "started" if index % 2 == 0 else "stopped",
                100 + index // 2,
                "job",
            )
            for index in range(4)
        ]
        usage = aggregate_usage(
            now=NOW,
            window_hours=24,
            owner_limit=10,
            busy_pct=20.0,
            events_by_gpu={GPU: quiet, ("node-b", "GPU-2"): busy},
            active_by_gpu={GPU: {}, ("node-b", "GPU-2"): {}},
            utilization_by_gpu={},
            event_cap=4,
        )
        self.assertEqual(usage["partialGpus"], 1)
        self.assertEqual(usage["earliestDataAt"], _at(23 * 60))
        # Without a cap nothing can be judged truncated; a device below the cap
        # holds its complete timeline even when it starts inside the window.
        complete = aggregate_usage(
            now=NOW,
            window_hours=24,
            owner_limit=10,
            busy_pct=20.0,
            events_by_gpu={GPU: quiet, ("node-b", "GPU-2"): busy},
            active_by_gpu={},
            utilization_by_gpu={},
            event_cap=5,
        )
        self.assertEqual(complete["partialGpus"], 0)

    def test_unmatched_stops_anchor_on_the_monitors_first_observation(self) -> None:
        # Live evidence: a busy GPU churns hundreds of processes a day, so the
        # started edge of many runs leaves the retained event window before
        # the stopped edge does; 592 of 644 dropped records in a 24-hour
        # report were such orphan stops. A stop that carries when this monitor
        # first saw the process on the device is a complete run on its own.
        carol = {"owner": "carol", "kind": "process"}
        events = [
            Transition(_at(20), "stopped", 5, "train", carol, first_seen_at=_at(50)),
            # A first observation after the stop is corrupt and stays dropped,
            # as does a stop without one.
            Transition(_at(15), "stopped", 6, "eval", carol, first_seen_at=_at(10)),
            Transition(_at(12), "stopped", 7, "ghost", carol),
        ]
        usage = aggregate_usage(
            now=NOW,
            window_hours=1,
            owner_limit=10,
            busy_pct=20.0,
            events_by_gpu={GPU: events},
            active_by_gpu={GPU: {}},
            utilization_by_gpu={GPU: []},
        )
        self.assertEqual(usage["droppedRecords"], 2)
        (owner,) = usage["owners"]
        self.assertEqual(owner["owner"], "carol")
        self.assertEqual(owner["gpuSeconds"], 30 * 60.0)
        self.assertEqual(usage["earliestDataAt"], _at(50))

    def test_pairs_transitions_merges_owners_and_classifies_idle_time(self) -> None:
        alice = {"owner": "alice", "kind": "slurm"}
        events = [
            Transition(_at(50), "started", 1, "train", alice),
            # A concurrent second process of the same owner on the same GPU
            # is one occupancy window, not double GPU-time.
            Transition(_at(40), "started", 2, "eval", alice),
            Transition(_at(30), "stopped", 2, "eval", alice),
            Transition(_at(20), "stopped", 1, "train", alice),
            # An unmatched stop without the monitor's first observation has
            # no safe anchor and is reported dropped.
            Transition(_at(10), "stopped", 9, "ghost"),
        ]
        # One sample per minute; each segment takes the classification of the
        # sample that opens it, so utilization 0 at minutes 55..36 makes the
        # 15 segments from minute 50 to minute 35 idle and the rest busy.
        samples = [
            (_at(minute), 0.0 if minute > 35 else 90.0) for minute in range(55, 15, -1)
        ]
        usage = aggregate_usage(
            now=NOW,
            window_hours=1,
            owner_limit=10,
            busy_pct=20.0,
            events_by_gpu={GPU: events},
            active_by_gpu={GPU: {}},
            utilization_by_gpu={GPU: samples},
        )
        (owner,) = usage["owners"]
        self.assertEqual(owner["owner"], "alice")
        self.assertEqual(owner["gpuSeconds"], 30 * 60)
        self.assertEqual(owner["processes"], 2)
        self.assertEqual(owner["kinds"], {"slurm": 2})
        self.assertEqual(owner["hosts"], ["gpu-1"])
        # Minute 50 to 35 idle, 35 to 20 busy: the whole window is sampled.
        self.assertEqual(owner["sampledSeconds"], 30 * 60)
        self.assertEqual(owner["idleSeconds"], 15 * 60)
        self.assertEqual(owner["idleShare"], 0.5)
        self.assertEqual(usage["droppedRecords"], 1)
        self.assertEqual(usage["totalGpuSeconds"], 30 * 60)
        self.assertEqual(usage["earliestDataAt"], _at(55))
        self.assertEqual(usage["sinceAt"], _at(60))

    def test_live_processes_anchor_open_starts_and_first_samples(self) -> None:
        # An open start survives only while the live table still lists it and
        # keeps the attribution its start transition carried; a live process
        # that never emitted a start is anchored at its monitor-side first
        # sighting and attributed from the live workload record.
        live = {
            (1, "train"): GpuProcess(pid=1, name="train", used_memory_mib=1024),
            (3, "notebook"): GpuProcess(
                pid=3, name="notebook", used_memory_mib=512, first_seen_at=_at(15)
            ),
        }
        usage = aggregate_usage(
            now=NOW,
            window_hours=1,
            owner_limit=10,
            busy_pct=20.0,
            events_by_gpu={
                GPU: [
                    Transition(
                        _at(30), "started", 1, "train", {"owner": "bob", "kind": "k8s"}
                    ),
                    Transition(_at(30), "started", 2, "orphan"),
                ]
            },
            active_by_gpu={GPU: live},
            utilization_by_gpu={},
        )
        by_owner = {entry["owner"]: entry for entry in usage["owners"]}
        self.assertEqual(by_owner["bob"]["gpuSeconds"], 30 * 60)
        self.assertEqual(by_owner["bob"]["kinds"], {"k8s": 1})
        self.assertEqual(by_owner[None]["gpuSeconds"], 15 * 60)
        self.assertEqual(by_owner[None]["kinds"], {"process": 1})
        self.assertIsNone(by_owner["bob"]["idleShare"])
        self.assertEqual(usage["droppedRecords"], 1)
        # Named owners rank before the anonymous bucket at equal time and the
        # limit truncates the ranking but not the totals.
        live[(1, "train")] = GpuProcess(
            pid=1,
            name="train",
            used_memory_mib=1024,
            workload=WorkloadMetadata(kind="k8s", owner="bob"),
            first_seen_at=_at(15),
        )
        limited = aggregate_usage(
            now=NOW,
            window_hours=1,
            owner_limit=1,
            busy_pct=20.0,
            events_by_gpu={},
            active_by_gpu={GPU: live},
            utilization_by_gpu={},
        )
        self.assertEqual([entry["owner"] for entry in limited["owners"]], ["bob"])
        self.assertEqual(limited["totalOwners"], 2)

    def test_window_clipping_and_stale_sample_gaps(self) -> None:
        events = [
            Transition(_at(180), "started", 1, "long", {"owner": "carol"}),
            Transition(_at(30), "stopped", 1, "long", {"owner": "carol"}),
        ]
        # Two samples 5 minutes apart exceed the 60 s classification gap, so
        # the segment between them stays unclassified.
        samples = [(_at(60), 0.0), (_at(55), 0.0)]
        usage = aggregate_usage(
            now=NOW,
            window_hours=1,
            owner_limit=10,
            busy_pct=20.0,
            events_by_gpu={GPU: events},
            active_by_gpu={},
            utilization_by_gpu={GPU: samples},
        )
        (owner,) = usage["owners"]
        self.assertEqual(owner["gpuSeconds"], 30 * 60)
        self.assertEqual(owner["sampledSeconds"], 0)
        self.assertIsNone(owner["idleShare"])
        # The earliest event predates the window and is still reported.
        self.assertEqual(usage["earliestDataAt"], _at(180))
        self.assertEqual(usage["windowHours"], 1)
        self.assertEqual(usage["gpuBusyPct"], 20.0)


if __name__ == "__main__":
    unittest.main()
