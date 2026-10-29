import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mocop.directory_disk import parse_disk
from mocop.remote_script import DIRECTORY_DISK_SCRIPT


class MountDiscoveryTests(unittest.TestCase):
    def test_all_nas_and_local_mounts_are_discovered_and_aliases_are_not_rescanned(
        self,
    ):
        mounts = [
            {"target": "/", "source": "/dev/root", "fstype": "ext4", "fsroot": "/"},
            {"target": "/data", "source": "/dev/disk", "fstype": "ext4", "fsroot": "/"},
            *[
                {
                    "target": "/mnt/nas/" + name,
                    "source": "//nas/" + name,
                    "fstype": "cifs",
                    "fsroot": "/",
                }
                for name in ["data", "scihub", "droid"]
            ],
            {
                "target": "/mnt/duplicate-nas/data",
                "source": "//nas/data",
                "fstype": "cifs",
                "fsroot": "/",
            },
            {
                "target": "/data/alice/nested",
                "source": "//nas/nested",
                "fstype": "cifs",
                "fsroot": "/",
            },
            {
                "target": "/home/dataset-local",
                "source": "/dev/nvme",
                "fstype": "xfs",
                "fsroot": "/volume",
            },
            {
                "target": "/home/dataset-assist-0",
                "source": "nas:/users",
                "fstype": "nfs",
                "fsroot": "/",
            },
            {"target": "/dev/shm", "source": "tmpfs", "fstype": "tmpfs", "fsroot": "/"},
            {
                "target": "/snap/tool",
                "source": "/dev/loop0",
                "fstype": "squashfs",
                "fsroot": "/",
            },
        ]
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            self.assertNotIn("shell", kwargs)
            if args[0] == "findmnt":
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"filesystems": mounts}).encode()
                )
            if args[0] == "df":
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                        + "\n".join(
                            f"{m['source']} 2048 1536 512 75% {m['target']}"
                            for m in mounts
                        )
                    ).encode(),
                )
            self.assertEqual(args[0], "du")
            self.assertEqual(args[-2], "--")
            return SimpleNamespace(returncode=0, stdout=b"123\tpath\0")

        @contextlib.contextmanager
        def entries(root):
            yield iter(
                [
                    SimpleNamespace(
                        path=root.rstrip("/") + "/alice", is_dir=lambda **kw: True
                    )
                ]
            )

        baseline = os.environ.get("MOCOP_DISK_SCRIPT_BASELINE")
        script = Path(baseline).read_text() if baseline else DIRECTORY_DISK_SCRIPT
        source = script.split("<<'MOCOP_DISK_PY'\n", 1)[1].rsplit("\nMOCOP_DISK_PY", 1)[
            0
        ]
        output = io.StringIO()
        with (
            patch("subprocess.run", side_effect=command),
            patch("shutil.which", return_value=None),
            patch("os.path.isdir", return_value=True),
            patch("os.path.islink", return_value=False),
            patch("os.scandir", side_effect=entries),
            contextlib.redirect_stdout(output),
        ):
            exec(compile(source, "<fixed-disk-producer>", "exec"), {})
        sample = parse_disk(output.getvalue())
        found = {p for fs in sample["filesystems"] for p in fs["aliases"]}
        expected = {
            "/mnt/nas/data",
            "/mnt/nas/scihub",
            "/mnt/nas/droid",
            "/home/dataset-local",
            "/home/dataset-assist-0",
        }
        self.assertTrue(
            expected <= found, f"missing storage mounts: {expected - found}"
        )
        self.assertNotIn("/dev/shm", found)
        self.assertNotIn("/snap/tool", found)
        self.assertTrue(all(fs["usedKiB"] == 1536 for fs in sample["filesystems"]))
        scanned = {args[-1] for args in calls if args[0] == "du"}
        self.assertIn("/mnt/nas/data/alice", scanned)
        self.assertIn("/home/dataset-local/alice", scanned)
        self.assertNotIn("/mnt/duplicate-nas/data/alice", scanned)
        parent = next(
            args for args in calls if args[0] == "du" and args[-1] == "/data/alice"
        )
        self.assertIn("--exclude=/data/alice/nested", parent)
        self.assertTrue(
            all(row["mountPoint"] in found for row in sample["directories"])
        )
