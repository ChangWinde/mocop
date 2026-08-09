from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_TARGET = re.compile(r"\[[^]]*]\(([^)]+)\)")
HTML_TARGET = re.compile(r'(?:href|src)="([^"]+)"')
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


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
