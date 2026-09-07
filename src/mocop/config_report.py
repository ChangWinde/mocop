"""The ``mocop config check`` report: one structure, two renderers.

It names environment variables and whether they are set, never their values,
so the output is safe to paste into an issue.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import MonitorConfig


def _environment_state(name: str) -> str:
    """Report whether a referenced environment variable is set, never its value."""
    return "set" if os.environ.get(name) else "unset"


def config_check_report(config_path: Path, config: MonitorConfig) -> dict[str, object]:
    """One report feeds both renderers; it names environment variables, never values."""
    if config.topology is not None:
        topology: dict[str, object] = {
            "source": "configured",
            "links": len(config.topology.links),
        }
    elif config.ssh_discovery.mode == "topology":
        topology = {"source": "resolved", "links": None}
    else:
        topology = {"source": "none", "links": None}
    return {
        "configPath": str(config_path),
        "hosts": len(config.hosts),
        "localHost": config.local_host,
        "sshDiscovery": {
            "mode": config.ssh_discovery.mode,
            "refreshSeconds": config.ssh_discovery.refresh_seconds,
            "resolveTimeoutSeconds": config.ssh_discovery.resolve_timeout_seconds,
        },
        "persistence": config.persistence.enabled,
        "workloads": config.workloads.mode,
        "topology": topology,
        "updates": config.updates.mode,
        "listen": {"host": config.listen_host, "port": config.listen_port},
        "webhooks": [
            {
                "name": webhook.name,
                "urlEnv": webhook.url_env,
                "urlEnvState": _environment_state(webhook.url_env),
                "secretEnv": webhook.secret_env,
                "secretEnvState": (
                    _environment_state(webhook.secret_env)
                    if webhook.secret_env is not None
                    else None
                ),
            }
            for webhook in config.webhooks
        ],
    }


def print_config_check_report(report: dict[str, object]) -> None:
    print(f"configuration OK: {report['configPath']}")
    local_note = f" (local: {report['localHost']})" if report["localHost"] else ""
    print(f"hosts: {report['hosts']}{local_note}")
    discovery = report["sshDiscovery"]
    assert isinstance(discovery, dict)
    print(
        f"ssh discovery: {discovery['mode']} "
        f"(refresh {discovery['refreshSeconds']}s, "
        f"resolve timeout {discovery['resolveTimeoutSeconds']:g}s)"
    )
    print(f"persistence: {'enabled' if report['persistence'] else 'disabled'}")
    print(f"workloads: {report['workloads']}")
    print(f"updates: {report['updates']}")
    topology = report["topology"]
    assert isinstance(topology, dict)
    if topology["source"] == "configured":
        print(f"topology: configured ({topology['links']} links)")
    elif topology["source"] == "resolved":
        print("topology: resolved from SSH at runtime")
    else:
        print("topology: none")
    webhooks = report["webhooks"]
    assert isinstance(webhooks, list)
    if not webhooks:
        print("webhooks: none")
        return
    print(f"webhooks: {len(webhooks)}")
    for webhook in webhooks:
        references = [f"url_env {webhook['urlEnv']} ({webhook['urlEnvState']})"]
        if webhook["secretEnv"] is not None:
            references.append(
                f"secret_env {webhook['secretEnv']} ({webhook['secretEnvState']})"
            )
        print(f"  {webhook['name']}: {', '.join(references)}")
