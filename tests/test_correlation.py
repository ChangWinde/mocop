from __future__ import annotations

import unittest

from mocop.config import ConnectionTopologyConfig, TopologyLinkConfig
from mocop.correlation import (
    SimultaneousLossCorrelator,
    TopologyIncidentCorrelator,
    create_incident_correlator,
)


def connectivity(
    host: str,
    *,
    silenced: bool = False,
    actionable: bool | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "host": host,
        "conditionKey": "connectivity",
        "category": "connectivity",
        "severity": "critical",
        "silenced": silenced,
    }
    if actionable is not None:
        item["actionable"] = actionable
    return item


class TopologyIncidentCorrelatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.correlator = TopologyIncidentCorrelator(
            ConnectionTopologyConfig(
                root="monitor",
                links=(
                    TopologyLinkConfig("monitor", "gateway", "ssh"),
                    TopologyLinkConfig("gateway", "gpu-01", "ssh"),
                    TopologyLinkConfig("gateway", "gpu-02", "ssh"),
                    TopologyLinkConfig("monitor", "gpu-03", "ssh"),
                ),
            )
        )

    def test_groups_unreachable_descendants_by_the_deepest_shared_path(self) -> None:
        correlations = self.correlator.correlate(
            (connectivity("gpu-01"), connectivity("gpu-02")),
            frozenset({"gpu-01", "gpu-02", "gpu-03"}),
        )

        self.assertEqual(len(correlations), 1)
        self.assertEqual(correlations[0]["anchor"], "gateway")
        self.assertEqual(correlations[0]["hosts"], ["gpu-01", "gpu-02"])
        self.assertEqual(correlations[0]["confidence"], "possible")

    def test_does_not_invent_a_root_cause_or_include_silenced_hosts(self) -> None:
        correlations = self.correlator.correlate(
            (
                connectivity("gpu-01"),
                connectivity("gpu-02", silenced=True),
                connectivity("gpu-03"),
            ),
            frozenset({"gpu-01", "gpu-02", "gpu-03"}),
        )

        self.assertEqual(correlations, ())

    def test_excludes_conditions_that_are_not_actionable(self) -> None:
        # An acknowledged condition is not silenced but is not actionable
        # either; items without the key stay included for compatibility.
        correlations = self.correlator.correlate(
            (
                connectivity("gpu-01"),
                connectivity("gpu-02", actionable=False),
                connectivity("gpu-03"),
            ),
            frozenset({"gpu-01", "gpu-02", "gpu-03"}),
        )

        self.assertEqual(correlations, ())


if __name__ == "__main__":
    unittest.main()


def connectivity_since(host: str, first_observed_at: str) -> dict[str, object]:
    return {**connectivity(host), "firstObservedAt": first_observed_at}


class SimultaneousLossCorrelatorTests(unittest.TestCase):
    """Live evidence: a home uplink whose relay reconnects every evening takes
    most of the fleet down within one collection cycle; per-node incidents
    are true but the explanation is the shared path."""

    def setUp(self) -> None:
        self.fleet = frozenset({"gpu-01", "gpu-02", "gpu-03", "gpu-04"})
        self.correlator = SimultaneousLossCorrelator()

    def test_groups_most_of_the_fleet_failing_within_the_window(self) -> None:
        incidents = [
            connectivity_since("gpu-01", "2026-09-05T12:00:05Z"),
            connectivity_since("gpu-02", "2026-09-05T12:00:20Z"),
            connectivity_since("gpu-03", "2026-09-05T12:01:40Z"),
        ]
        (group,) = self.correlator.correlate(incidents, self.fleet)
        self.assertEqual(group["kind"], "simultaneous_connectivity_loss")
        self.assertEqual(group["hosts"], ["gpu-01", "gpu-02", "gpu-03"])
        self.assertEqual(
            group["correlationKey"], "simultaneous-loss:2026-09-05T12:00:05Z"
        )
        self.assertIsNone(group["anchor"])
        self.assertEqual(group["confidence"], "possible")
        self.assertIn("3 of 4 monitored nodes", str(group["detail"]))

    def test_needs_three_hosts_half_the_fleet_and_one_window(self) -> None:
        two = [
            connectivity_since("gpu-01", "2026-09-05T12:00:05Z"),
            connectivity_since("gpu-02", "2026-09-05T12:00:06Z"),
        ]
        self.assertEqual(self.correlator.correlate(two, self.fleet), ())
        # Three of ten is not a fleet-wide event.
        big_fleet = frozenset(f"gpu-{index:02d}" for index in range(1, 11))
        three = two + [connectivity_since("gpu-03", "2026-09-05T12:00:07Z")]
        self.assertEqual(self.correlator.correlate(three, big_fleet), ())
        # Three hosts that failed hours apart are three separate outages.
        spread = [
            connectivity_since("gpu-01", "2026-09-05T09:00:00Z"),
            connectivity_since("gpu-02", "2026-09-05T12:00:00Z"),
            connectivity_since("gpu-03", "2026-09-05T15:00:00Z"),
        ]
        self.assertEqual(self.correlator.correlate(spread, self.fleet), ())
        # A silenced or non-actionable loss does not count, nor does one that
        # never recorded when it began.
        muted = three[:2] + [
            connectivity("gpu-03", silenced=True),
            connectivity("gpu-04", actionable=False),
        ]
        self.assertEqual(self.correlator.correlate(muted, self.fleet), ())
        undated = two + [connectivity("gpu-03")]
        self.assertEqual(self.correlator.correlate(undated, self.fleet), ())

    def test_densest_window_wins_when_losses_straddle_it(self) -> None:
        # One old outage plus three fresh losses: the fresh cluster is the
        # group, the old one is left to stand on its own.
        incidents = [
            connectivity_since("gpu-01", "2026-09-05T06:00:00Z"),
            connectivity_since("gpu-02", "2026-09-05T12:00:00Z"),
            connectivity_since("gpu-03", "2026-09-05T12:00:30Z"),
            connectivity_since("gpu-04", "2026-09-05T12:01:00Z"),
        ]
        (group,) = self.correlator.correlate(incidents, self.fleet)
        self.assertEqual(group["hosts"], ["gpu-02", "gpu-03", "gpu-04"])


class CompositeIncidentCorrelatorTests(unittest.TestCase):
    def test_fleet_wide_loss_subsumes_configured_paths_inside_it(self) -> None:
        topology = ConnectionTopologyConfig(
            root="monitor",
            links=(
                TopologyLinkConfig("monitor", "gateway", "ssh"),
                TopologyLinkConfig("gateway", "gpu-01", "ssh"),
                TopologyLinkConfig("gateway", "gpu-02", "ssh"),
                TopologyLinkConfig("monitor", "gpu-03", "ssh"),
                TopologyLinkConfig("monitor", "gpu-04", "ssh"),
            ),
        )
        correlator = create_incident_correlator(topology)
        fleet = frozenset({"gpu-01", "gpu-02", "gpu-03", "gpu-04"})
        everything = [
            connectivity_since(host, f"2026-09-05T12:00:{second:02d}Z")
            for second, host in enumerate(sorted(fleet))
        ]
        kinds = [item["kind"] for item in correlator.correlate(everything, fleet)]
        self.assertEqual(kinds, ["simultaneous_connectivity_loss"])

        # Only the gateway's children failed, hours apart: no fleet event, the
        # configured path explains them.
        path_only = [
            connectivity_since("gpu-01", "2026-09-05T09:00:00Z"),
            connectivity_since("gpu-02", "2026-09-05T15:00:00Z"),
        ]
        kinds = [item["kind"] for item in correlator.correlate(path_only, fleet)]
        self.assertEqual(kinds, ["configured_shared_path"])

        # Without a topology the composite still detects the fleet-wide loss.
        bare = create_incident_correlator(None)
        self.assertEqual(
            [item["kind"] for item in bare.correlate(everything, fleet)],
            ["simultaneous_connectivity_loss"],
        )
        self.assertEqual(bare.correlate(path_only, fleet), ())
