"""Daily directory disk usage, with independent bounded remote scans."""

import json
import subprocess
from pathlib import PurePosixPath

from .config import is_safe_alias
from .probe import _ProcessCancelled, _ProcessOutputLimitExceeded
from .remote_script import DIRECTORY_DISK_INVENTORY_SCRIPT, DIRECTORY_DISK_SCRIPT
from .user_memory import (
    INTERVAL_SECONDS,
    DailyUserMemory,
    MemoryCollectionError,
    timestamp,
)


def parse_disk(text: str) -> dict:
    sample = json.loads(text)
    if not isinstance(sample, dict) or sample.get("protocol") != "DIRECTORY_DISK_V1":
        raise ValueError("invalid disk sample")
    rows, notes = sample.get("directories"), sample.get("notes")
    if not isinstance(rows, list) or len(rows) > 256 or not isinstance(notes, list):
        raise ValueError("invalid directory list")
    if len(notes) > 32 or any(not isinstance(n, str) or len(n) > 4096 for n in notes):
        raise ValueError("invalid disk notes")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid directory")
        path, size, status = row.get("path"), row.get("sizeKiB"), row.get("status")
        if (
            not isinstance(path, str)
            or len(path) > 4096
            or not path.startswith("/")
            or ".." in PurePosixPath(path).parts
            or "\x00" in path
            or path in seen
        ):
            raise ValueError("invalid directory path")
        if status not in ("ok", "unreadable", "timeout", "skipped"):
            raise ValueError("invalid directory status")
        if status == "ok":
            if type(size) is not int or not 0 <= size <= 2**50:
                raise ValueError("invalid directory size")
        elif size is not None:
            raise ValueError("incomplete directory must not report a size")
        seen.add(path)
    filesystems = sample.get("filesystems", [])
    if not isinstance(filesystems, list) or len(filesystems) > 128:
        raise ValueError("invalid filesystem inventory")
    mounts = set()
    for fs in filesystems:
        if not isinstance(fs, dict):
            raise ValueError("invalid filesystem")
        mount, aliases = fs.get("mountPoint"), fs.get("aliases")
        if (
            not isinstance(mount, str)
            or not mount.startswith("/")
            or len(mount) > 4096
            or mount in mounts
        ):
            raise ValueError("invalid mount point")
        if (
            not isinstance(aliases, list)
            or not 1 <= len(aliases) <= 128
            or mount not in aliases
        ):
            raise ValueError("invalid mount aliases")
        if any(
            not isinstance(p, str) or not p.startswith("/") or len(p) > 4096
            for p in aliases
        ):
            raise ValueError("invalid mount alias")
        if (
            not isinstance(fs.get("fsType"), str)
            or len(fs["fsType"]) > 128
            or fs.get("kind") not in ("network", "local")
            or type(fs.get("scanRoot")) is not bool
        ):
            raise ValueError("invalid filesystem type")
        for key in ("totalKiB", "usedKiB", "availableKiB", "percentUsed"):
            value = fs.setdefault(key, None)
            if value is not None and (
                type(value) is not int
                or abs(value) > 2**50
                or (key != "availableKiB" and value < 0)
            ):
                raise ValueError("invalid filesystem capacity")
        mounts.add(mount)
    for row in rows:
        if "mountPoint" in row and (
            not isinstance(row["mountPoint"], str) or row["mountPoint"] not in mounts
        ):
            raise ValueError("directory has unknown filesystem")
    return {"directories": rows, "notes": notes, "filesystems": filesystems}


class DailyDirectoryDisk(DailyUserMemory):
    metric = "disk"
    sample_key = "directories"
    script = DIRECTORY_DISK_SCRIPT
    parser = staticmethod(parse_disk)
    timeout_seconds = 1830

    def collect_due(self) -> None:
        # Publish every host's cheap mount inventory before any recursive du scan.
        hosts = sorted({h for h in self._hosts() if is_safe_alias(h)})
        for host in hosts:
            if self._stop.is_set():
                return
            with self._lock:
                previous = self._records.get(host, {})
            if self._monotonic() < previous.get("_inventoryDeadline", 0):
                continue
            try:
                inventory = super()._collect(
                    host, script=DIRECTORY_DISK_INVENTORY_SCRIPT, timeout=40
                )
                now = self._clock()
                current_mounts = {m["mountPoint"] for m in inventory["filesystems"]}
                inventory["directories"] = [
                    r
                    for r in previous.get("directories", [])
                    if r.get("mountPoint") in current_mounts
                ]
                row = {
                    **previous,
                    **inventory,
                    "host": host,
                    "status": "pending",
                    "sampledAt": previous.get("sampledAt"),
                    "inventoryAt": timestamp(now),
                    "message": None,
                }
            except _ProcessCancelled:
                return
            except (
                OSError,
                ValueError,
                subprocess.TimeoutExpired,
                _ProcessOutputLimitExceeded,
            ) as exc:
                row = {
                    **previous,
                    "host": host,
                    "status": "error",
                    "directories": previous.get("directories", []),
                    "sampledAt": previous.get("sampledAt"),
                    "message": str(exc)
                    if isinstance(exc, MemoryCollectionError)
                    else "Mount inventory failed or exceeded limits",
                }
                self._deadlines[host] = self._monotonic() + INTERVAL_SECONDS
            row["_inventoryDeadline"] = self._monotonic() + INTERVAL_SECONDS
            with self._lock:
                self._records[host] = row
        super().collect_due()

    def snapshot(self) -> dict:
        result = super().snapshot()
        for row in result["servers"]:
            row.pop("_inventoryDeadline", None)
        return result

    def _collect(self, host: str, **kwargs) -> dict:
        started = self._clock()
        result = super()._collect(host, **kwargs)
        with self._lock:
            previous = self._records.get(host, {})
            result["_inventoryDeadline"] = previous.get("_inventoryDeadline", 0)
            result["inventoryAt"] = timestamp(started)
        return result
