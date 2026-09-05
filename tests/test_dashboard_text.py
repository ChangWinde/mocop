from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from mocop.models import SERVER_MESSAGE_PREFIXES, SERVER_MESSAGES

ROOT = Path(__file__).resolve().parents[1]
LEAF = ROOT / "src" / "mocop" / "static" / "incident-text.js"
TRANSLATION = re.compile(r'^\s+"([^"]+)": "[^"]+",$', re.MULTILINE)
PREFIX = re.compile(r'^\s+\["([^"]+)", "[^"]+"\],$', re.MULTILINE)


def _leaf_block(source: str, opener: str) -> str:
    """The text of one top-level constant, from its opener to its closer."""
    start = source.index(opener)
    return source[
        start : source.index("\n  }", start)
        if opener.endswith("{")
        else source.index("\n  ]", start)
    ]


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _probe_failure_messages() -> tuple[set[str], set[str]]:
    """Every message the probe attaches to a result: exact ones and the
    literal heads of the f-string messages that carry an exit code.

    Three emit sites exist: the ``failure(status, message)`` closure and the
    ``gpu_message`` assignments in ``probe.py``, and the ``classify_ssh_failure``
    table plus its fallback return in ``ssh_failures.py``.
    """
    exact: set[str] = set()
    prefixed: set[str] = set()

    def collect(expression: ast.AST) -> None:
        if isinstance(expression, ast.IfExp):
            collect(expression.body)
            collect(expression.orelse)
        elif isinstance(expression, ast.JoinedStr):
            head = _literal(expression.values[0])
            assert head is not None
            prefixed.add(head.strip())
        elif (message := _literal(expression)) is not None:
            exact.add(message)

    modules = ("probe.py", "ssh_failures.py")
    nodes = [
        node
        for name in modules
        for node in ast.walk(
            ast.parse((ROOT / "src" / "mocop" / name).read_text(encoding="utf-8"))
        )
    ]
    for node in nodes:
        is_failure_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "failure"
        )
        if is_failure_call:
            for argument in node.args[1:]:
                collect(argument)
        if isinstance(node, ast.FunctionDef) and node.name == "classify_ssh_failure":
            for inner in ast.walk(node):
                pair = (
                    isinstance(inner, ast.Tuple)
                    and len(inner.elts) == 2
                    and isinstance(inner.elts[0], ast.Tuple)
                )
                if pair:
                    collect(inner.elts[1])
                if isinstance(inner, ast.Return) and inner.value is not None:
                    collect(inner.value)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "gpu_message"
            for target in node.targets
        ):
            collect(node.value)
    return exact, prefixed


def _collector_failure_messages() -> set[str]:
    tree = ast.parse(
        (ROOT / "src" / "mocop" / "service.py").read_text(encoding="utf-8")
    )
    messages: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "message" and (value := _literal(keyword.value)):
                    messages.add(value)
    return messages


class DashboardFailureVocabularyTests(unittest.TestCase):
    """The dashboard translates exactly the failure messages the backend emits.

    A backend message without a translation reaches the operator in English;
    a translation without a backend message is dead copy that hides the drift.
    """

    def test_every_backend_failure_message_has_a_translation(self) -> None:
        leaf = LEAF.read_text(encoding="utf-8")
        translated = set(
            TRANSLATION.findall(_leaf_block(leaf, "const FAILURE_TEXT = {"))
        )
        prefixes = set(PREFIX.findall(_leaf_block(leaf, "const FAILURE_PREFIXES = [")))
        exact, prefixed = _probe_failure_messages()
        exact |= _collector_failure_messages()
        self.assertGreater(len(exact), 15, "the probe emit sites were not found")
        self.assertEqual(exact - translated, set())
        self.assertEqual(translated - exact, set())
        # Exit-code messages are matched by prefix: every literal head the probe
        # formats must start with one dashboard prefix, and every prefix must
        # still correspond to a head the probe formats.
        for head in prefixed:
            self.assertTrue(any(head.startswith(prefix) for prefix in prefixes), head)
        for prefix in prefixes:
            self.assertTrue(any(head.startswith(prefix) for head in prefixed), prefix)

    def test_the_published_vocabulary_is_exactly_what_the_code_emits(self) -> None:
        # models.SERVER_MESSAGES is what /api/meta publishes; it must list every
        # message the emit sites can produce and nothing else, in a stable
        # order without duplicates.
        exact, prefixed = _probe_failure_messages()
        exact |= _collector_failure_messages()
        self.assertEqual(set(SERVER_MESSAGES), exact)
        self.assertEqual(len(SERVER_MESSAGES), len(set(SERVER_MESSAGES)))
        for head in prefixed:
            self.assertTrue(
                any(head.startswith(prefix) for prefix in SERVER_MESSAGE_PREFIXES), head
            )
        for prefix in SERVER_MESSAGE_PREFIXES:
            self.assertTrue(any(head.startswith(prefix) for head in prefixed), prefix)

    def test_the_api_reference_lists_every_failure_message(self) -> None:
        # Automation branches on servers[].message, so the reference table is
        # the third party to the probe/dashboard vocabulary: every message the
        # backend emits appears in it, and it lists nothing the backend no
        # longer emits.
        reference = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
        start = reference.index("#### Failure messages")
        table = reference[start : reference.index("\n`servers[].system`", start)]
        documented: set[str] = set()
        body_rows = re.findall(r"^\| ([^|]+) \|", table, re.MULTILINE)[
            1:
        ]  # skip header
        for cell in body_rows:
            documented.update(re.findall(r"`([^`]+)`", cell))
        exact, prefixed = _probe_failure_messages()
        exact |= _collector_failure_messages()
        self.assertEqual(exact - documented, set())
        for head in prefixed:
            self.assertTrue(any(doc.startswith(head) for doc in documented), head)
        stale = {
            doc
            for doc in documented
            if doc not in exact and not any(doc.startswith(head) for head in prefixed)
        }
        self.assertEqual(stale, set())


if __name__ == "__main__":
    unittest.main()
