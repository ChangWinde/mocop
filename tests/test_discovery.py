from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mocop.config import MonitorConfig
from mocop.discovery import OpenSshConfigHostSource


class DiscoveryTests(unittest.TestCase):
    def test_discovers_includes_and_ignores_patterns(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        includes = root / "config.d"
        includes.mkdir()
        (root / "config").write_text(
            "Include config.d/*.conf\nHost *\n  ServerAliveInterval 10\nHost direct !blocked wildcard-*\n",
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


if __name__ == "__main__":
    unittest.main()
