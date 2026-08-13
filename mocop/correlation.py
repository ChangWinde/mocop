from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .config import ConnectionTopologyConfig


class IncidentCorrelator(Protocol):
    def correlate(
        self,
        active_incidents: Sequence[dict[str, object]],
        monitored_hosts: frozenset[str],
    ) -> tuple[dict[str, object], ...]: ...


class NoopIncidentCorrelator:
    def correlate(
        self,
        active_incidents: Sequence[dict[str, object]],
        monitored_hosts: frozenset[str],
    ) -> tuple[dict[str, object], ...]:
        del active_incidents, monitored_hosts
        return ()


class TopologyIncidentCorrelator:
    """Groups connectivity incidents that share a configured non-root path."""

    def __init__(self, topology: ConnectionTopologyConfig) -> None:
        self._root = topology.root
        children: dict[str, list[str]] = {}
        for link in topology.links:
            children.setdefault(link.source, []).append(link.target)
        self._children = {node: tuple(targets) for node, targets in children.items()}
        self._depths = self._node_depths()

    def correlate(
        self,
        active_incidents: Sequence[dict[str, object]],
        monitored_hosts: frozenset[str],
    ) -> tuple[dict[str, object], ...]:
        affected = {
            str(item["host"])
            for item in active_incidents
            if item.get("category") == "connectivity"
            and not item.get("silenced", False)
            and item.get("actionable", True)
            and item.get("host") in monitored_hosts
        }
        if len(affected) < 2:
            return ()

        descendants = self._monitored_descendants(monitored_hosts)
        candidates = sorted(
            (
                (self._depths[node], node, descendants[node] & affected)
                for node in descendants
                if node != self._root and len(descendants[node] & affected) >= 2
            ),
            key=lambda item: (-item[0], item[1]),
        )
        remaining = set(affected)
        correlations = []
        for _depth, anchor, candidate_hosts in candidates:
            hosts = sorted(candidate_hosts & remaining)
            if len(hosts) < 2:
                continue
            remaining.difference_update(hosts)
            correlations.append(
                {
                    "correlationKey": f"configured-path:{anchor}",
                    "kind": "configured_shared_path",
                    "anchor": anchor,
                    "hosts": hosts,
                    "severity": "critical",
                    "confidence": "possible",
                    "detail": (
                        f"{len(hosts)} unreachable nodes share the configured "
                        f"path through {anchor}"
                    ),
                }
            )
        return tuple(correlations)

    def _node_depths(self) -> dict[str, int]:
        depths = {self._root: 0}
        pending = [self._root]
        while pending:
            node = pending.pop()
            for child in self._children.get(node, ()):
                depths[child] = depths[node] + 1
                pending.append(child)
        return depths

    def _monitored_descendants(
        self, monitored_hosts: frozenset[str]
    ) -> dict[str, set[str]]:
        descendants: dict[str, set[str]] = {}

        def visit(node: str) -> set[str]:
            hosts = {node} if node in monitored_hosts else set()
            for child in self._children.get(node, ()):
                hosts.update(visit(child))
            descendants[node] = hosts
            return hosts

        visit(self._root)
        return descendants


def create_incident_correlator(
    topology: ConnectionTopologyConfig | None,
) -> IncidentCorrelator:
    if topology is None:
        return NoopIncidentCorrelator()
    return TopologyIncidentCorrelator(topology)
