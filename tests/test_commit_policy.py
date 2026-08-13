from __future__ import annotations

import os
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

    def test_rejects_non_forge_subjects(self) -> None:
        subjects = (
            "",
            "refactor(repo): simplify layout",
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


if __name__ == "__main__":
    unittest.main()
