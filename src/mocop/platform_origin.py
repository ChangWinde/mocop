"""Read the administrator's private training-platform origin configuration."""

import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

ORIGIN_PATH = Path.home() / ".config/mocop/zenith/origin"


def read_origin(path=ORIGIN_PATH):
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 2048
        ):
            return None
        origin = path.read_text().strip().rstrip("/")
        parts = urlsplit(origin)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path
            or parts.query
            or parts.fragment
            or any(ord(c) <= 32 for c in origin)
        ):
            return None
        _ = parts.port
        return origin
    except (OSError, UnicodeError, ValueError):
        return None


ORIGIN = read_origin()
