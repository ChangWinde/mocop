from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mocop.config import MonitorConfig
from mocop.discovery import OpenSshConfigHostSource


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

        with self.assertRaisesRegex(ValueError, "hash#value"):
            OpenSshConfigHostSource().aliases(config)

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
