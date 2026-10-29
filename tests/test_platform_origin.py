import os
import tempfile
import unittest
from pathlib import Path

from mocop.platform_origin import read_origin


class PlatformOriginTests(unittest.TestCase):
    def test_only_a_private_origin_without_credentials_or_extra_url_parts_is_accepted(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "origin"
            for origin in ("https://training.example.test", "http://127.0.0.1:8765/"):
                path.write_text(origin)
                path.chmod(0o600)
                self.assertEqual(read_origin(path), origin.rstrip("/"))
            for origin in (
                "file:///tmp/platform",
                "http://user:password@example.test",
                "http://example.test/path",
                "http://example.test?q=1",
                "http://example.test#fragment",
                "http://example.test:99999",
                "http://example.test\nextra",
            ):
                path.write_text(origin)
                self.assertIsNone(read_origin(path), origin)
            path.write_text("https://training.example.test")
            path.chmod(0o644)
            self.assertIsNone(read_origin(path))
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            self.assertIsNone(read_origin(link))
            self.assertIsNone(read_origin(Path(directory) / "missing"))
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo, 0o600)
            self.assertIsNone(read_origin(fifo))
