from __future__ import annotations

import json
import unittest
from pathlib import Path

from mocop.capacity import CapacityRequest, match_capacity

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "capacity_match.json"


def _normalize(result: dict[str, object]) -> dict[str, object]:
    candidates = result["candidates"]
    assert isinstance(candidates, list)
    return {
        "excludedMaintenance": result["excludedMaintenance"],
        "excludedHealth": result["excludedHealth"],
        "candidates": [
            {
                "host": candidate["host"],
                "model": candidate["model"],
                "total": candidate["total"],
                "available": [gpu["index"] for gpu in candidate["available"]],
                "satisfies": candidate["satisfies"],
                "deficit": candidate["deficit"],
                "minimumFreeMiB": candidate["minimumFreeMiB"],
                "averageUtilization": candidate["averageUtilization"],
            }
            for candidate in candidates
        ],
    }


class CapacityMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_ranks_the_shared_fixture_exactly_like_the_browser_leaf(self) -> None:
        # tests/capacity_match_test.mjs asserts the same `expected` for the
        # same inputs, so the two implementations cannot drift apart.
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                request = CapacityRequest(
                    case["request"]["gpuCount"],
                    case["request"]["minVramGiB"],
                    case["request"]["model"],
                )
                result = match_capacity(
                    self.fixture["servers"],
                    self.fixture["activeConditions"],
                    request,
                    busy_pct=self.fixture["busyPct"],
                    temperature_c=self.fixture["temperatureC"],
                )
                self.assertEqual(_normalize(result), case["expected"])

    def test_response_is_json_ready_and_echoes_the_request(self) -> None:
        result = match_capacity(
            self.fixture["servers"],
            self.fixture["activeConditions"],
            CapacityRequest(2, 24),
            busy_pct=10,
            temperature_c=80,
        )
        json.dumps(result, allow_nan=False)
        self.assertEqual(
            result["request"], {"gpuCount": 2, "minVramGiB": 24, "model": "any"}
        )
        self.assertEqual(result["satisfying"], 2)
        first = result["candidates"][0]
        self.assertEqual(first["host"], "roomier-host")
        self.assertEqual(
            first["available"][2],
            {
                "index": 2,
                "uuid": "GPU-2",
                "freeVramMiB": 80000,
                "utilizationPct": 2,
                "temperatureC": None,
            },
        )
        by_host = {candidate["host"]: candidate for candidate in result["candidates"]}
        # A host without a system sample reports no CPU figure rather than NaN.
        self.assertIsNone(by_host["mixed-host"]["cpuUsagePct"])
        self.assertEqual(by_host["ready-host"]["cpuUsagePct"], 10)

    def test_empty_fleet_matches_nothing(self) -> None:
        result = match_capacity(
            [], [], CapacityRequest(1, 0), busy_pct=10, temperature_c=80
        )
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["satisfying"], 0)


if __name__ == "__main__":
    unittest.main()
