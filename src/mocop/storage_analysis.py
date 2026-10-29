"""Bounded scheduled personal storage analysis, with private persistent history."""

import copy
import json
import math
import os
import shlex
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import is_safe_alias
from .probe import _ActiveProcessRegistry, _run_bounded_process, ssh_environment
from .remote_script import PERSONAL_STORAGE_SCRIPT
from .user_memory import timestamp

POLICY_PATH = Path.home() / ".config/mocop/storage-policy.json"
HISTORY_PATH = Path.home() / ".local/state/mocop/storage-history.json"
INTERVAL = 21600


def private_json(path, maximum):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size > maximum
    ):
        raise ValueError("invalid private storage file")
    return json.loads(path.read_text())


def validate_sample(data, roots):
    if not isinstance(data, dict) or data.get("protocol") != "PERSONAL_STORAGE_V1":
        raise ValueError("invalid storage protocol")
    rows, filesystems = data.get("directories"), data.get("filesystems")
    if (
        not isinstance(rows, list)
        or len(rows) != len(roots)
        or not isinstance(filesystems, list)
        or len(filesystems) > 64
    ):
        raise ValueError("invalid storage result")
    seen = set()
    for row in rows:
        if row.get("path") not in roots or row["path"] in seen:
            raise ValueError("unexpected storage root")
        seen.add(row["path"])
        if row.get("status") not in (
            "ok",
            "missing",
            "linked",
            "inventory_error",
            "skipped",
            "timeout",
            "unreadable",
            "partial",
        ):
            raise ValueError("invalid storage state")
        if row["status"] == "ok":
            if type(row.get("bytes")) is not int or not 0 <= row["bytes"] <= 2**60:
                raise ValueError("invalid storage total")
        elif row.get("bytes") is not None:
            raise ValueError("incomplete total")
        children = row.get("children")
        if not isinstance(children, list) or len(children) > 32:
            raise ValueError("invalid storage children")
        for child in children:
            path = child.get("path")
            if (
                not isinstance(path, str)
                or not path.startswith(row["path"].rstrip("/") + "/")
                or ".." in path.split("/")
                or len(path) > 4096
                or not (
                    (type(child.get("bytes")) is int and 0 <= child["bytes"] <= 2**60)
                    or (
                        child.get("bytes") is None
                        and child.get("status")
                        in ("timeout", "unreadable", "skipped", "partial")
                    )
                )
            ):
                raise ValueError("invalid storage child")
    for fs in filesystems:
        if (
            not isinstance(fs.get("key"), str)
            or len(fs["key"]) > 4096
            or not isinstance(fs.get("mountPoint"), str)
            or not fs["mountPoint"].startswith("/")
        ):
            raise ValueError("invalid storage filesystem")
        for key in ("totalBytes", "availableBytes", "usedBytes"):
            if type(fs.get(key)) is not int or abs(fs[key]) > 2**60:
                raise ValueError("invalid filesystem capacity")
    return data


def analysis(sample, history):
    result = copy.deepcopy(sample)
    for row in result.get("directories", []):
        old = next(
            (
                r
                for s in reversed(history)
                for r in s.get("directories", [])
                if r.get("path") == row["path"]
                and r.get("status") == "ok"
                and s.get("excludeNames", []) == sample.get("excludeNames", [])
            ),
            None,
        )
        row["changeBytes"] = (
            row["bytes"] - old["bytes"]
            if row.get("status") == "ok" and old is not None
            else None
        )
    for fs in result.get("filesystems", []):
        if fs.get("capacityVirtual"):
            fs.update(
                changeAvailableBytes=None,
                daysUntilFull=None,
                percentUsed=None,
                pressure="unknown",
            )
            continue
        prior = [
            s
            for s in history
            if any(f.get("key") == fs["key"] for f in s.get("filesystems", []))
        ]
        fs["changeAvailableBytes"] = None
        fs["daysUntilFull"] = None
        if prior:
            old = next(f for f in prior[-1]["filesystems"] if f.get("key") == fs["key"])
            fs["changeAvailableBytes"] = fs["availableBytes"] - old["availableBytes"]
        if len(prior) >= 2:
            first = prior[max(0, len(prior) - 8)]
            old = next(f for f in first["filesystems"] if f.get("key") == fs["key"])
            elapsed = result["epoch"] - first["epoch"]
            growth = old["availableBytes"] - fs["availableBytes"]
            if elapsed >= 21600 and growth > 1024**3:
                fs["daysUntilFull"] = fs["availableBytes"] / growth * elapsed / 86400
        fs["percentUsed"] = (
            round(100 * fs["usedBytes"] / fs["totalBytes"], 1)
            if fs["totalBytes"]
            else None
        )
        fs["pressure"] = (
            "critical"
            if fs["availableBytes"] < 5 * 1024**3
            or (fs["percentUsed"] is not None and fs["percentUsed"] >= 95)
            else "warning"
            if fs["percentUsed"] is not None and fs["percentUsed"] >= 85
            else "ok"
        )
    result["knownBytes"] = sum(
        r["bytes"] for r in result.get("directories", []) if r.get("status") == "ok"
    )
    result["complete"] = all(
        r.get("status") in ("ok", "linked") for r in result.get("directories", [])
    )
    return result


class PersonalStorageMonitor:
    def __init__(
        self,
        config,
        hosts,
        *,
        policy_path=POLICY_PATH,
        history_path=HISTORY_PATH,
        runner=_run_bounded_process,
    ):
        self.config, self.hosts, self.policy_path, self.history_path, self.runner = (
            config,
            hosts,
            Path(policy_path),
            Path(history_path),
            runner,
        )
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.registry = _ActiveProcessRegistry()
        self.thread = None
        self.records = {}
        self.histories = {}
        self.deadlines = {}
        try:
            self.histories = private_json(self.history_path, 16 * 1024**2)["histories"]
            for host, records in self.histories.items():
                if records:
                    self.records[host] = analysis(records[-1], records[:-1])
                    age = time.time() - records[-1]["epoch"]
                    self.deadlines[host] = time.monotonic() + max(0, INTERVAL - age)
        except (OSError, ValueError, KeyError, TypeError):
            self.histories = {}

    def start(self):
        self.thread = threading.Thread(
            target=self.run, name="personal-storage-analysis", daemon=True
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.registry.cancel()
        if self.thread:
            self.thread.join(timeout=5)

    def policy(self):
        try:
            data = private_json(self.policy_path, 65536)
        except FileNotFoundError:
            return {}
        hosts = set(self.hosts())
        result = {}
        for host, config in data.get("hosts", {}).items():
            if host not in hosts or not is_safe_alias(host):
                continue
            roots = config.get("roots")
            if not isinstance(roots, list) or not roots or len(roots) > 16:
                raise ValueError("invalid personal root policy")
            from .storage_probe import valid_exclusions, valid_path

            excluded_names = config.get("excludeNames", [])
            if not valid_exclusions(excluded_names):
                raise ValueError("invalid storage exclusions")

            if not all(
                valid_path(p)
                and not any(
                    c in p.split("/") for c in (".ssh", ".gnupg", *excluded_names)
                )
                for p in roots
            ):
                raise ValueError("invalid personal storage root")
            if any(
                a != b and b.startswith(a.rstrip("/") + "/")
                for a in roots
                for b in roots
            ):
                raise ValueError("overlapping personal roots")
            result[host] = config
        return result

    def snapshot(self):
        priorities = self.policy()
        with self.lock:
            hosts = set(self.hosts())
            rows = [copy.deepcopy(v) for h, v in self.records.items() if h in hosts]
            rows = [
                r for r in rows if self.matches_policy(r, priorities.get(r["host"], {}))
            ]
            for row in rows:
                row["history"] = [
                    {
                        "epoch": r["epoch"],
                        "knownBytes": sum(
                            d["bytes"]
                            for d in r.get("directories", [])
                            if d.get("status") == "ok"
                        ),
                        "complete": all(
                            d.get("status") in ("ok", "linked")
                            for d in r.get("directories", [])
                        ),
                        "free": [
                            {"key": f["key"], "bytes": f["availableBytes"]}
                            for f in r.get("filesystems", [])
                        ],
                    }
                    for r in self.histories.get(row["host"], [])
                    if self.matches_policy(r, priorities.get(row["host"], {}))
                ]
        present = {row["host"] for row in rows}
        for host, policy in priorities.items():
            if host not in present:
                rows.append(
                    {
                        "host": host,
                        "label": policy.get("label", host),
                        "status": "pending",
                        "sampledAt": None,
                        "knownBytes": None,
                        "complete": False,
                        "directories": [
                            {
                                "path": p,
                                "bytes": None,
                                "status": "pending",
                                "children": [],
                            }
                            for p in policy["roots"]
                        ],
                        "filesystems": [],
                        "history": [],
                    }
                )
        for row in rows:
            row["priority"] = -priorities.get(row["host"], {}).get("priority", 0)
            row["stale"] = row.get("status") == "error" or (
                row.get("epoch") is not None
                and time.time() - row["epoch"] > INTERVAL * 1.5
            )
            row["nextAttemptAt"] = timestamp(
                time.time()
                + max(0, self.deadlines.get(row["host"], 0) - time.monotonic())
            )
        return {
            "enabled": True,
            "intervalSeconds": INTERVAL,
            "retentionDays": 30,
            "servers": rows,
            "scope": "configured_personal_directories",
            "readOnly": True,
        }

    def legacy_snapshot(self):
        snapshot = self.snapshot()
        rows = []
        for server in snapshot["servers"]:
            directories = [
                {
                    "path": r["path"],
                    "mountPoint": r.get("mountPoint", "/"),
                    "sizeKiB": r["bytes"] // 1024
                    if r.get("bytes") is not None
                    else None,
                    "status": r["status"]
                    if r["status"] in ("ok", "timeout", "skipped")
                    else "unreadable",
                }
                for r in server.get("directories", [])
            ]
            filesystems = [
                {
                    "mountPoint": f["mountPoint"],
                    "aliases": [f["mountPoint"]],
                    "kind": f["kind"],
                    "fsType": f["fsType"],
                    "scanRoot": True,
                    "capacityVirtual": f.get("capacityVirtual", False),
                    "totalKiB": None
                    if f.get("capacityVirtual")
                    else f["totalBytes"] // 1024,
                    "availableKiB": None
                    if f.get("capacityVirtual")
                    else f["availableBytes"] // 1024,
                    "usedKiB": None
                    if f.get("capacityVirtual")
                    else f["usedBytes"] // 1024,
                    "percentUsed": f.get("percentUsed"),
                }
                for f in server.get("filesystems", [])
            ]
            rows.append(
                {
                    **server,
                    "directories": directories,
                    "filesystems": filesystems,
                    "inventoryAt": server.get("sampledAt"),
                }
            )
        return {"intervalSeconds": INTERVAL, "metric": "disk", "servers": rows}

    def persist(self):
        self.history_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.history_path.with_name(self.history_path.name + ".tmp")
        raw = json.dumps({"histories": self.histories}, ensure_ascii=True)
        if len(raw.encode()) > 16 * 1024**2:
            raise ValueError("storage history exceeds limit")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.history_path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def collect(self, item):
        host, policy = item
        with self.lock:
            previous = self.records.get(host, {})
            if not self.matches_policy(previous, policy):
                previous = {
                    "directories": [
                        {"path": p, "bytes": None, "status": "pending", "children": []}
                        for p in policy["roots"]
                    ]
                }
            self.records[host] = {
                **previous,
                "host": host,
                "status": "collecting",
                "label": policy.get("label", host),
                "priority": -policy.get("priority", 0),
            }
        try:
            command = [
                "ssh",
                "-F",
                str(self.config.ssh_config),
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=2",
                "-o",
                "ConnectTimeout=10",
                "--",
                host,
                "python3 -c " + shlex.quote(PERSONAL_STORAGE_SCRIPT),
            ]
            output = self.runner(
                command,
                input_text=json.dumps(
                    {
                        "roots": policy["roots"],
                        "excludeNames": policy.get("excludeNames", []),
                    }
                ),
                timeout_seconds=260,
                max_output_bytes=1024**2,
                environment=ssh_environment(),
                cancel_event=self.stop_event,
                process_registry=self.registry,
            )
            if output.returncode:
                raise ValueError("storage SSH scan failed")
            sample = validate_sample(json.loads(output.stdout), policy["roots"])
            sample.update(
                excludeNames=policy.get("excludeNames", []),
                host=host,
                label=policy.get("label", host),
                priority=-policy.get("priority", 0),
                status="ok",
                sampledAt=timestamp(time.time()),
                epoch=time.time(),
            )
            with self.lock:
                history = self.histories.get(host, [])
                row = analysis(sample, history)
                self.histories[host] = [
                    r for r in history if r["epoch"] >= time.time() - 30 * 86400
                ][-119:] + [sample]
                self.records[host] = row
                self.persist()
        except Exception:
            with self.lock:
                self.records[host] = {
                    **previous,
                    "host": host,
                    "label": policy.get("label", host),
                    "priority": -policy.get("priority", 0),
                    "status": "error",
                    "message": "个人目录采集失败或超时，保留上次成功观测",
                }
        finally:
            failed = self.records.get(host, {}).get("status") == "error"
            self.deadlines[host] = time.monotonic() + (900 if failed else INTERVAL)

    def run(self):
        while not self.stop_event.is_set():
            try:
                items = sorted(
                    self.policy().items(), key=lambda x: -x[1].get("priority", 0)
                )
                due = [
                    item
                    for item in items
                    if time.monotonic() >= self.deadlines.get(item[0], 0)
                    or not self.matches_policy(self.records.get(item[0], {}), item[1])
                ]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(self.collect, due))
            except (OSError, ValueError, TypeError):
                pass
            self.stop_event.wait(30)

    @staticmethod
    def matches_policy(record, policy):
        return [r.get("path") for r in record.get("directories", [])] == policy.get(
            "roots"
        ) and record.get("excludeNames", []) == policy.get("excludeNames", [])


def merge_live_capacity(storage, fleet):
    result = copy.deepcopy(storage)
    hosts = {r["host"]: r for r in fleet.get("servers", [])}
    for server in result.get("servers", []):
        live = hosts.get(server["host"])
        if live is None or live.get("status") != "online" or live.get("stale"):
            continue
        disks = {d["mountpoint"]: d for d in live.get("system", {}).get("disks", [])}
        for fs in server.get("filesystems", []):
            if fs.get("capacityVirtual"):
                continue
            disk = disks.get(fs["mountPoint"])
            if disk is None or not all(
                type(disk.get(k)) in (int, float) and math.isfinite(disk[k])
                for k in ("total_mib", "used_mib", "available_mib", "used_pct")
            ):
                continue
            available = int(disk["available_mib"] * 1024**2)
            old_available = fs["availableBytes"]
            if fs.get("changeAvailableBytes") is not None:
                fs["changeAvailableBytes"] += available - old_available
            if fs.get("daysUntilFull") is not None and old_available > 0:
                fs["daysUntilFull"] *= available / old_available
            fs.update(
                totalBytes=int(disk["total_mib"] * 1024**2),
                usedBytes=int(disk["used_mib"] * 1024**2),
                availableBytes=available,
                percentUsed=disk["used_pct"],
                capacitySampledAt=live.get("lastSuccessAt"),
            )
            fs["pressure"] = (
                "critical"
                if available < 5 * 1024**3 or disk["used_pct"] >= 95
                else "warning"
                if disk["used_pct"] >= 85
                else "ok"
            )
    return result


def handle_read(handler, request_url):
    if request_url.path not in (
        "/api/user-memory",
        "/api/directory-disk",
        "/api/storage-analysis",
    ):
        return False
    disk = request_url.path != "/api/user-memory"
    source = getattr(
        handler.monitor_server, "directory_disk" if disk else "user_memory"
    )
    if source is None:
        result = {
            "intervalSeconds": INTERVAL if disk else 86400,
            "metric": "disk" if disk else "rss",
            "servers": [],
        }
    elif request_url.path == "/api/directory-disk" and hasattr(
        source, "legacy_snapshot"
    ):
        result = source.legacy_snapshot()
    else:
        result = source.snapshot()
    if request_url.path == "/api/storage-analysis":
        result = merge_live_capacity(
            result, handler.monitor_server.state.snapshot_view()
        )
    handler._send_json(result)
    return True
