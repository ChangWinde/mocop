from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mocop.config import (
    ConnectionTopologyConfig,
    MonitorConfig,
    SshDiscoveryConfig,
    TopologyLinkConfig,
)
from mocop.discovery import OpenSshConfigHostSource
from mocop.ssh_topology import SshRoute, SshTopologyPlanner


class _RouteResolver:
    def __init__(self, routes: dict[str, SshRoute | None]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def resolve(self, alias, _config, _known_aliases, _timeout_seconds):
        self.calls.append(alias)
        return self.routes.get(alias)


class DiscoveryTests(unittest.TestCase):
    @staticmethod
    def _config(ssh_config: Path) -> MonitorConfig:
        return MonitorConfig(
            ssh_config=ssh_config,
            auto_discover=True,
            hosts=(),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=4,
            listen_host="127.0.0.1",
            listen_port=8787,
        )

    def test_discovers_includes_and_ignores_patterns(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        includes = root / "config.d"
        includes.mkdir()
        (root / "config").write_text(
            f"Include {includes}/*.conf\nHost *\n  ServerAliveInterval 10\n"
            "Host direct !blocked wildcard-*\n",
            encoding="utf-8",
        )
        (includes / "servers.conf").write_text(
            "Host gpu-b gpu_a\nHost ?single\n", encoding="utf-8"
        )
        config = MonitorConfig(
            ssh_config=root / "config",
            auto_discover=True,
            hosts=("manual",),
            exclude_hosts=frozenset({"gpu-b"}),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=4,
            listen_host="127.0.0.1",
            listen_port=8787,
        )

        hosts = OpenSshConfigHostSource().hosts(config)

        self.assertEqual(hosts, ("direct", "gpu_a", "manual"))

    def test_inventory_scan_filters_recognizable_code_hosts_only_from_discovery(
        self,
    ) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ssh_config = Path(directory.name) / "config"
        ssh_config.write_text(
            "Host gpu-01 github github.com corp-gitlab gitlab-work git.bfs gpu-02\n",
            encoding="utf-8",
        )
        config = MonitorConfig(
            ssh_config=ssh_config,
            auto_discover=True,
            hosts=("github", "manual"),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=4,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        source = OpenSshConfigHostSource()

        self.assertEqual(
            source.aliases(config),
            (
                "corp-gitlab",
                "git.bfs",
                "github",
                "github.com",
                "gitlab-work",
                "gpu-01",
                "gpu-02",
            ),
        )
        self.assertEqual(source.hosts(config), ("github", "gpu-01", "gpu-02", "manual"))

    def test_unsafe_ssh_alias_is_skipped_without_stopping_discovery(self) -> None:
        # One exotic literal Host entry (an IPv6 literal here) must not veto
        # monitoring: explicit hosts stay active, safe aliases stay
        # discoverable, and the skip is visible as a warning (ADR-0022).
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ssh_config = Path(directory.name) / "config"
        ssh_config.write_text("Host gpu-01 fe80::1 gpu-02\n", encoding="utf-8")
        config = replace(
            self._config(ssh_config),
            auto_discover=True,
            hosts=("manual",),
        )
        source = OpenSshConfigHostSource()

        self.assertEqual(source.aliases(config), ("gpu-01", "gpu-02"))
        discovery = source.discovery(config)
        self.assertEqual(discovery.hosts, ("gpu-01", "gpu-02", "manual"))
        self.assertEqual(discovery.warnings, ("fe80::1: unsafe ssh alias ignored",))

    def test_group_metadata_does_not_authorize_an_undiscovered_alias(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ssh_config = Path(directory.name) / "config"
        ssh_config.write_text("Host gpu-01\n", encoding="utf-8")
        config = replace(
            self._config(ssh_config),
            host_groups=(("not-in-ssh-config", "Training"),),
        )

        discovery = OpenSshConfigHostSource().discovery(config)

        self.assertEqual(discovery.hosts, ("gpu-01",))
        self.assertEqual(discovery.host_groups, (("not-in-ssh-config", "Training"),))

    def test_topology_discovery_excludes_proxy_aliases_and_infers_groups(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ssh_config = Path(directory.name) / "config"
        ssh_config.write_text(
            "Host bastion gpu-01 gpu-02 github.com\n", encoding="utf-8"
        )
        resolver = _RouteResolver(
            {
                "bastion": SshRoute("direct"),
                "gpu-01": SshRoute("proxyjump", ("bastion",)),
                "gpu-02": SshRoute("proxyjump", ("bastion",)),
            }
        )
        config = replace(
            self._config(ssh_config),
            hosts=("monitor",),
            local_host="monitor",
            ssh_discovery=SshDiscoveryConfig(
                mode="topology", refresh_seconds=300, resolve_timeout_seconds=2
            ),
        )
        source = OpenSshConfigHostSource(SshTopologyPlanner(resolver))

        snapshot = source.discovery(config)

        self.assertEqual(snapshot.hosts, ("gpu-01", "gpu-02", "monitor"))
        self.assertEqual(snapshot.infrastructure_hosts, ("bastion",))
        self.assertEqual(snapshot.eligible_aliases, ("gpu-01", "gpu-02"))
        self.assertEqual(
            snapshot.host_groups,
            (("gpu-01", "bastion"), ("gpu-02", "bastion")),
        )
        self.assertIsNotNone(snapshot.topology)

    def test_explicit_inventory_and_groups_override_topology_inference(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ssh_config = Path(directory.name) / "config"
        ssh_config.write_text("Host bastion gpu-01\n", encoding="utf-8")
        resolver = _RouteResolver(
            {
                "bastion": SshRoute("direct"),
                "gpu-01": SshRoute("proxyjump", ("bastion",)),
            }
        )
        config = replace(
            self._config(ssh_config),
            hosts=("monitor", "bastion", "gpu-01"),
            local_host="monitor",
            host_groups=(("gpu-01", "Training"),),
            topology=ConnectionTopologyConfig(
                "monitor", (TopologyLinkConfig("monitor", "gpu-01", "vpn"),)
            ),
            ssh_discovery=SshDiscoveryConfig(mode="topology"),
        )
        source = OpenSshConfigHostSource(SshTopologyPlanner(resolver))

        snapshot = source.discovery(config)

        self.assertEqual(snapshot.hosts, ("bastion", "gpu-01", "monitor"))
        self.assertEqual(dict(snapshot.host_groups)["gpu-01"], "Training")
        self.assertEqual(snapshot.topology, config.topology)

    def test_topology_resolution_is_cached_until_refresh_deadline(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ssh_config = Path(directory.name) / "config"
        ssh_config.write_text("Host bastion gpu-01\n", encoding="utf-8")
        resolver = _RouteResolver(
            {
                "bastion": SshRoute("direct"),
                "gpu-01": SshRoute("proxyjump", ("bastion",)),
            }
        )
        clock = [10.0]
        config = replace(
            self._config(ssh_config),
            ssh_discovery=SshDiscoveryConfig(
                mode="topology", refresh_seconds=30, resolve_timeout_seconds=2
            ),
        )
        source = OpenSshConfigHostSource(
            SshTopologyPlanner(resolver), monotonic=lambda: clock[0]
        )

        source.discovery(config)
        first_calls = len(resolver.calls)
        source.discovery(config)
        self.assertEqual(len(resolver.calls), first_calls)

        clock[0] = 40.0
        source.discovery(config)
        self.assertGreater(len(resolver.calls), first_calls)

    def test_rejects_option_like_explicit_host(self) -> None:
        config = MonitorConfig(
            ssh_config=Path("/missing"),
            auto_discover=False,
            hosts=("-oProxyCommand=bad",),
            exclude_hosts=frozenset(),
            poll_interval_seconds=5,
            probe_timeout_seconds=12,
            connect_timeout_seconds=5,
            max_workers=1,
            listen_host="127.0.0.1",
            listen_port=8787,
        )
        with self.assertRaisesRegex(ValueError, "host aliases"):
            OpenSshConfigHostSource().hosts(config)

    def test_supports_openssh_equals_and_comment_token_boundaries(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ssh_config = Path(directory.name) / "config"
        ssh_config.write_text(
            "Host=equals\nHost = spaced\nHost hash#value\nHost safe # comment\n",
            encoding="utf-8",
        )
        config = self._config(ssh_config)
        source = OpenSshConfigHostSource()

        # The '#' token never becomes a candidate, but as an unrelated entry
        # it must not stop discovery either; it degrades to a warning.
        self.assertEqual(source.aliases(config), ("equals", "safe", "spaced"))
        self.assertEqual(
            source.discovery(config).warnings,
            ("hash#value: unsafe ssh alias ignored",),
        )

        ssh_config.write_text(
            "Host=equals\nHost = spaced\nHost safe # comment\n", encoding="utf-8"
        )
        self.assertEqual(
            OpenSshConfigHostSource().aliases(config),
            ("equals", "safe", "spaced"),
        )

    def test_does_not_traverse_include_inside_conditional_blocks(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        child = root / "child.conf"
        child.write_text("Host phantom\n", encoding="utf-8")
        ssh_config = root / "config"
        ssh_config.write_text(
            f"Match host never\n  Include {child}\nHost visible\n",
            encoding="utf-8",
        )
        config = self._config(ssh_config)

        self.assertEqual(OpenSshConfigHostSource().aliases(config), ("visible",))

    def test_relative_include_uses_the_user_ssh_directory(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        custom = root / "custom"
        custom.mkdir()
        (custom / "child.conf").write_text("Host wrong-base\n", encoding="utf-8")
        ssh_config = custom / "config"
        ssh_config.write_text("Include child.conf\nHost direct\n", encoding="utf-8")
        config = self._config(ssh_config)

        self.assertEqual(OpenSshConfigHostSource().aliases(config), ("direct",))

    def test_invalid_include_home_is_a_bounded_discovery_error(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ssh_config = Path(directory.name) / "config"
        ssh_config.write_text(
            "Include ~mocop-user-that-must-not-exist/config\nHost direct\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "Include path is invalid"):
            OpenSshConfigHostSource().aliases(self._config(ssh_config))

    def test_non_regular_ssh_config_is_rejected_without_blocking(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fifo = Path(directory.name) / "config"
        os.mkfifo(fifo)

        with self.assertRaisesRegex(ValueError, "not a regular file"):
            OpenSshConfigHostSource().aliases(self._config(fifo))


if __name__ == "__main__":
    unittest.main()
