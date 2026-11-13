"""Read-only, bounded adapter for complete external directory-count receipts."""

import json
import math
import os
import stat
import time
from datetime import datetime, timezone


def read_measurement(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 1024**2
        ):
            raise ValueError("invalid private measurement file")
        raw = stream.read(1024**2 + 1)
    if len(raw) > 1024**2:
        raise ValueError("measurement exceeds size bound")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("complete") is not True:
        raise ValueError("external measurement incomplete")
    start, end = data.get("started_at"), data.get("finished_at")
    if (
        any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end))
        or not 0 < start <= end <= time.time() + 60
    ):
        raise ValueError("invalid measurement timestamps")
    roots = data.get("root_allocated_bytes")
    if (
        not isinstance(roots, dict)
        or not 1 <= len(roots) <= 32
        or any(
            not isinstance(p, str)
            or not p.startswith("/")
            or ".." in p.split("/")
            or type(n) is not int
            or not 0 <= n <= 2**60
            for p, n in roots.items()
        )
    ):
        raise ValueError("invalid measurement roots")
    return data


def measurement_row(data, root, target):
    row = {"path": root, "bytes": None, "status": "unreadable", "children": []}
    if target != root:
        row["usagePath"] = target
    if data is None or target not in data["root_allocated_bytes"]:
        return row
    children = data.get("top_directories", [])
    if not isinstance(children, list):
        raise ValueError("invalid measurement children")
    selected = [
        {"path": c["path"], "bytes": c["allocated_bytes"]}
        for c in children[:1024]
        if isinstance(c, dict)
        and isinstance(c.get("path"), str)
        and c["path"].startswith(target.rstrip("/") + "/")
        and ".." not in c["path"].split("/")
        and len(c["path"]) <= 4096
        and type(c.get("allocated_bytes")) is int
        and 0 <= c["allocated_bytes"] <= 2**60
    ]
    end = data["finished_at"]
    row.update(
        bytes=data["root_allocated_bytes"][target],
        status="ok",
        children=sorted(selected, key=lambda c: -c["bytes"])[:32],
        source="complete_receipt",
        sampleEpoch=end,
        sampledAt=datetime.fromtimestamp(end, timezone.utc).isoformat(),
        scanDurationSeconds=end - data["started_at"],
        liveChanges=data.get("archive_changed_during_measurement") is True,
    )
    return row
