from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mocop.config import MonitorConfig, SshDiscoveryConfig
from mocop.probe import _BoundedProcessResult
from mocop.ssh_topology import (
    OpenSshRouteResolver,
    SshRoute,
    SshRouteResolution,
    SshTopologyPlanner,
)


def config() -> MonitorConfig:
    return MonitorConfig(
        ssh_config=Path("/tmp/ssh-config"),
        auto_discover=True,
        hosts=(),
        exclude_hosts=frozenset(),
        poll_interval_seconds=5,
        probe_timeout_seconds=12,
        connect_timeout_seconds=5,
        max_workers=4,
        listen_host="127.0.0.1",
        listen_port=8787,
        ssh_discovery=SshDiscoveryConfig(mode="topology"),
    )


class OpenSshRouteResolverTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ssh"), "system OpenSSH client is required")
    def test_system_ssh_g_resolves_jump_and_common_proxycommand(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ssh_config = Path(directory) / "config"
            ssh_config.write_text(
                "Host bastion\n"
                "  HostName 192.0.2.10\n"
                "Host gpu-jump\n"
                "  HostName 192.0.2.20\n"
                "  ProxyJump bastion\n"
                "Host gpu-command\n"
                "  HostName 192.0.2.30\n"
                "  ProxyCommand ssh -W %h:%p bastion\n",
                encoding="utf-8",
            )
            runtime_config = replace(config(), ssh_config=ssh_config)
            aliases = ("bastion", "gpu-command", "gpu-jump")
            resolver = OpenSshRouteResolver()

            jump = resolver.resolve("gpu-jump", runtime_config, aliases, 3)
            command = resolver.resolve("gpu-command", runtime_config, aliases, 3)

        self.assertEqual(jump, SshRoute("proxyjump", ("bastion",)))
        self.assertEqual(command, SshRoute("proxycommand", ("bastion",)))

    @patch("mocop.ssh_topology._run_bounded_process")
    def test_resolves_proxyjump_chain_to_known_aliases(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0,
            "proxyjump operator@bastion:2222,relay\nproxycommand none\n",
            "",
        )

        route = OpenSshRouteResolver().resolve(
            "gpu-01", config(), ("bastion", "gpu-01", "relay"), 3
        )

        self.assertEqual(route, SshRoute("proxyjump", ("bastion", "relay")))
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--", "gpu-01"])

    @patch("mocop.ssh_topology._run_bounded_process")
    def test_extracts_known_alias_from_common_proxycommand(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0,
            "proxyjump none\nproxycommand ssh -q -W 10.0.0.8:22 bastion\n",
            "",
        )

        route = OpenSshRouteResolver().resolve(
            "gpu-01", config(), ("bastion", "gpu-01"), 3
        )

        self.assertEqual(route, SshRoute("proxycommand", ("bastion",)))

    @patch("mocop.ssh_topology._run_bounded_process")
    def test_opaque_proxycommand_never_exposes_command_or_address(self, run) -> None:
        run.return_value = _BoundedProcessResult(
            0,
            "proxyjump none\n"
            "proxycommand nc -x secret.proxy.example:1080 10.0.0.8 22\n",
            "",
        )

        route = OpenSshRouteResolver().resolve("gpu-01", config(), ("gpu-01",), 3)

        self.assertEqual(
            route, SshRoute("proxycommand", ("proxy-gpu-01",), opaque=True)
        )
        self.assertNotIn("secret.proxy.example", repr(route))
        self.assertNotIn("10.0.0.8", repr(route))


class SshTopologyPlannerTests(unittest.TestCase):
    def test_groups_direct_hosts_by_a_shared_alias_prefix(self) -> None:
        resolution = SshRouteResolution(
            known_aliases=("training-1", "training-2", "standalone-0"),
            routes=(
                ("training-1", SshRoute("direct")),
                ("training-2", SshRoute("direct")),
                ("standalone-0", SshRoute("direct")),
            ),
            failures=(),
            warnings=(),
        )

        projection = SshTopologyPlanner.project(
            "monitor", ("training-1", "training-2", "standalone-0"), resolution
        )

        self.assertEqual(
            projection.host_groups,
            (("training-1", "training"), ("training-2", "training")),
        )

    def test_projects_nested_routes_and_groups_by_closest_jump_alias(self) -> None:
        resolution = SshRouteResolution(
            known_aliases=("bastion", "cluster-a", "gpu-01", "gpu-02"),
            routes=(
                ("bastion", SshRoute("direct")),
                ("cluster-a", SshRoute("proxyjump", ("bastion",))),
                (
                    "gpu-01",
                    SshRoute("proxyjump", ("cluster-a",)),
                ),
                (
                    "gpu-02",
                    SshRoute("proxyjump", ("cluster-a",)),
                ),
            ),
            failures=(),
            warnings=(),
        )

        projection = SshTopologyPlanner.project(
            "monitor", ("gpu-01", "gpu-02"), resolution
        )

        self.assertEqual(
            projection.host_groups,
            (("gpu-01", "cluster-a"), ("gpu-02", "cluster-a")),
        )
        self.assertEqual(projection.infrastructure_hosts, ("bastion", "cluster-a"))
        assert projection.topology is not None
        self.assertEqual(
            tuple((link.source, link.target) for link in projection.topology.links),
            (
                ("bastion", "cluster-a"),
                ("cluster-a", "gpu-01"),
                ("cluster-a", "gpu-02"),
                ("monitor", "bastion"),
            ),
        )
