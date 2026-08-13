from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from mocop.web import API_ROUTES

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_TARGET = re.compile(r"\[[^]]*]\(([^)]+)\)")
HTML_TARGET = re.compile(r'(?:href|src)="([^"]+)"')
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
API_PATH = re.compile(r"/api/[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*")


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


class DocumentationTests(unittest.TestCase):
    def test_local_links_resolve_inside_the_repository(self) -> None:
        documents = [
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
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


if __name__ == "__main__":
    unittest.main()
