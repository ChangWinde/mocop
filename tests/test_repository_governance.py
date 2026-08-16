from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

import mocop

ROOT = Path(__file__).resolve().parents[1]

TRACKED_ROOT_ENTRIES = {
    ".gitattributes",
    ".githooks",
    ".github",
    ".gitignore",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "docs",
    "examples",
    "mocop",
    "pyproject.toml",
    "tests",
}

# These ceilings are a ratchet over the reviewed 0.9.0 tree, not a general style
# rule. Extract a coherent leaf and lower the budget instead of raising it.
CORE_MODULE_LINE_BUDGETS = {
    "mocop/static/app.js": 6_825,
    "mocop/static/process-search.js": 225,
    "mocop/service.py": 2_750,
    "mocop/web.py": 1_800,
    "mocop/probe.py": 1_625,
    "mocop/config.py": 1_575,
}


class RepositoryGovernanceTests(unittest.TestCase):
    def test_tracked_root_matches_the_documented_layout(self) -> None:
        if not (ROOT / ".git").exists():
            self.skipTest("tracked-root governance requires a Git checkout")
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        entries = {line.split("/", 1)[0] for line in completed.stdout.splitlines()}
        self.assertEqual(entries, TRACKED_ROOT_ENTRIES)

    def test_core_module_line_budgets_do_not_regress(self) -> None:
        for relative, budget in CORE_MODULE_LINE_BUDGETS.items():
            with self.subTest(path=relative):
                lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
                self.assertLessEqual(
                    len(lines),
                    budget,
                    f"{relative} exceeds its {budget}-line extraction boundary",
                )

    def test_readmes_stay_onboarding_sized(self) -> None:
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "docs/locales/zh-CN/README.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(english.splitlines()), 245)
        self.assertLessEqual(len(english.split()), 1_650)
        self.assertLessEqual(len(chinese.splitlines()), 245)

    def test_release_version_is_consistent_and_installable(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(
            r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"$', pyproject, re.MULTILINE
        )
        self.assertIsNotNone(match)
        version = match.group(1)
        self.assertEqual(version, mocop.__version__)

        changelog = (ROOT / "docs/CHANGELOG.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "docs/locales/zh-CN/README.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog)
        self.assertIn(f"mocop.git@v{version}", readme)
        self.assertIn(f"mocop.git@v{version}", chinese)


if __name__ == "__main__":
    unittest.main()
