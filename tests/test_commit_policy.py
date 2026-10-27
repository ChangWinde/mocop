from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / ".github" / "scripts" / "check_commit_message.py"


def check(*arguments: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


class CommitPolicyTests(unittest.TestCase):
    def test_accepts_forge_subjects_and_autosquash_commits(self) -> None:
        subjects = (
            "[repo/add]: publish initial open-source release",
            "[docs/readme/document]: explain cluster configuration",
            "[tests/fix]: stabilize browser smoke test",
            "fixup! [repo/harden]: enforce commit policy",
        )
        for subject in subjects:
            with self.subTest(subject=subject):
                self.assertEqual(check("--subject", subject).returncode, 0)

    def test_accepts_conventional_subjects(self) -> None:
        subjects = (
            "feat(web): show selected-server processes",
            "fix(docs/readme): correct the cluster example",
            "refactor(repo)!: drop the legacy layout",
            "fixup! test(probe): cover guest counters",
        )
        for subject in subjects:
            with self.subTest(subject=subject):
                self.assertEqual(check("--subject", subject).returncode, 0)

    def test_rejects_malformed_conventional_subjects(self) -> None:
        cases = {
            "feat: add a subject without a scope": "type(scope): imperative summary",
            "Fix(web): capitalize the type": "type(scope): imperative summary",
            "ship(web): use an unknown type": "type 'ship' is not allowed",
            "fix(Web): use an uppercase scope": "lowercase path segments",
            "fix(): leave the scope empty": "lowercase path segments",
            "fix(web): end with a period.": "must not end with a period",
            "fix(web): " + "x" * 70: "must not exceed 72 characters",
        }
        for subject, reason in cases.items():
            with self.subTest(subject=subject):
                result = check("--subject", subject)
                self.assertEqual(result.returncode, 1)
                self.assertIn(reason, result.stderr)

    def test_rejects_non_forge_subjects(self) -> None:
        subjects = (
            "",
            "[repo]: simplify layout",
            "[Repo/refactor]: simplify layout",
            "[repo/ship]: simplify layout",
            "[repo/refactor]: simplify layout.",
            "[repo/document]: " + "x" * 80,
            "Merge arbitrary invalid subject",
            'Revert "manual subject"',
        )
        for subject in subjects:
            with self.subTest(subject=subject):
                result = check("--subject", subject)
                self.assertEqual(result.returncode, 1)
                self.assertIn("invalid commit subject", result.stderr)

    def test_reads_the_first_non_comment_message_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            message = Path(directory) / "COMMIT_EDITMSG"
            message.write_text(
                "\n# template\n[ci/validate]: enforce commit subjects\n\nbody\n",
                encoding="utf-8",
            )
            self.assertEqual(check(str(message)).returncode, 0)

    def test_range_accepts_real_merge_and_revert_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            # Isolate the throwaway repository from operator-level git
            # configuration such as a global core.hooksPath or signing.
            environment = {
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            }

            def git(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *arguments],
                    cwd=repository,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )

            git("init", "--quiet", "--initial-branch=main")
            git("config", "user.name", "Mocop Tests")
            git("config", "user.email", "tests@example.invalid")
            git(
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "[repo/add]: establish test baseline",
            )
            git("switch", "--quiet", "-c", "topic")
            git(
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "[repo/add]: add merge fixture",
            )
            git("switch", "--quiet", "main")
            git(
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "[repo/add]: add mainline fixture",
            )
            git("merge", "--quiet", "--no-ff", "topic", "-m", "Merge branch 'topic'")
            (repository / "fixture.txt").write_text("fixture\n", encoding="utf-8")
            git("add", "fixture.txt")
            git("commit", "--quiet", "-m", "[repo/add]: add reversible fixture")
            git("revert", "--quiet", "--no-edit", "HEAD")

            self.assertEqual(check("--range", "HEAD", cwd=repository).returncode, 0)

            git(
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "Merge arbitrary invalid subject",
            )
            result = check("--range", "HEAD", cwd=repository)
            self.assertEqual(result.returncode, 1)
            self.assertIn("generated Merge/Revert subjects require", result.stderr)

    def test_hook_runs_the_checker_from_a_subdirectory(self) -> None:
        # Hook managers refuse interpreters started on runtime-derived paths,
        # so the hook must name the checker statically.
        hook = ROOT / ".githooks" / "commit-msg"
        self.assertNotIn("$(", hook.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            # Drop inherited configuration (including GIT_CONFIG_COUNT
            # injections of core.hooksPath) so the repository hook runs.
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_CONFIG")
                and key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
            }
            environment["GIT_CONFIG_GLOBAL"] = os.devnull
            environment["GIT_CONFIG_SYSTEM"] = os.devnull
            for relative in (
                ".githooks/commit-msg",
                ".github/scripts/check_commit_message.py",
            ):
                target = repository / relative
                target.parent.mkdir(parents=True)
                shutil.copy2(ROOT / relative, target)
            nested = repository / "nested"
            nested.mkdir()

            def git(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *arguments],
                    cwd=nested,
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )

            git("init", "--quiet", "--initial-branch=main", str(repository))
            git("config", "user.name", "Mocop Tests")
            git("config", "user.email", "tests@example.invalid")
            git("config", "core.hooksPath", ".githooks")
            accepted = git(
                "commit", "--quiet", "--allow-empty", "-m", "test(repo): pass the hook"
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            rejected = git(
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "Add a subject without a type",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("invalid commit subject", rejected.stderr)

    def test_historical_exemptions_are_exact_and_explained(self) -> None:
        # The whole-history gate tolerates only the listed pre-enforcement
        # commits; each entry is a full SHA with a reason, and the same subject
        # on any other commit is still rejected.
        namespace: dict[str, object] = {}
        exec(  # noqa: S102 - loading the script's constants, not user input
            compile(CHECKER.read_text(encoding="utf-8"), str(CHECKER), "exec"),
            namespace,
        )
        exemptions = namespace["HISTORICAL_EXEMPTIONS"]
        assert isinstance(exemptions, dict)
        self.assertEqual(set(exemptions), {"b76e6507748715d2f9e009423b88d290a20e85eb"})
        for sha, reason in exemptions.items():
            self.assertRegex(sha, r"^[0-9a-f]{40}$")
            self.assertGreater(len(reason), 20)
        rejected = check(
            "--subject", "[dashboard/feat]: lead GPU task rows with entry point"
        )
        self.assertEqual(rejected.returncode, 1)
        if (ROOT / ".git").exists():
            self.assertEqual(check("--range", "HEAD").returncode, 0)


if __name__ == "__main__":
    unittest.main()
