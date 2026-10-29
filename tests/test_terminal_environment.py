"""Execute the actual shell preamble against controlled terminfo inventories."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mocop.terminal import REMOTE_COMMAND


class TerminalEnvironmentTests(unittest.TestCase):
    def test_terminal_selection_and_literal_environment_values(self):
        cases = [
            ("xterm-ghostty", "xterm-256color", "xterm-256color"),
            ("xterm-kitty", "xterm-256color", "xterm-256color"),
            ("xterm-ghostty", "xterm-ghostty xterm-256color", "xterm-ghostty"),
            (None, "xterm-256color", "xterm-256color"),
            ("dumb", "dumb xterm-256color", "xterm-256color"),
            ("missing", "xterm", "xterm"),
            ("missing", "vt100", "vt100"),
            ("missing", "", "missing"),
            ("$(touch injected)", "xterm-256color", "xterm-256color"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in {
                "infocmp": 'for entry in $AVAILABLE; do [ "$1" = "$entry" ] && exit 0; done; exit 1',
                "df": 'printf "DISK_TABLE\\n"',
                "login-shell": 'printf "FINAL_TERM=%s ARGS=%s\\n" "${TERM-}" "$*"',
            }.items():
                script = root / name
                script.write_text("#!/bin/sh\n" + body + "\n")
                script.chmod(0o700)
            for initial, available, expected in cases:
                with self.subTest(initial=initial, available=available):
                    env = {
                        **os.environ,
                        "PATH": str(root),
                        "AVAILABLE": available,
                        "SHELL": str(root / "login-shell"),
                    }
                    env.pop("TERM", None)
                    if initial is not None:
                        env["TERM"] = initial
                    result = subprocess.run(
                        ["/bin/sh", "-c", REMOTE_COMMAND],
                        cwd=root,
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"FINAL_TERM={expected} ARGS=-l", result.stdout)
                    self.assertIn("DISK_TABLE", result.stdout)
                    self.assertFalse((root / "injected").exists())
