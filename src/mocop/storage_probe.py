"""Fixed remote program. Input is JSON on stdin, never shell text."""

import json
import os
import shutil
import subprocess
import sys
import time


def valid_path(path):
    return (
        isinstance(path, str)
        and path.startswith("/")
        and path != "/"
        and ".." not in path.split("/")
        and not any(ord(c) < 32 for c in path)
        and len(path) <= 2048
    )


def valid_exclusions(names):
    return (
        isinstance(names, list)
        and len(names) <= 32
        and all(
            isinstance(name, str)
            and 1 <= len(name) <= 64
            and name not in (".", "..")
            and all(c.isascii() and (c.isalnum() or c in "_.-") for c in name)
            for name in names
        )
    )


def valid_options(roots, options):
    if not isinstance(roots, list) or not all(valid_path(root) for root in roots):
        return False
    if not isinstance(options, dict) or options.keys() - set(roots):
        return False
    for root, value in options.items():
        if not isinstance(value, dict) or value.keys() - {
            "usagePath",
            "scanSeconds",
            "purpose",
            "limitBytes",
        }:
            return False
        path = value.get("usagePath", root)
        if not valid_path(path) or not (
            path == root or path.startswith(root.rstrip("/") + "/")
        ):
            return False
        seconds = value.get("scanSeconds", 90)
        if type(seconds) is not int or not 30 <= seconds <= 1200:
            return False
        if "limitBytes" in value and (
            type(value["limitBytes"]) is not int
            or not 1 <= value["limitBytes"] <= 2**60
        ):
            return False
        purpose = value.get("purpose", "")
        if (
            not isinstance(purpose, str)
            or len(purpose) > 160
            or any(ord(c) < 32 for c in purpose)
        ):
            return False
    return sum(options.get(root, {}).get("scanSeconds", 90) for root in roots) <= 3600


def mount_inventory():
    try:
        p = subprocess.run(
            ["findmnt", "-J", "-l", "-o", "TARGET,FSTYPE,MAJ:MIN,SOURCE,FSROOT"],
            capture_output=True,
            timeout=8,
            check=True,
        )
        return json.loads(p.stdout)["filesystems"][:512]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return []


def scan(path, timeout, *, apparent=False, excluded=(), excluded_names=()):
    command = ["du", "-P", "-x", "-B1", "--max-depth=1", "--null"]
    if apparent:
        command.append("--apparent-size")
    for name in (".ssh", ".gnupg", *excluded_names, *excluded):
        command.append("--exclude=" + name)
    command += ["--", path]
    if shutil.which("nice"):
        command = ["nice", "-n", "19"] + command
    if shutil.which("ionice"):
        command = ["ionice", "-c", "3"] + command
    status, raw = "ok", b""
    try:
        p = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        raw = p.stdout
        if p.returncode:
            status = "unreadable"
    except subprocess.TimeoutExpired as error:
        status, raw = "timeout", error.stdout or b""
    except OSError:
        status = "unreadable"
    parsed = []
    for item in raw.split(b"\0"):
        if b"\t" not in item:
            continue
        size, name = item.split(b"\t", 1)
        if size.isdigit():
            parsed.append({"path": os.fsdecode(name), "bytes": int(size)})
    summary = next((r for r in parsed if r["path"] == path), None)
    children = sorted(
        (r for r in parsed if r["path"] != path), key=lambda r: -r["bytes"]
    )[:32]
    return {
        "path": path,
        "bytes": summary["bytes"] if status == "ok" and summary else None,
        "observedBytes": summary["bytes"] if summary and status != "ok" else None,
        "status": status if status != "ok" or summary else "unreadable",
        "children": children,
    }


def main():
    policy = json.loads(sys.stdin.read(65537))
    roots = policy["roots"]
    excluded_names = policy.get("excludeNames", [])
    options = policy.get("directoryOptions", {})
    assert valid_exclusions(excluded_names)
    assert (
        isinstance(roots, list)
        and len(roots) <= 16
        and all(valid_path(p) for p in roots)
    )
    assert valid_options(roots, options)
    result = {
        "protocol": "PERSONAL_STORAGE_V1",
        "directories": [],
        "filesystems": [],
        "notes": [],
    }
    started = time.monotonic()
    budget = sum(options.get(root, {}).get("scanSeconds", 90) for root in roots)
    seen_roots = {}
    mounts = mount_inventory()
    memory_mounts = {
        m["target"] for m in mounts if m.get("fstype") in ("tmpfs", "devtmpfs")
    }

    def inventory(path):
        mount = next((m for m in mounts if m.get("target") == path), None)
        if mount is None:
            # The longest ancestor is the filesystem containing this path.
            mount = max(
                (
                    m
                    for m in mounts
                    if path.startswith(m.get("target", "").rstrip("/") + "/")
                ),
                key=lambda m: len(m["target"]),
            )
        key = "|".join(
            str(mount.get(k, "")) for k in ("source", "fsroot", "fstype", "maj:min")
        )
        if not any(x["key"] == key for x in result["filesystems"]):
            v = os.statvfs(path)
            result["filesystems"].append(
                {
                    "mountPoint": mount["target"],
                    "key": key,
                    "kind": "memory"
                    if mount["fstype"] in ("tmpfs", "devtmpfs")
                    else "local"
                    if mount["fstype"] in ("ext4", "xfs", "btrfs", "overlay", "zfs")
                    else "network",
                    "fsType": mount["fstype"],
                    "capacityVirtual": mount["fstype"]
                    in ("fuse.bosfs", "fuse.bosfs2", "fuse.s3fs", "fuse.rclone"),
                    "totalBytes": v.f_blocks * v.f_frsize,
                    "availableBytes": v.f_bavail * v.f_frsize,
                    "usedBytes": (v.f_blocks - v.f_bfree) * v.f_frsize,
                }
            )
        return mount, key

    for root in roots:
        row = {"path": root, "bytes": None, "status": "missing", "children": []}
        settings = options.get(root, {})
        target = settings.get("usagePath", root)
        if target != root:
            row["usagePath"] = target
        parents = [target]
        while parents[-1] != root and len(parents[-1]) > len(root):
            parents.append(os.path.dirname(parents[-1]))
        if any(os.path.islink(path) for path in parents):
            row["status"] = "linked"
            result["directories"].append(row)
            continue
        if not os.path.isdir(target):
            result["directories"].append(row)
            continue
        identity = (os.stat(target).st_dev, os.stat(target).st_ino)
        if identity in seen_roots:
            row.update(status="linked", duplicateOf=seen_roots[identity])
            result["directories"].append(row)
            continue
        seen_roots[identity] = root
        # Inventory every child mount before slow scans, including NAS container paths.
        nested = [
            m["target"]
            for m in mounts
            if m.get("target", "").startswith(target.rstrip("/") + "/")
            and not any(
                c in m["target"].split("/") for c in (".ssh", ".gnupg", *excluded_names)
            )
        ]
        nested = sorted(set(nested), key=lambda p: (p.count("/"), p))
        if len(nested) > 16 or len(result["filesystems"]) + len(nested) >= 64:
            row["status"] = "inventory_error"
            result["directories"].append(row)
            continue
        try:
            mount, key = inventory(target)
            row["mountPoint"] = mount["target"]
            row["filesystemKey"] = key
            for path in nested:
                try:
                    inventory(path)
                except OSError:
                    if path not in memory_mounts:
                        raise
        except (OSError, ValueError, KeyError, IndexError):
            row["status"] = "inventory_error"
            result["directories"].append(row)
            continue
        # Each root has its own administrative deadline, within a one-hour host cap.
        remaining = min(
            settings.get("scanSeconds", 90),
            budget - (time.monotonic() - started),
        )
        if remaining <= 0:
            row["status"] = "skipped"
        else:
            deadline = time.monotonic() + remaining
            paths = [target] + [p for p in nested if p not in memory_mounts]
            parts = []
            for index, path in enumerate(paths):
                left = max(0.1, (deadline - time.monotonic()) / (len(paths) - index))
                current, _ = inventory(path)
                parts.append(
                    scan(
                        path,
                        left,
                        apparent=current["fstype"].startswith("fuse."),
                        excluded_names=excluded_names,
                        excluded=[
                            p for p in nested if p.startswith(path.rstrip("/") + "/")
                        ],
                    )
                )
            row.update(parts[0])
            row["path"] = root
            if len(parts) > 1:
                row["children"] = (
                    parts[0]["children"]
                    + [
                        {k: v for k, v in p.items() if k != "children"}
                        for p in parts[1:]
                    ]
                )[:32]
                row["status"] = (
                    "ok" if all(p["status"] == "ok" for p in parts) else "partial"
                )
                row["bytes"] = (
                    sum(p["bytes"] for p in parts) if row["status"] == "ok" else None
                )
                observed = [
                    p.get("bytes")
                    if p.get("bytes") is not None
                    else p.get("observedBytes")
                    for p in parts
                ]
                row["observedBytes"] = (
                    sum(value for value in observed if value is not None)
                    if row["status"] != "ok"
                    and any(value is not None for value in observed)
                    else None
                )
        result["directories"].append(row)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
