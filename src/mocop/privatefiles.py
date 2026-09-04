"""Private-file primitives shared by the configuration and service lifecycle.

Mocop is Linux-only, so ``O_NOFOLLOW`` is always available and Python file
descriptors are non-inheritable by default; callers therefore never need to
probe ``os`` for flags before opening a private path.
"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

PRIVATE_FILE_MODE = 0o600
_GROUP_OTHER_BITS = 0o077


def is_private_regular_file(metadata: os.stat_result) -> bool:
    """Return whether an open descriptor names the caller's own 0600 file."""
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and not metadata.st_mode & _GROUP_OTHER_BITS
    )


def acquire_private_lock(path: Path) -> int:
    """Create or open a private lock file and take its exclusive ``flock``.

    The returned descriptor owns the lock; ``release_private_lock`` unlocks
    and closes it. ``OSError`` (including a synthesized one for a lock path
    that is not a private regular file) leaves no descriptor open.
    """
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, PRIVATE_FILE_MODE
    )
    try:
        if not is_private_regular_file(os.fstat(descriptor)):
            raise OSError(f"lock file is not private: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def release_private_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
