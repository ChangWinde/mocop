from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from mocop.api_manifest import API_ROUTES, API_VERSION, ERROR_CODES
from mocop.config import _OPTIONAL_KEYS, _REQUIRED_KEYS
from mocop.remote_script import _PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[1]
ENGLISH_README = ROOT / "README.md"
CHINESE_README = ROOT / "docs" / "locales" / "zh-CN" / "README.md"
DOCUMENTATION_PORTAL = ROOT / "docs" / "README.md"
ADR_INDEX = ROOT / "docs" / "adr" / "README.md"
MARKDOWN_TARGET = re.compile(r"\[[^]]*]\(([^)]+)\)")
HTML_TARGET = re.compile(r'(?:href|src)="([^"]+)"')
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
API_PATH = re.compile(r"/api/[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*")
ROUTE_ROW = re.compile(r"^\| (GET|POST) \| `([^`]+)` \| ([PARW]) \|", re.MULTILINE)
ERROR_TABLE_ROW = re.compile(r"^\| `([A-Z][A-Z0-9_]+)` \| (\d{3}) \|", re.MULTILINE)
CONFIG_ROW = re.compile(r"^\| `([^`]+)` \| (?:yes|no) \|", re.MULTILINE)
ERROR_LITERAL = re.compile(r"^[A-Z][A-Z0-9_]+$")
ACCESS_ABBREVIATIONS = {
    "public": "P",
    "authenticated": "A",
    "reader": "R",
    "writer": "W",
}
NON_ERROR_LITERALS = {"DELETE", "DENY", "GET", "HEAD", "PATCH", "POST", "PUT", "TRACE"}


def heading_anchors(content: str) -> set[str]:
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for heading in HEADING.findall(content):
        base = re.sub(r"[^\w\s-]", "", heading.lower())
        base = re.sub(r"\s+", "-", base.strip())
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return anchors


class ApiReferenceDriftTests(unittest.TestCase):
    """Keep docs/API.md and the live route manifest from drifting apart."""

    def setUp(self) -> None:
        self.reference = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")

    def test_every_route_in_the_manifest_is_documented(self) -> None:
        for method, path, _access in API_ROUTES:
            self.assertIn(
                f"{method} {path}",
                self.reference,
                f"docs/API.md does not document {method} {path}",
            )

    def test_every_documented_api_path_exists_in_the_manifest(self) -> None:
        routed_paths = {path for _method, path, _access in API_ROUTES}
        for documented in sorted(set(API_PATH.findall(self.reference))):
            self.assertIn(
                documented,
                routed_paths,
                f"docs/API.md mentions unrouted API path {documented}",
            )

    def test_endpoint_index_matches_route_methods_paths_and_access(self) -> None:
        documented = {
            (method, path, access)
            for method, path, access in ROUTE_ROW.findall(self.reference)
        }
        implemented = {
            (method, path, ACCESS_ABBREVIATIONS[access])
            for method, path, access in API_ROUTES
        }
        self.assertEqual(implemented, documented)

    def test_stable_error_code_table_matches_web_implementation(self) -> None:
        import ast

        # The manifest catalog is what /api/meta publishes; the reference table
        # must list exactly that, and every code the handler or the manifest
        # validators can emit must be in the catalog.
        catalog = {code for code, _status in ERROR_CODES}
        documented = {
            code: int(status)
            for code, status in ERROR_TABLE_ROW.findall(self.reference)
        }
        self.assertEqual(documented, dict(ERROR_CODES))
        emitted: set[str] = set()
        for module in ("web.py", "api_manifest.py"):
            source = (ROOT / "src" / "mocop" / module).read_text(encoding="utf-8")
            emitted.update(
                node.value
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ERROR_LITERAL.fullmatch(node.value)
                and node.value not in NON_ERROR_LITERALS
            )
        self.assertEqual(emitted - catalog, set())

    def test_authentication_and_protocol_contracts_are_current(self) -> None:
        self.assertIn(f"**API version:** `{API_VERSION}`", self.reference)
        self.assertIn("Authorization: Bearer ${MOCOP_TOKEN}", self.reference)
        self.assertIn("fetch()", self.reference)
        self.assertIn("never creates an ambient Cookie", self.reference)
        self.assertNotIn("Tier L", self.reference)
        self.assertNotIn("| L |", self.reference)

        current_contracts = (
            ROOT / "docs" / "API.md",
            ROOT / "docs" / "ARCHITECTURE.md",
            ROOT / "docs" / "SECURITY.md",
            ROOT / "docs" / "adr" / "0016-single-version-protocol-and-agent-api.md",
        )
        for document in current_contracts:
            content = document.read_text(encoding="utf-8")
            self.assertIn(_PROTOCOL_VERSION, content, f"missing protocol in {document}")


class ConfigurationReferenceDriftTests(unittest.TestCase):
    def test_top_level_field_table_matches_strict_schema(self) -> None:
        reference = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        documented = set(CONFIG_ROW.findall(reference))
        self.assertEqual(_REQUIRED_KEYS | _OPTIONAL_KEYS, documented)

    def test_current_service_docs_do_not_claim_mount_namespace_guarantees(self) -> None:
        for relative in ("docs/ARCHITECTURE.md", "docs/SECURITY.md"):
            content = (ROOT / relative).read_text(encoding="utf-8")
            for directive in ("PrivateTmp", "ProtectSystem", "ReadWritePaths"):
                self.assertNotIn(directive, content, f"stale guarantee in {relative}")

    def test_readmes_point_to_capability_and_retention_contracts(self) -> None:
        for document in (ENGLISH_README, CHINESE_README):
            content = document.read_text(encoding="utf-8")
            self.assertIn("#access_token=...", content)
            self.assertIn("CONFIGURATION.md", content)
            self.assertIn("OPERATIONS.md", content)
            self.assertIn("SQLite", content)


class DocumentationTests(unittest.TestCase):
    def test_local_links_resolve_inside_the_repository(self) -> None:
        documents = [
            ENGLISH_README,
            *sorted((ROOT / "docs").rglob("*.md")),
            *sorted((ROOT / ".github").glob("*.md")),
        ]

        for document in documents:
            content = document.read_text(encoding="utf-8")
            anchors = heading_anchors(content)
            targets = MARKDOWN_TARGET.findall(content) + HTML_TARGET.findall(content)
            for raw_target in targets:
                target = raw_target.strip().split(maxsplit=1)[0]
                parsed = urlsplit(target)
                if parsed.scheme in {"http", "https", "mailto"}:
                    continue
                self.assertFalse(
                    parsed.scheme, f"unsupported link in {document}: {target}"
                )
                self.assertFalse(
                    parsed.netloc, f"protocol-relative link in {document}: {target}"
                )
                if not parsed.path:
                    if parsed.fragment:
                        self.assertIn(
                            unquote(parsed.fragment),
                            anchors,
                            f"broken anchor in {document}: {target}",
                        )
                    continue
                resolved = (document.parent / unquote(parsed.path)).resolve()
                self.assertTrue(
                    resolved.is_relative_to(ROOT),
                    f"link escapes repository in {document}: {target}",
                )
                self.assertTrue(
                    resolved.exists(),
                    f"broken local link in {document}: {target}",
                )

    def test_documentation_portal_indexes_every_canonical_document(self) -> None:
        content = DOCUMENTATION_PORTAL.read_text(encoding="utf-8")
        expected_targets = {
            path.name
            for path in (ROOT / "docs").glob("*.md")
            if path != DOCUMENTATION_PORTAL
        } | {
            "../README.md",
            "locales/zh-CN/README.md",
            "../examples/mocop.example.json",
            "adr/README.md",
            "../.github/CONTRIBUTING.md",
            "../.github/CODE_OF_CONDUCT.md",
            "../.github/SECURITY.md",
        }
        indexed_targets = {
            urlsplit(target).path for target in MARKDOWN_TARGET.findall(content)
        }
        self.assertLessEqual(expected_targets, indexed_targets)

    def test_adr_index_lists_every_numbered_decision(self) -> None:
        expected = {
            path.name
            for path in (ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")
        }
        indexed = {
            Path(urlsplit(target).path).name
            for target in MARKDOWN_TARGET.findall(ADR_INDEX.read_text(encoding="utf-8"))
            if re.fullmatch(r"[0-9]{4}-[^/]+\.md", Path(urlsplit(target).path).name)
        }
        self.assertEqual(expected, indexed)

    def test_localized_readme_has_one_governed_location(self) -> None:
        self.assertTrue(CHINESE_README.is_file())
        self.assertFalse((ROOT / "README.zh-CN.md").exists())
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("recursive-include docs *.md *.png", manifest)


if __name__ == "__main__":
    unittest.main()
