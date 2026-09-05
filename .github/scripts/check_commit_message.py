#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ALLOWED_OPERATIONS = frozenset(
    {
        "add",
        "cleanup",
        "document",
        "fix",
        "harden",
        "promote",
        "refactor",
        "remove",
        "test",
        "validate",
    }
)
SUBJECT_PATTERN = re.compile(r"^\[(?P<body>[a-z0-9][a-z0-9/-]*)\]: (?P<summary>\S.*)$")
SCOPE_SEGMENT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
AUTOSQUASH_PREFIXES = ("fixup! ", "squash! ")
GENERATED_PREFIXES = ("Merge ", "Revert ")
MAX_SUBJECT_LENGTH = 72
# Commits already on the protected default branch that predate their own
# enforcement. Release tags are immutable, so they cannot be reworded; keep the
# whole-history gate honest by listing each one with its reason instead of
# weakening the rule. New entries need a pull-request review.
HISTORICAL_EXEMPTIONS = {
    "b76e6507748715d2f9e009423b88d290a20e85eb": (
        "pushed directly to master on 2026-08-26 with the unlisted 'feat' "
        "operation before the branch ruleset required pull requests"
    ),
}


def normalize_subject(subject: str) -> str:
    normalized = subject.strip()
    while normalized.startswith(AUTOSQUASH_PREFIXES):
        for prefix in AUTOSQUASH_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].strip()
                break
    return normalized


def validate_subject(subject: str, *, allow_git_generated: bool = False) -> str | None:
    normalized = normalize_subject(subject)
    if not normalized:
        return "commit subject is empty"
    if normalized.startswith(GENERATED_PREFIXES):
        if allow_git_generated:
            return None
        return "generated Merge/Revert subjects require matching Git metadata"
    if len(normalized) > MAX_SUBJECT_LENGTH:
        return f"subject must not exceed {MAX_SUBJECT_LENGTH} characters"

    match = SUBJECT_PATTERN.fullmatch(normalized)
    if match is None:
        return "subject must match '[scope/op]: imperative summary'"

    body = match.group("body")
    if "/" not in body:
        return "brackets must contain a scope and operation separated by '/'"
    scope, operation = body.rsplit("/", 1)
    if operation not in ALLOWED_OPERATIONS:
        allowed = ", ".join(sorted(ALLOWED_OPERATIONS))
        return f"operation '{operation}' is not allowed; choose one of: {allowed}"
    invalid_segments = [
        segment
        for segment in scope.split("/")
        if SCOPE_SEGMENT_PATTERN.fullmatch(segment) is None
    ]
    if invalid_segments:
        return "scope must use lowercase path segments with letters, digits, or hyphens"
    if match.group("summary").rstrip().endswith("."):
        return "imperative summary must not end with a period"
    return None


def subject_from_message_file(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def git_state_allows_generated_subject(subject: str) -> bool:
    if subject.startswith("Merge "):
        reference = "MERGE_HEAD"
    elif subject.startswith("Revert "):
        reference = "REVERT_HEAD"
    else:
        return False
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", reference],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def commit_allows_generated_subject(commit: str, subject: str) -> bool:
    if subject.startswith("Merge "):
        # Read the raw object: in a shallow checkout (the default for
        # pull-request test jobs) the graft hides parents from `git log %P`
        # while the commit itself still records them.
        completed = subprocess.run(
            ["git", "cat-file", "-p", commit],
            check=True,
            capture_output=True,
            text=True,
        )
        header = completed.stdout.split("\n\n", 1)[0]
        parents = [line for line in header.splitlines() if line.startswith("parent ")]
        return len(parents) >= 2
    if subject.startswith("Revert "):
        completed = subprocess.run(
            ["git", "log", "-1", "--format=%B", commit],
            check=True,
            capture_output=True,
            text=True,
        )
        return "This reverts commit " in completed.stdout
    return False


def subjects_from_revision(revision: str) -> list[tuple[str, str]]:
    if revision.startswith("-"):
        raise ValueError("revision must not start with '-'")
    completed = subprocess.run(
        ["git", "log", "--reverse", "--format=%H%x09%s", revision],
        check=True,
        capture_output=True,
        text=True,
    )
    subjects: list[tuple[str, str]] = []
    for line in completed.stdout.splitlines():
        commit, subject = line.split("\t", 1)
        subjects.append((commit, subject))
    return subjects


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Validate Forge-style Git commit subjects."
    )
    argument_parser.add_argument("message_file", nargs="?", type=Path)
    argument_parser.add_argument("--subject")
    argument_parser.add_argument("--range", dest="revision")
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    modes = sum(
        value is not None
        for value in (arguments.message_file, arguments.subject, arguments.revision)
    )
    if modes != 1:
        parser().error("provide exactly one message file, --subject, or --range")

    if arguments.revision is not None:
        failures: list[str] = []
        try:
            subjects = subjects_from_revision(arguments.revision)
            for commit, subject in subjects:
                if commit in HISTORICAL_EXEMPTIONS:
                    continue
                error = validate_subject(
                    subject,
                    allow_git_generated=commit_allows_generated_subject(
                        commit, subject
                    ),
                )
                if error:
                    failures.append(f"{commit[:12]} {subject}: {error}")
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            print(f"cannot inspect commit range: {exc}", file=sys.stderr)
            return 2
        if failures:
            print("\n".join(failures), file=sys.stderr)
            return 1
        return 0

    try:
        if arguments.subject is not None:
            subject = arguments.subject
            allow_git_generated = False
        else:
            subject = subject_from_message_file(arguments.message_file)
            allow_git_generated = git_state_allows_generated_subject(subject)
    except OSError as exc:
        print(f"cannot read commit message: {exc}", file=sys.stderr)
        return 2
    error = validate_subject(subject, allow_git_generated=allow_git_generated)
    if error:
        print(f"invalid commit subject: {error}\nreceived: {subject}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
