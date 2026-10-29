"""Daily, bounded RAM accounting independent of the GPU scheduler."""

from __future__ import annotations

import copy
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Protocol

from .config import MonitorConfig, is_safe_alias
from .probe import (
    _ActiveProcessRegistry,
    _ProcessCancelled,
    _ProcessOutputLimitExceeded,
    _run_bounded_process,
    ssh_environment,
)
from .remote_script import USER_MEMORY_SCRIPT
from .ssh_failures import classify_ssh_failure

INTERVAL_SECONDS = 86_400
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class MemorySource(Protocol):
    def snapshot(self) -> dict: ...


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def parse_memory(text: str) -> dict:
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != "USER_MEMORY_V1" or lines[-1] != "END":
        raise ValueError("incomplete memory sample")
    header = lines[1].split()
    if len(header) != 2 or header[0] != "TOTAL" or not header[1].isdigit():
        raise ValueError("invalid memory total")
    total_kib = int(header[1])
    if not 0 < total_kib <= 2**50:
        raise ValueError("invalid memory total")
    owners: dict[int, dict] = {}
    for line in lines[2:-1]:
        fields = line.split()
        if len(fields) != 3 or not fields[0].isdigit() or not fields[2].isdigit():
            raise ValueError("invalid process memory row")
        uid, name, rss = int(fields[0]), fields[1], int(fields[2])
        if uid > 2**32 - 1 or len(name) > 64 or rss > 2**50:
            raise ValueError("process memory row exceeds limits")
        owner = owners.setdefault(
            uid, {"uid": uid, "user": name, "rssKiB": 0, "processes": 0}
        )
        owner["rssKiB"] += rss
        owner["processes"] += 1
        if len(owners) > 4096:
            raise ValueError("too many memory owners")
    return {
        "totalKiB": total_kib,
        "users": sorted(owners.values(), key=lambda row: (-row["rssKiB"], row["uid"])),
    }


class DailyUserMemory:
    """One worker; HTTP reads never launch SSH or wait for a collection."""

    metric = "rss"
    sample_key = "users"
    script = USER_MEMORY_SCRIPT
    parser = staticmethod(parse_memory)
    timeout_seconds = None

    def __init__(
        self,
        config: MonitorConfig,
        hosts: Callable[[], Iterable[str]],
        *,
        runner=_run_bounded_process,
        clock=time.time,
        monotonic=time.monotonic,
    ) -> None:
        self._config, self._hosts = config, hosts
        self._runner, self._clock, self._monotonic = runner, clock, monotonic
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        self._deadlines: dict[str, float] = {}
        self._stop = threading.Event()
        self._registry = _ActiveProcessRegistry()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="mocop-user-memory", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._registry.cancel()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._thread is None or not self._thread.is_alive():
            self._registry.close()

    def snapshot(self) -> dict:
        hosts = sorted({host for host in self._hosts() if is_safe_alias(host)})
        with self._lock:
            rows = [
                copy.deepcopy(
                    self._records.get(
                        host,
                        {
                            "host": host,
                            "status": "pending",
                            self.sample_key: [],
                            "sampledAt": None,
                        },
                    )
                )
                for host in hosts
            ]
        return {
            "intervalSeconds": INTERVAL_SECONDS,
            "metric": self.metric,
            "servers": rows,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            self.collect_due()
            self._stop.wait(30)

    def collect_due(self) -> None:
        hosts = sorted({host for host in self._hosts() if is_safe_alias(host)})
        with self._lock:
            self._records = {
                host: row for host, row in self._records.items() if host in hosts
            }
            self._deadlines = {
                host: when for host, when in self._deadlines.items() if host in hosts
            }
        for host in hosts:
            if self._stop.is_set():
                return
            if self._monotonic() < self._deadlines.get(host, 0):
                continue
            now = self._clock()
            with self._lock:
                previous = self._records.get(host, {})
                self._records[host] = {
                    **previous,
                    "host": host,
                    "status": "collecting",
                    self.sample_key: previous.get(self.sample_key, []),
                    "sampledAt": previous.get("sampledAt"),
                }
            try:
                sample = self._collect(host)
                row = {
                    "host": host,
                    "status": "ok",
                    "sampledAt": timestamp(self._clock()),
                    "message": None,
                    **sample,
                }
            except _ProcessCancelled:
                return
            except (
                OSError,
                ValueError,
                subprocess.TimeoutExpired,
                _ProcessOutputLimitExceeded,
            ) as exc:
                message = (
                    str(exc)
                    if isinstance(exc, MemoryCollectionError)
                    else "Memory collection failed or exceeded limits"
                )
                row = {
                    **previous,
                    "host": host,
                    "status": "error",
                    "message": message,
                    self.sample_key: previous.get(self.sample_key, []),
                    "sampledAt": previous.get("sampledAt"),
                }
            row.update(
                attemptedAt=timestamp(now),
                nextAttemptAt=timestamp(self._clock() + INTERVAL_SECONDS),
            )
            with self._lock:
                self._records[host] = row
                self._deadlines[host] = self._monotonic() + INTERVAL_SECONDS

    def _collect(self, host: str, *, script=None, timeout=None) -> dict:
        config = self._config
        command = (
            ["sh", "-s"]
            if host == config.local_host
            else [
                "ssh",
                "-F",
                str(config.ssh_config),
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                f"ConnectTimeout={config.connect_timeout_seconds}",
                "--",
                host,
                "sh",
                "-s",
            ]
        )
        result = self._runner(
            command,
            input_text=self.script if script is None else script,
            timeout_seconds=timeout
            or self.timeout_seconds
            or config.probe_timeout_seconds,
            max_output_bytes=MAX_OUTPUT_BYTES,
            environment=ssh_environment(),
            cancel_event=self._stop,
            process_registry=self._registry,
        )
        if result.returncode:
            raise MemoryCollectionError(
                classify_ssh_failure(result.stderr)
                if result.returncode == 255
                else "Remote memory command failed"
            )
        return self.parser(result.stdout)


class MemoryCollectionError(ValueError):
    pass
