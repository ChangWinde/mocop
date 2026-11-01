"""Select an explicitly configured API version without following redirects."""

import os
import stat

from .platform_origin import ORIGIN_PATH

API_PREFIX_PATH = ORIGIN_PATH.with_name("api-prefix")


def read_api_prefix(path=API_PREFIX_PATH):
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 64
        ):
            return None
        value = path.read_text().strip()
        return value if value in {"/pt/api", "/pt/api2"} else None
    except FileNotFoundError:
        return "/pt/api"
    except (OSError, UnicodeError):
        return None


API_PREFIX = read_api_prefix()
