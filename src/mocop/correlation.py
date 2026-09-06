from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .config import ConnectionTopologyConfig
from .models import epoch_seconds

# A relay or the monitor's own uplink failing takes many hosts down within
# one collection cycle plus probe timeouts; genuine node failures are spread
# out. The window is generous enough for a 60-second cadence and still reads
# as "at once" to an operator.
SIMULTANEOUS_LOSS_WINDOW_SECONDS = 120.0
SIMULTANEOUS_LOSS_MIN_HOSTS = 3
SIMULTANEOUS_LOSS_MIN_SHARE = 0.5


class IncidentCorrelator(Protocol):
    def correlate(
        self,
        active_incidents: Sequence[dict[str, object]],
        monitored_hosts: frozenset[str],
    ) -> tuple[dict[str, object], ...]: ...


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
        affected = set(_actionable_connectivity(active_incidents, monitored_hosts))
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


def _actionable_connectivity(
    active_incidents: Sequence[dict[str, object]], monitored_hosts: frozenset[str]
) -> dict[str, dict[str, object]]:
    return {
        str(item["host"]): item
        for item in active_incidents
        if item.get("category") == "connectivity"
        and not item.get("silenced", False)
        and item.get("actionable", True)
        and item.get("host") in monitored_hosts
    }


class SimultaneousLossCorrelator:
    """Groups connectivity losses that began within one short window.

    When at least ``SIMULTANEOUS_LOSS_MIN_HOSTS`` hosts and at least half of
    the monitored fleet lost connectivity within
    ``SIMULTANEOUS_LOSS_WINDOW_SECONDS`` of each other, the likely cause is a
    path they share — the monitor's own uplink or a relay — rather than the
    nodes, so the group is offered as one possible correlation. Nothing is
    claimed about which shared path; that needs the configured topology.
    """

    def correlate(
        self,
        active_incidents: Sequence[dict[str, object]],
        monitored_hosts: frozenset[str],
    ) -> tuple[dict[str, object], ...]:
        affected = _actionable_connectivity(active_incidents, monitored_hosts)
        if not monitored_hosts or len(affected) < SIMULTANEOUS_LOSS_MIN_HOSTS:
            return ()
        losses = sorted(
            (epoch, host)
            for host, item in affected.items()
            if (epoch := epoch_seconds(item.get("firstObservedAt"))) is not None
        )
        # The densest window: for each loss as the earliest member, count the
        # losses that follow it inside the window and keep the largest group.
        best: list[str] = []
        for start, (epoch, _host) in enumerate(losses):
            group = [
                member
                for member_epoch, member in losses[start:]
                if member_epoch - epoch <= SIMULTANEOUS_LOSS_WINDOW_SECONDS
            ]
            if len(group) > len(best):
                best = group
        if len(best) < SIMULTANEOUS_LOSS_MIN_HOSTS or len(
            best
        ) < SIMULTANEOUS_LOSS_MIN_SHARE * len(monitored_hosts):
            return ()
        hosts = sorted(best)
        first = min(str(affected[host]["firstObservedAt"]) for host in hosts)
        return (
            {
                "correlationKey": f"simultaneous-loss:{first}",
                "kind": "simultaneous_connectivity_loss",
                "anchor": None,
                "hosts": hosts,
                "severity": "critical",
                "confidence": "possible",
                "detail": (
                    f"{len(hosts)} of {len(monitored_hosts)} monitored nodes lost "
                    f"connectivity within {int(SIMULTANEOUS_LOSS_WINDOW_SECONDS)} "
                    "seconds of each other; the monitor's own uplink or a shared "
                    "relay is the likely cause"
                ),
            },
        )


class CompositeIncidentCorrelator:
    """Simultaneous loss first; configured paths explain what remains.

    A fleet-wide loss already accounts for every path group inside it, so
    those groups are dropped; a configured group with hosts outside the loss
    stays, because it explains something the fleet-wide event does not.
    """

    def __init__(self, correlators: Sequence[IncidentCorrelator]) -> None:
        self._simultaneous = SimultaneousLossCorrelator()
        self._correlators = tuple(correlators)

    def correlate(
        self,
        active_incidents: Sequence[dict[str, object]],
        monitored_hosts: frozenset[str],
    ) -> tuple[dict[str, object], ...]:
        fleet = self._simultaneous.correlate(active_incidents, monitored_hosts)
        covered = set(fleet[0]["hosts"]) if fleet else set()  # type: ignore[arg-type]
        others = tuple(
            correlation
            for correlator in self._correlators
            for correlation in correlator.correlate(active_incidents, monitored_hosts)
            if not set(correlation["hosts"]) <= covered  # type: ignore[arg-type]
        )
        return fleet + others


def create_incident_correlator(
    topology: ConnectionTopologyConfig | None,
) -> IncidentCorrelator:
    correlators: list[IncidentCorrelator] = []
    if topology is not None:
        correlators.append(TopologyIncidentCorrelator(topology))
    return CompositeIncidentCorrelator(correlators)
