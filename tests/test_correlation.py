from __future__ import annotations

import unittest

from mocop.config import ConnectionTopologyConfig, TopologyLinkConfig
from mocop.correlation import TopologyIncidentCorrelator


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
