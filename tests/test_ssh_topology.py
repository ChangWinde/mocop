from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mocop.config import MonitorConfig, SshDiscoveryConfig
from mocop.discovery import HostDiscoverySnapshot, OpenSshConfigHostSource
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
    def _proxy_options_route(self, command: str) -> SshRoute | None:
        with patch("mocop.probe._run_bounded_process") as run:
            run.return_value = _BoundedProcessResult(
                0, "proxyjump none\nproxycommand " + command + "\n", ""
            )
            return OpenSshRouteResolver().resolve(
                "gpu-01",
                config(),
                ("gpu-01", "compute", "bastion", "relay", "edge", "ssh", "none"),
                3,
            )

    def _assert_proxy_options_match_ssh(
        self, command: str, hops: tuple[str, ...] | None, jump: str = "none"
    ) -> None:
        # -G reads options only; /dev/null excludes user and system SSH config.
        completed = subprocess.run(
            ["ssh", "-G", "-F", "/dev/null", *shlex.split(command)[1:]],
            capture_output=True,
            text=True,
            timeout=5,
            env={"LC_ALL": "C"},
        )
        if hops is None:
            self.assertNotEqual(completed.returncode, 0)
            expected = SshRoute("proxycommand", ("proxy-gpu-01",), opaque=True)
        else:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            options = dict(line.split(" ", 1) for line in completed.stdout.splitlines())
            self.assertEqual(options.get("proxyjump", "none"), jump)
            expected = SshRoute("proxycommand", hops)
        self.assertEqual(self._proxy_options_route(command), expected)

    def _proxy_options_snapshot(self, command: str) -> HostDiscoverySnapshot:
        with tempfile.TemporaryDirectory() as directory:
            ssh_config = Path(directory) / "config"
            ssh_config.write_text(
                "Host gpu-01 compute bastion relay\n", encoding="utf-8"
            )

            def resolved(argv, **kwargs):
                proxy = command if argv[-1] == "gpu-01" else "none"
                return _BoundedProcessResult(
                    0, f"proxyjump none\nproxycommand {proxy}\n", ""
                )

            with patch("mocop.probe._run_bounded_process", side_effect=resolved):
                return OpenSshConfigHostSource().discovery(
                    replace(config(), ssh_config=ssh_config)
                )

    def test_ssh_topology_proxy_options_post_destination(self) -> None:
        for command, hops, jump in (
            ("ssh bastion -J relay -W gpu-01:22", ("relay", "bastion"), "relay"),
            (
                "ssh -lcompute bastion -vvJrelay -Wgpu-01:22",
                ("relay", "bastion"),
                "relay",
            ),
            (
                "ssh bastion -oProxyJump=relay -Wgpu-01:22",
                ("relay", "bastion"),
                "relay",
            ),
            ("ssh -Jedge bastion -lcompute -Wgpu-01:22", ("edge", "bastion"), "edge"),
            ("ssh bastion -- -J relay", ("bastion",), "none"),
            ("ssh -- bastion -J relay", ("bastion",), "none"),
            ("ssh bastion uptime -J relay", ("bastion",), "none"),
            ("ssh bastion -v uptime -Jrelay", ("bastion",), "none"),
            ("ssh -Jedge bastion -- -Jcompute", ("edge", "bastion"), "edge"),
            ("ssh bastion -Jrelay -- -Jcompute", ("relay", "bastion"), "relay"),
        ):
            with self.subTest(command=command):
                self._assert_proxy_options_match_ssh(command, hops, jump)

    def test_ssh_topology_proxy_options_first_jump_value_wins(self) -> None:
        for command, hops, jump in (
            (
                "ssh -J bastion -oProxyJump=compute -W gpu-01:22 relay",
                ("bastion", "relay"),
                "bastion",
            ),
            (
                "ssh -oProxyJump=bastion -o ProxyJump=compute relay",
                ("bastion", "relay"),
                "bastion",
            ),
            (
                "ssh -o 'proxyjump = bastion' relay -oProxyJump=compute",
                ("bastion", "relay"),
                "bastion",
            ),
            ("ssh -oProxyJump=none -oProxyJump=compute relay", ("relay",), "none"),
            ("ssh -Jnone -oProxyJump=compute relay", ("relay",), "none"),
            ("ssh -oProxyCommand=none -oProxyJump=compute relay", ("relay",), "none"),
            ("ssh -oProxyCommand=compute -oProxyJump=edge relay", ("relay",), "none"),
            ("ssh -oProxyJump=none -oProxyCommand=compute relay", ("relay",), "none"),
            ("ssh bastion -Jrelay -oProxyJump=compute", ("relay", "bastion"), "relay"),
        ):
            with self.subTest(command=command):
                self._assert_proxy_options_match_ssh(command, hops, jump)

    def test_ssh_topology_proxy_options_conflicting_jump_is_opaque(self) -> None:
        for command in (
            "ssh -Jedge -Jrelay bastion",
            "ssh -J edge -J relay bastion",
            "ssh -oProxyJump=edge -Jrelay bastion",
            "ssh -oProxyJump=none -Jrelay bastion",
            "ssh -oProxyCommand=compute -Jrelay bastion",
            "ssh -oProxyCommand=none -Jrelay bastion",
            "ssh -Jedge bastion -Jrelay",
            "ssh bastion -Jedge -vvJrelay",
            "ssh -oProxyJump=none bastion -Jrelay",
            "ssh -oProxyCommand=none bastion -Jrelay",
        ):
            with self.subTest(command=command):
                self._assert_proxy_options_match_ssh(command, None)

    def test_ssh_topology_proxy_options_discovery_uses_effective_hops(self) -> None:
        for command in (
            "ssh bastion -J relay -W gpu-01:22",
            "ssh -J bastion -oProxyJump=compute -W gpu-01:22 relay",
        ):
            with self.subTest(command=command):
                snapshot = self._proxy_options_snapshot(command)
                self.assertEqual(snapshot.hosts, ("compute", "gpu-01"))
                self.assertEqual(snapshot.eligible_aliases, ("compute", "gpu-01"))
                self.assertEqual(snapshot.infrastructure_hosts, ("bastion", "relay"))

    def test_ssh_topology_proxy_options_separate_and_attached_arguments(self) -> None:
        for option in "lpiFWbcDELRS mwBIeOPQ".replace(" ", ""):
            for argument in (f"-{option} compute", f"-{option}compute"):
                with self.subTest(argument=argument):
                    route = self._proxy_options_route(
                        f"ssh {argument} -W %h:%p bastion"
                    )
                    self.assertEqual(route, SshRoute("proxycommand", ("bastion",)))
        for argument in (
            "-o User=compute",
            "-oUser=compute",
            "-o 'User compute'",
            "-o IdentityFile=compute",
            "-o ProxyCommand=compute",
            "-o 'ProxyCommand ssh compute'",
            "-o Hostname=compute",
            "-Jnone",
            "-oProxyJump=none",
        ):
            with self.subTest(argument=argument):
                self.assertEqual(
                    self._proxy_options_route(f"ssh {argument} bastion"),
                    SshRoute("proxycommand", ("bastion",)),
                )

    def test_ssh_topology_proxy_options_keep_jump_hosts(self) -> None:
        for argument in (
            "-J operator@edge:2200,relay",
            "-Joperator@edge:2200,relay",
            "-o ProxyJump=operator@edge:2200,relay",
            "-oProxyJump=operator@edge:2200,relay",
            "-o 'ProxyJump operator@edge:2200,relay'",
            "-o 'proxyjump = operator@edge:2200,relay'",
            "-o '  ProxyJump = operator@edge:2200,relay  '",
            "-vJedge,relay",
        ):
            with self.subTest(argument=argument):
                self.assertEqual(
                    self._proxy_options_route(
                        f"/usr/bin/ssh -lcompute {argument} -W%h:%p bastion"
                    ),
                    SshRoute("proxycommand", ("edge", "relay", "bastion")),
                )

    def test_ssh_topology_proxy_options_destination_boundary(self) -> None:
        for command in (
            "ssh -vvq -lcompute -- operator@bastion compute",
            "ssh -lcompute bastion echo compute",
            "exec ssh -lcompute bastion compute",
            "ssh -Jedge,edge -oProxyJump=edge,bastion bastion compute",
        ):
            with self.subTest(command=command):
                expected = ("edge", "bastion") if "-J" in command else ("bastion",)
                self.assertEqual(
                    self._proxy_options_route(command),
                    SshRoute("proxycommand", expected),
                )

    def test_ssh_topology_proxy_options_opaque_commands(self) -> None:
        for command in (
            "ssh -l",
            "ssh -J",
            "ssh -Jedge",
            "ssh -oProxyJump=edge",
            "ssh -o",
            "ssh -Z compute bastion",
            "ssh -oUser=compute",
            "nc -x compute %h %p",
            "ssh 'unterminated",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self._proxy_options_route(command),
                    SshRoute("proxycommand", ("proxy-gpu-01",), opaque=True),
                )

    def test_ssh_topology_proxy_options_login_alias_remains_discoverable(self) -> None:
        snapshot = self._proxy_options_snapshot(
            "ssh -l compute -J relay -W %h:%p bastion"
        )
        self.assertEqual(snapshot.hosts, ("compute", "gpu-01"))
        self.assertEqual(snapshot.eligible_aliases, ("compute", "gpu-01"))
        self.assertEqual(snapshot.infrastructure_hosts, ("bastion", "relay"))

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

    @patch("mocop.probe._run_bounded_process")
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

    @patch("mocop.probe._run_bounded_process")
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

    @patch("mocop.probe._run_bounded_process")
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

    def test_word_prefixes_and_model_numbers_do_not_form_groups(self) -> None:
        # ADR-0022 sanctions only numbered fleets: a shared word prefix or an
        # embedded model number is not evidence of one scheduling pool.
        aliases = ("paris-web", "paris-db", "gpu-a100", "gpu-h100")
        resolution = SshRouteResolution(
            known_aliases=aliases,
            routes=tuple((alias, SshRoute("direct")) for alias in aliases),
            failures=(),
            warnings=(),
        )

        projection = SshTopologyPlanner.project("monitor", aliases, resolution)

        self.assertEqual(projection.host_groups, ())

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

    def test_projects_proxy_chains_deeper_than_the_recursion_limit(self) -> None:
        # A pathological operator config may chain ProxyJump through every
        # known alias. Chain expansion must stay iterative: a RecursionError
        # would escape the collector's error boundary and stop all
        # monitoring instead of degrading one route.
        depth = 1500
        aliases = tuple(f"hop-{index:04d}" for index in range(depth))
        routes = [(aliases[0], SshRoute("direct"))]
        routes.extend(
            (aliases[index], SshRoute("proxyjump", (aliases[index - 1],)))
            for index in range(1, depth)
        )
        resolution = SshRouteResolution(
            known_aliases=aliases,
            routes=tuple(routes),
            failures=(),
            warnings=(),
        )

        projection = SshTopologyPlanner.project("monitor", (aliases[-1],), resolution)

        # The display tree stays inside its documented link bound while the
        # grouping projection still reflects the full resolved chain.
        assert projection.topology is not None
        self.assertLessEqual(len(projection.topology.links), 512)
        self.assertEqual(projection.host_groups, ((aliases[-1], aliases[-2][:48]),))
