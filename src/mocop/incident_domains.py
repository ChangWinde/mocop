"""Telemetry domains: which parts of a sample a condition's recovery needs.

A resource condition may only recover on a sample that actually observed the
telemetry it depends on; a probe that failed, or a GPU query that returned
nothing, is a blind spot rather than good news. The policy reports which
domains a sample observed, and the tracker asks here whether a condition's
domains are among them.
"""

from __future__ import annotations

_SYSTEM_CATEGORIES = frozenset({"cpu", "memory", "swap", "disk", "pressure"})
_GPU_HEALTH_CATEGORIES = frozenset({"gpu_ecc", "gpu_memory_repair", "gpu_slowdown"})


def condition_domains(category: str, key: str) -> tuple[str, ...]:
    """Telemetry domains a condition needs before its recovery may advance."""
    if category == "cpu":
        return ("system", "system_cpu")
    if category == "pressure":
        return ("system", key)
    if category in _SYSTEM_CATEGORIES:
        return ("system",)
    if category in {"gpu_availability", "gpu_count"}:
        return ("gpu_query",)
    identity = key.partition(":")[2]
    if category in _GPU_HEALTH_CATEGORIES:
        return (f"gpu_present:{identity}", f"gpu_health:{identity}")
    if category == "gpu_processes":
        return ("gpu_processes",)
    if category == "gpu_temperature":
        return (f"gpu_present:{identity}", f"gpu_temperature:{identity}")
    if category == "gpu_memory":
        return (f"gpu_present:{identity}", f"gpu_memory:{identity}")
    if category == "gpu_idle_memory":
        return (
            f"gpu_present:{identity}",
            f"gpu_memory:{identity}",
            f"gpu_utilization:{identity}",
        )
    return ()


_PER_IDENTITY_GPU_DOMAINS = frozenset(
    {"gpu_present", "gpu_health", "gpu_temperature", "gpu_memory", "gpu_utilization"}
)


def telemetry_unknown(
    category: str,
    key: str,
    observed_domains: frozenset[str],
) -> bool:
    """True when the sample carried no fresh telemetry for this condition."""
    missing = [
        domain
        for domain in condition_domains(category, key)
        if domain not in observed_domains
    ]
    if not missing:
        return False
    # A fully observed GPU inventory is authoritative about absence: when a
    # device identity has left a complete inventory (a replaced or renumbered
    # card), its per-identity domains can never be observed again. Freezing
    # would pin the ghost condition and its counts forever, so recovery may
    # advance instead. A failed GPU query never reaches this branch because
    # it does not observe ``gpu_inventory``.
    if "gpu_inventory" in observed_domains:
        identities = set()
        for domain in missing:
            prefix, _, identity = domain.partition(":")
            if prefix not in _PER_IDENTITY_GPU_DOMAINS:
                return True
            identities.add(identity)
        if all(
            f"gpu_present:{identity}" not in observed_domains for identity in identities
        ):
            return False
    return True
