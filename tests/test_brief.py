from __future__ import annotations

import unittest

from mocop.brief import build_brief, fleet_status, render_brief

GENERATED_AT = "2026-09-07T09:00:00Z"


def _stats(**overrides: object) -> dict[str, object]:
    stats: dict[str, object] = {
        "servers": 3,
        "onlineServers": 2,
        "staleServers": 0,
        "maintenanceServers": 1,
        "actionableIssueServers": 1,
        "actionableCriticalIncidents": 1,
        "gpus": 12,
        "busyGpus": 7,
        "memoryTotalMiB": 100_000.0,
        "memoryUsedMiB": 42_500.0,
    }
    stats.update(overrides)
    return stats


def _snapshot() -> dict[str, object]:
    return {
        "generatedAt": "2026-09-07T08:59:30Z",
        "stats": _stats(),
        "servers": [
            {"host": "gpu-01", "status": "online", "maintenance": None},
            {
                "host": "gpu-02",
                "status": "unreachable",
                "maintenance": {"until": "2026-09-07T12:00:00Z", "reason": "firmware"},
            },
            {"host": "gpu-03", "status": "online", "maintenance": None},
        ],
    }


def _condition(
    host: str,
    key: str,
    *,
    severity: str = "critical",
    actionable: bool = True,
    first: str = "2026-09-07T06:00:00Z",
    value: float | None = 97.0,
    threshold: float | None = 85.0,
    detail: str | None = None,
    group_key: str | None = None,
) -> dict[str, object]:
    return {
        "host": host,
        "conditionKey": key,
        "category": key.split(":")[0],
        "resource": key.split(":")[-1],
        "severity": severity,
        "value": value,
        "threshold": threshold,
        "detail": detail,
        "groupKey": group_key,
        "firstObservedAt": first,
        "actionable": actionable,
        "diagnosis": {"title": f"{key} needs attention"},
    }


def _event(
    host: str, key: str, state: str, observed_at: str, event_id: int
) -> dict[str, object]:
    return {
        "host": host,
        "conditionKey": key,
        "category": key.split(":")[0],
        "resource": key.split(":")[-1],
        "severity": "critical",
        "value": None,
        "threshold": None,
        "observedAt": observed_at,
        "detail": None,
        "groupKey": None,
        "eventId": event_id,
        "state": state,
    }


def _incidents(
    events: list[dict[str, object]] | None = None, capacity: int = 500
) -> dict[str, object]:
    return {
        "version": 7,
        "active": [
            _condition("gpu-01", "disk:/dev/sda1:/data"),
            _condition(
                "gpu-03",
                "gpu_idle_memory:GPU-1",
                severity="warning",
                value=61.0,
                threshold=50.0,
                detail="pid 4242 holds 48 GiB with 0% utilization",
            ),
            _condition("gpu-02", "connectivity", actionable=False, value=None),
        ],
        "events": events if events is not None else [],
        "correlations": [
            {
                "correlationKey": "configured-path:switch-a",
                "kind": "configured_shared_path",
                "anchor": "switch-a",
                "hosts": ["gpu-02", "gpu-04"],
                "severity": "critical",
                "confidence": "possible",
                "detail": "2 unreachable nodes share the configured path through switch-a",
            }
        ],
        "eventCapacity": capacity,
    }


def _capacity() -> dict[str, object]:
    def gpu(index: int, free: float) -> dict[str, object]:
        return {
            "index": index,
            "uuid": f"GPU-{index}",
            "freeVramMiB": free,
            "utilizationPct": 0.0,
            "temperatureC": 30.0,
        }

    return {
        "candidates": [
            {"host": "gpu-01", "model": "H100", "total": 8, "available": []},
            {
                "host": "gpu-03",
                "model": "H100",
                "total": 4,
                "available": [gpu(0, 81_000.0), gpu(2, 40_960.0)],
            },
        ],
        "satisfying": 1,
        "excludedMaintenance": 1,
        "excludedHealth": 0,
    }


def _usage(source: str | None = "history") -> dict[str, object]:
    usage: dict[str, object] = {
        "generatedAt": GENERATED_AT,
        "sinceAt": "2026-09-06T09:00:00Z",
        "totalGpuSeconds": 36_000.0 * 4,
        "owners": [
            {
                "owner": "alice",
                "gpuSeconds": 36_000.0 * 3,
                "idleSeconds": 36_000.0 * 2,
                "idleShare": 0.6667,
                "hosts": ["gpu-01", "gpu-03"],
                "gpus": 6,
            },
            {
                "owner": "bob",
                "gpuSeconds": 36_000.0,
                "idleSeconds": 3_600.0,
                "idleShare": 0.1,
                "hosts": ["gpu-01"],
                "gpus": 2,
            },
        ],
    }
    if source is not None:
        usage["source"] = source
        usage["coveredFromAt"] = "2026-09-06T10:00:00Z"
    return usage


def _utilization() -> dict[str, object]:
    return {
        "fleet": [
            {
                "hour": "2026-09-07T07:00:00Z",
                "gpus": 12,
                "samples": 100,
                "busyShare": 0.5,
            },
            {
                "hour": "2026-09-07T08:00:00Z",
                "gpus": 12,
                "samples": 300,
                "busyShare": 0.9,
            },
        ]
    }


def _brief(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "capacity": _capacity(),
        "usage": _usage(),
        "utilization": _utilization(),
        "window_hours": 24,
        "generated_at": GENERATED_AT,
    }
    arguments.update(overrides)
    incidents = arguments.pop("incidents", _incidents())
    return build_brief(_snapshot(), incidents, **arguments)  # type: ignore[arg-type]


class FleetStatusTests(unittest.TestCase):
    def test_follows_the_dashboard_badge_rule(self) -> None:
        self.assertEqual(fleet_status(_stats()), "critical")
        self.assertEqual(
            fleet_status(_stats(actionableCriticalIncidents=0)), "attention"
        )
        self.assertEqual(
            fleet_status(
                _stats(actionableCriticalIncidents=0, actionableIssueServers=0)
            ),
            "maintenance",
        )
        self.assertEqual(
            fleet_status(
                _stats(
                    actionableCriticalIncidents=0,
                    actionableIssueServers=0,
                    maintenanceServers=0,
                )
            ),
            "healthy",
        )
        self.assertEqual(fleet_status(_stats(servers=0)), "unconfigured")


class BuildBriefTests(unittest.TestCase):
    def test_window_and_fleet_come_from_the_snapshot(self) -> None:
        brief = _brief()
        self.assertEqual(brief["generatedAt"], GENERATED_AT)
        self.assertEqual(brief["sinceAt"], "2026-09-06T09:00:00Z")
        self.assertEqual(brief["windowHours"], 24)
        self.assertEqual(brief["status"], "critical")
        self.assertEqual(
            brief["fleet"],
            {
                "hosts": 3,
                "online": 2,
                "offline": ["gpu-02"],
                "stale": 0,
                "maintenance": 1,
                "gpus": 12,
                "busyGpus": 7,
                "idleGpus": 5,
                "gpuMemoryUsedPct": 42.5,
            },
        )
        self.assertEqual(
            brief["maintenance"],
            [{"host": "gpu-02", "reason": "firmware", "until": "2026-09-07T12:00:00Z"}],
        )

    def test_attention_lists_actionable_conditions_and_correlations(self) -> None:
        attention = _brief()["attention"]
        self.assertEqual(attention["active"], 3)
        self.assertEqual(attention["critical"], 2)
        self.assertEqual(attention["actionable"], 2)
        self.assertEqual(attention["actionableCritical"], 1)
        self.assertEqual(attention["silenced"], 1)
        self.assertEqual(attention["hosts"], 2)
        self.assertEqual(attention["byCategory"], {"disk": 1, "gpu_idle_memory": 1})
        self.assertEqual(
            [item["conditionKey"] for item in attention["items"]],
            ["disk:/dev/sda1:/data", "gpu_idle_memory:GPU-1"],
        )
        self.assertEqual(attention["entries"], 2)
        self.assertEqual(
            attention["items"][0],
            {
                "hosts": ["gpu-01"],
                "conditionKey": "disk:/dev/sda1:/data",
                "groupKey": None,
                "category": "disk",
                "resource": "/data",
                "severity": "critical",
                "value": 97.0,
                "threshold": 85.0,
                "detail": None,
                "belowThreshold": False,
                "firstObservedAt": "2026-09-07T06:00:00Z",
                "title": "disk:/dev/sda1:/data needs attention",
            },
        )
        self.assertEqual(
            attention["correlations"],
            [
                {
                    "kind": "configured_shared_path",
                    "anchor": "switch-a",
                    "hosts": ["gpu-02", "gpu-04"],
                    "detail": (
                        "2 unreachable nodes share the configured path through switch-a"
                    ),
                }
            ],
        )

    def test_shared_filesystems_collapse_into_one_entry(self) -> None:
        export = "nfs|10.0.0.9:/export/datasets"
        incidents = _incidents()
        incidents["active"] = [
            _condition(
                "gpu-05", "disk:10.0.0.9:/export/datasets:/data", group_key=export
            ),
            _condition(
                "gpu-01",
                "disk:10.0.0.9:/export/datasets:/data",
                severity="warning",
                value=99.5,
                first="2026-09-06T20:00:00Z",
                detail="grew 40 GiB in an hour",
                group_key=export,
            ),
            _condition("gpu-03", "disk:/dev/nvme0n1:/scratch", value=91.0),
            _condition(
                "gpu-02",
                "disk:10.0.0.9:/export/datasets:/data",
                actionable=False,
                group_key=export,
            ),
        ]
        attention = _brief(incidents=incidents)["attention"]
        self.assertEqual(attention["actionable"], 3)
        self.assertEqual(attention["entries"], 2)
        group, single = attention["items"]
        self.assertEqual(group["hosts"], ["gpu-01", "gpu-05"])
        self.assertIsNone(group["conditionKey"])
        self.assertEqual(group["groupKey"], export)
        self.assertEqual(group["severity"], "critical")
        self.assertEqual(group["value"], 99.5)
        self.assertEqual(group["firstObservedAt"], "2026-09-06T20:00:00Z")
        self.assertIsNone(group["detail"])
        self.assertEqual(single["hosts"], ["gpu-03"])
        self.assertEqual(single["groupKey"], None)
        text = render_brief(_brief(incidents=incidents))
        self.assertIn("  !! 2 hosts (gpu-01 gpu-05) disk /data 99.5/85 · 13h", text)
        self.assertIn("  !! gpu-03 disk /scratch 91/85 · 3h", text)

    def test_changes_count_the_window_and_flag_recurring_conditions(self) -> None:
        events = [
            _event("gpu-02", "connectivity", "resolved", "2026-09-07T08:50:00Z", 9),
            _event("gpu-02", "connectivity", "opened", "2026-09-07T08:40:00Z", 8),
            _event("gpu-02", "connectivity", "resolved", "2026-09-07T05:00:00Z", 7),
            _event("gpu-02", "connectivity", "opened", "2026-09-07T04:00:00Z", 6),
            _event(
                "gpu-01", "disk:/dev/sda1:/data", "escalated", "2026-09-07T03:00:00Z", 5
            ),
            _event("gpu-02", "connectivity", "resolved", "2026-09-07T01:00:00Z", 4),
            _event("gpu-02", "connectivity", "opened", "2026-09-07T00:00:00Z", 3),
            _event(
                "gpu-01", "disk:/dev/sda1:/data", "opened", "2026-09-06T12:00:00Z", 2
            ),
            # Outside the window: neither counted nor part of the recurrence.
            _event("gpu-02", "connectivity", "opened", "2026-09-05T00:00:00Z", 1),
        ]
        changes = _brief(incidents=_incidents(events))["changes"]
        self.assertEqual(changes["coveredFromAt"], "2026-09-06T09:00:00Z")
        self.assertEqual(changes["opened"], 4)
        self.assertEqual(changes["resolved"], 3)
        self.assertEqual(changes["escalated"], 1)
        self.assertEqual(
            changes["recurring"],
            [
                {
                    "host": "gpu-02",
                    "conditionKey": "connectivity",
                    "category": "connectivity",
                    "resource": "connectivity",
                    "openings": 3,
                    "lastState": "resolved",
                }
            ],
        )

    def test_a_full_ring_inside_the_window_reports_partial_coverage(self) -> None:
        events = [
            _event("gpu-02", "connectivity", "opened", "2026-09-07T08:00:00Z", 2),
            _event("gpu-02", "connectivity", "opened", "2026-09-07T07:00:00Z", 1),
        ]
        partial = _brief(incidents=_incidents(events, capacity=2))["changes"]
        self.assertEqual(partial["coveredFromAt"], "2026-09-07T07:00:00Z")
        # The same two events in a ring with room cover the whole window.
        complete = _brief(incidents=_incidents(events, capacity=500))["changes"]
        self.assertEqual(complete["coveredFromAt"], "2026-09-06T09:00:00Z")
        # An empty feed is a quiet window, not a gap.
        quiet = _brief(incidents=_incidents([], capacity=2))["changes"]
        self.assertEqual(quiet["coveredFromAt"], "2026-09-06T09:00:00Z")
        self.assertEqual(quiet["opened"], 0)

    def test_capacity_skips_hosts_without_idle_gpus(self) -> None:
        capacity = _brief()["capacity"]
        self.assertEqual(capacity["idleGpus"], 2)
        self.assertEqual(capacity["hosts"], 1)
        self.assertEqual(capacity["excludedMaintenance"], 1)
        self.assertEqual(
            capacity["topHosts"],
            [
                {
                    "host": "gpu-03",
                    "model": "H100",
                    "available": 2,
                    "total": 4,
                    "minFreeVramGiB": 40.0,
                }
            ],
        )

    def test_usage_weights_busy_share_and_flags_idle_heavy_owners(self) -> None:
        usage = _brief()["usage"]
        self.assertEqual(usage["source"], "history")
        self.assertEqual(usage["coveredFromAt"], "2026-09-06T10:00:00Z")
        # (0.5 * 100 + 0.9 * 300) / 400 = 80%.
        self.assertEqual(usage["busySharePct"], 80.0)
        self.assertEqual(usage["totalGpuHours"], 40.0)
        self.assertEqual(
            usage["owners"],
            [
                {
                    "owner": "alice",
                    "gpuHours": 30.0,
                    "idleSharePct": 66.7,
                    "gpus": 6,
                    "hosts": 2,
                    "idleHeavy": True,
                },
                {
                    "owner": "bob",
                    "gpuHours": 10.0,
                    "idleSharePct": 10.0,
                    "gpus": 2,
                    "hosts": 1,
                    "idleHeavy": False,
                },
            ],
        )
        self.assertEqual(usage["idleHeavyOwners"], ["alice"])

    def test_usage_falls_back_to_the_memory_timeline_without_history(self) -> None:
        usage = _brief(usage=_usage(source=None), utilization=None)["usage"]
        self.assertEqual(usage["source"], "memory")
        self.assertEqual(usage["coveredFromAt"], "2026-09-06T09:00:00Z")
        self.assertIsNone(usage["busySharePct"])
        self.assertEqual(usage["totalGpuHours"], 40.0)

    def test_readings_inside_the_recovery_margin_are_marked(self) -> None:
        incidents = _incidents()
        incidents["active"] = [
            {
                **_condition("gpu-01", "gpu_memory:GPU-6", value=88.1, threshold=90.0),
                "belowThreshold": True,
            },
            {
                **_condition("gpu-02", "gpu_memory:GPU-0", value=93.0, threshold=90.0),
                "belowThreshold": False,
            },
        ]
        brief = _brief(incidents=incidents)
        self.assertEqual(
            [item["belowThreshold"] for item in brief["attention"]["items"]],
            [True, False],
        )
        text = render_brief(brief)
        self.assertIn(
            "  !! gpu-01 gpu_memory GPU-6 88.1/90 · 3h · below threshold\n", text
        )
        self.assertIn("  !! gpu-02 gpu_memory GPU-0 93/90 · 3h\n", text)

    def test_short_idle_reservations_are_not_flagged(self) -> None:
        usage = _usage()
        usage["owners"] = [
            {
                "owner": "carol",
                "gpuSeconds": 3_600.0 * 2,
                "idleSeconds": 3_600.0 * 2,
                "idleShare": 1.0,
                "hosts": ["gpu-01"],
                "gpus": 1,
            }
        ]
        self.assertEqual(_brief(usage=usage)["usage"]["idleHeavyOwners"], [])


class RenderBriefTests(unittest.TestCase):
    def test_text_reads_worst_first_on_one_screen(self) -> None:
        events = [
            _event("gpu-02", "connectivity", "opened", "2026-09-07T08:40:00Z", 3),
            _event("gpu-02", "connectivity", "opened", "2026-09-07T04:00:00Z", 2),
            _event("gpu-02", "connectivity", "opened", "2026-09-07T00:00:00Z", 1),
        ]
        text = render_brief(_brief(incidents=_incidents(events)))
        lines = text.splitlines()
        self.assertEqual(
            lines[0], "mocop brief 2026-09-07T09:00:00Z · last 24h · fleet CRITICAL"
        )
        self.assertEqual(
            lines[1],
            "hosts 2/3 online, offline: gpu-02, 1 in maintenance · gpus 7/12 busy "
            "· vram 42.5% used",
        )
        self.assertIn(
            "attention: 2 actionable (1 critical) on 2 hosts, 1 silenced", text
        )
        self.assertIn(
            "  !! 2 unreachable nodes share the configured path through switch-a", text
        )
        self.assertIn("  !! gpu-01 disk /data 97/85 · 3h", text)
        self.assertIn(
            "   ! gpu-03 gpu_idle_memory GPU-1 61/50 · 3h · pid 4242 holds 48 GiB "
            "with 0% utilization",
            text,
        )
        self.assertIn(
            "changes since 2026-09-06T09:00:00Z: 3 opened, 0 resolved, 0 escalated",
            text,
        )
        self.assertIn(
            "  ~ gpu-02 connectivity connectivity opened 3x, now opened", text
        )
        self.assertIn(
            "capacity: 2 idle GPUs on 1 host (1 host in maintenance excluded)", text
        )
        self.assertIn("  gpu-03: 2/4 H100 · ≥40 GiB free each", text)
        self.assertIn(
            "usage (history): 40 GPU-hours, fleet busy 80% of the window", text
        )
        self.assertIn("  * alice: 30 GPU-h, idle 66.7%, 6 GPUs on 2 hosts", text)
        self.assertIn("    bob: 10 GPU-h, idle 10%, 2 GPUs on 1 host", text)
        self.assertIn("  * reservation idle at least half the window: alice", text)
        self.assertEqual(lines[-2], "maintenance:")
        self.assertEqual(lines[-1], "  gpu-02 until 2026-09-07T12:00:00Z: firmware")
        self.assertTrue(text.endswith("\n"))

    def test_text_names_the_gaps_instead_of_hiding_them(self) -> None:
        empty = _usage(source=None)
        empty["owners"] = []
        empty["totalGpuSeconds"] = 0.0
        brief = _brief(usage=empty, utilization=None)
        brief["attention"]["entries"] = 14  # type: ignore[index]
        brief["maintenance"] = []
        text = render_brief(brief)
        self.assertIn("  … 12 more in /api/incidents", text)
        self.assertIn("usage (memory): 0 GPU-hours\n", text)
        self.assertNotIn("fleet busy", text)
        self.assertNotIn("maintenance:", text)
        self.assertFalse(text.endswith("\n\n"))


if __name__ == "__main__":
    unittest.main()
