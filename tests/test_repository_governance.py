from __future__ import annotations

import fnmatch
import re
import subprocess
import unittest
from pathlib import Path

import mocop
from mocop.static_assets import STATIC_ROUTES

ROOT = Path(__file__).resolve().parents[1]

TRACKED_ROOT_ENTRIES = {
    ".gitattributes",
    ".githooks",
    ".github",
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "docs",
    "examples",
    "pyproject.toml",
    "src",
    "tests",
}

# These ceilings are a ratchet over the reviewed tree, not a general style
# rule. Extract a coherent leaf and lower the budget instead of raising it.
CORE_MODULE_LINE_BUDGETS = {
    "src/mocop/static/app.js": 5_770,
    "src/mocop/static/owner-usage.js": 150,
    "src/mocop/static/incident-text.js": 150,
    "src/mocop/static/diagnosis-text.js": 110,
    "src/mocop/static/attention.js": 110,
    "src/mocop/static/attention-groups.js": 110,
    "src/mocop/static/background-asset.js": 350,
    "src/mocop/static/api-contracts.js": 300,
    "src/mocop/static/keyed-loader.js": 125,
    "src/mocop/static/capacity-match.js": 150,
    "src/mocop/static/capacity-watch.js": 200,
    "src/mocop/static/csv-export.js": 100,
    "src/mocop/static/dashboard-auth.js": 150,
    "src/mocop/static/format.js": 150,
    "src/mocop/static/gpu-tasks.js": 175,
    "src/mocop/static/process-search.js": 225,
    "src/mocop/static/update-pill.js": 175,
    "src/mocop/service.py": 2_150,
    "src/mocop/process_transitions.py": 150,
    "src/mocop/fleet_stats.py": 125,
    "src/mocop/telemetry_points.py": 250,
    "src/mocop/usage.py": 250,
    "src/mocop/usage_views.py": 110,
    "src/mocop/reports.py": 275,
    "src/mocop/occupancy.py": 175,
    "src/mocop/web.py": 1_275,
    "src/mocop/brief.py": 500,
    "src/mocop/web_auth.py": 100,
    "src/mocop/api_manifest.py": 285,
    "src/mocop/api_describe.py": 100,
    "src/mocop/api_schema.py": 275,
    "src/mocop/probe.py": 1_475,
    "src/mocop/ssh_failures.py": 125,
    "src/mocop/config.py": 300,
    "src/mocop/maintenance.py": 100,
    "src/mocop/config_loader.py": 1_100,
    "src/mocop/config_integrations.py": 275,
    "src/mocop/persistence.py": 800,
    "src/mocop/persistence_api.py": 100,
    "src/mocop/persistence_rollups.py": 200,
    "src/mocop/persistence_transitions.py": 175,
    "src/mocop/persistence_restore.py": 350,
    "src/mocop/persistence_schema.py": 200,
    "src/mocop/doctor.py": 985,
    "src/mocop/__main__.py": 620,
    "src/mocop/config_report.py": 100,
    "src/mocop/cli_arguments.py": 290,
    "src/mocop/cli_client_arguments.py": 125,
    "src/mocop/incidents.py": 770,
    "src/mocop/incident_health.py": 90,
    "src/mocop/incident_types.py": 125,
    "src/mocop/incident_domains.py": 100,
    "src/mocop/notifications.py": 560,
    "src/mocop/webhook_transport.py": 375,
}


def _contrast(
    text: tuple[float, float, float], surface: tuple[float, float, float]
) -> float:
    """WCAG 2 contrast ratio of two sRGB colors given as 0-255 channels."""

    def luminance(rgb: tuple[float, float, float]) -> float:
        channels = []
        for channel in rgb:
            share = channel / 255
            channels.append(
                share / 12.92 if share <= 0.03928 else ((share + 0.055) / 1.055) ** 2.4
            )
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    first, second = luminance(text), luminance(surface)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


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

    def test_every_browser_leaf_is_routed_loaded_and_tested(self) -> None:
        # A leaf that is not routed or not loaded before app.js breaks the
        # dashboard at runtime; a leaf without a Node contract test breaks the
        # ADR-0021 boundary. The route table, the HTML, and the test directory
        # are compared with the directory listing, so none of them can drift.
        static = ROOT / "src" / "mocop" / "static"
        leaves = sorted(path.name for path in static.glob("*.js"))
        routed = {filename for filename, _type in STATIC_ROUTES.values()}
        html = (static / "index.html").read_text(encoding="utf-8")
        loaded = re.findall(r'<script src="/([^"]+\.js)" defer></script>', html)
        self.assertEqual(sorted(loaded), leaves)
        self.assertEqual(
            loaded[-1], "app.js", "app.js consumes every leaf, so it loads last"
        )
        for leaf in leaves:
            with self.subTest(leaf=leaf):
                self.assertIn(leaf, routed)
                if leaf == "app.js":
                    continue
                contract = (
                    ROOT
                    / "tests"
                    / f"{leaf.removesuffix('.js').replace('-', '_')}_test.mjs"
                )
                self.assertTrue(contract.is_file(), f"{leaf} has no Node contract test")

    def test_every_routed_asset_ships_in_the_wheel(self) -> None:
        # The route table serves files from the installed package, so a routed
        # file that the package-data globs do not cover would 404 in every
        # installation while passing the source tree's tests.
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(
            r'^"mocop\.static" = \[([^\]]*)\]', pyproject, re.MULTILINE
        )
        assert declared is not None, "pyproject.toml declares mocop.static package data"
        globs = re.findall(r'"([^"]+)"', declared.group(1))
        self.assertTrue(globs)
        static = ROOT / "src" / "mocop" / "static"
        for filename, _type in STATIC_ROUTES.values():
            with self.subTest(asset=filename):
                self.assertTrue((static / filename).is_file())
                self.assertTrue(
                    any(fnmatch.fnmatch(filename, pattern) for pattern in globs),
                    f"{filename} is routed but no package-data glob ships it",
                )

    def test_stylesheet_keeps_secondary_text_legible(self) -> None:
        # 146 of the stylesheet's font sizes were 7, 8, or 9 px — below what
        # CJK text needs at desk distance. Secondary text now goes through
        # the --text-* scale (the compact density alone dials it down), and
        # no literal size below 9 px may come back.
        stylesheet = (ROOT / "src" / "mocop" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        literals = [
            float(size) for size in re.findall(r"font-size:\s*([0-9.]+)px", stylesheet)
        ]
        self.assertTrue(literals)
        self.assertGreaterEqual(min(literals), 9.0, sorted(literals)[:5])
        for variable in ("--text-2xs", "--text-xs", "--text-sm"):
            self.assertIn(f"{variable}:", stylesheet)

    def test_stylesheet_radii_come_from_the_scale(self) -> None:
        # Twenty distinct corner radii (2 to 17 px) sat side by side before the
        # scale; controls of one height now share one step, and a new literal
        # would reintroduce the drift.
        stylesheet = (ROOT / "src" / "mocop" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        literal = re.findall(r"border-radius:[^;}]*\d+px", stylesheet)
        self.assertEqual(literal, [])
        for step in ("2xs", "xs", "sm", "md", "lg", "xl", "2xl", "pill"):
            self.assertIn(f"--radius-{step}:", stylesheet)

    def test_muted_text_passes_contrast_on_every_style_surface(self) -> None:
        # WCAG AA for body-size text is 4.5:1. The two secondary text tiers are
        # checked against each visual style's static surfaces (bg, surface,
        # panel-solid), resolving the `color-mix(in srgb, var(--x) N%, #hex)`
        # form the tokens use; the default --muted-2 sat at 2.6:1 before.
        stylesheet = (ROOT / "src" / "mocop" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        root_block = re.search(r":root \{(.*?)\n\}", stylesheet, re.S)
        assert root_block is not None
        root = self._tokens(root_block.group(1))
        styles = {"precision": root}
        for match in re.finditer(
            r'html\[data-style="(\w+)"\] \{(.*?)\n\}', stylesheet, re.S
        ):
            styles[match.group(1)] = {**root, **self._tokens(match.group(2))}
        self.assertEqual(len(styles), 6)
        for name, tokens in styles.items():
            surfaces = [
                self._rgb(tokens[surface], tokens)
                for surface in ("bg", "surface", "panel-solid")
            ]
            for tier in ("muted", "muted-2"):
                text = self._rgb(tokens[tier], tokens)
                for surface, rgb in zip(
                    ("bg", "surface", "panel-solid"), surfaces, strict=False
                ):
                    with self.subTest(style=name, tier=tier, surface=surface):
                        self.assertGreaterEqual(_contrast(text, rgb), 4.5)

    @staticmethod
    def _tokens(block: str) -> dict[str, str]:
        return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", block))

    @classmethod
    def _rgb(cls, value: str, tokens: dict[str, str]) -> tuple[float, float, float]:
        value = value.strip()
        mixed = re.fullmatch(
            r"color-mix\(in srgb, var\(--([a-z0-9-]+)\) (\d+)%, (.+)\)", value
        )
        if mixed:
            share = int(mixed.group(2)) / 100
            first = cls._rgb(tokens[mixed.group(1)], tokens)
            second = cls._rgb(mixed.group(3), tokens)
            return tuple(
                a * share + b * (1 - share) for a, b in zip(first, second, strict=False)
            )
        rgba = re.fullmatch(r"rgba\((\d+), ?(\d+), ?(\d+), ?([\d.]+)\)", value)
        if rgba:
            # A translucent surface is read over the style's page background.
            alpha = float(rgba.group(4))
            backdrop = cls._rgb(tokens["bg"], tokens)
            return tuple(
                float(rgba.group(index)) * alpha + under * (1 - alpha)
                for index, under in zip((1, 2, 3), backdrop, strict=True)
            )
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), value
        return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))

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
