"""Read-only SSH connection diagnosis for configured monitoring targets.

`mocop doctor` verifies, per alias: non-interactive reachability under the
probe transport discipline, OpenSSH connection-reuse configuration, and the
control-socket directory permissions. It never mutates SSH configuration and
never prints remote usernames or addresses. Connection tests follow the
operator's existing OpenSSH policy, so a configured `ControlMaster auto` may
start its usual control master exactly as any probe would.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from typing import TextIO

from .config import MonitorConfig, is_safe_alias
from .probe import _run_bounded_process, _safe_ssh_failure

_SSH_G_TIMEOUT_SECONDS = 10
_SSH_G_MAX_OUTPUT_BYTES = 262_144
_PROBE_MAX_OUTPUT_BYTES = 65_536
_REUSE_MODES = frozenset({"auto", "autoask", "yes", "ask"})

_QUERY_KEYS = frozenset(
    {
        "controlmaster",
        "controlpath",
        "controlpersist",
        "proxycommand",
        "proxyjump",
        "serveraliveinterval",
        "serveralivecountmax",
        "stricthostkeychecking",
    }
)


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    return environment


def _resolved_options(alias: str, config: MonitorConfig) -> dict[str, str] | None:
    """Return the lowercase OpenSSH options `ssh -G` resolves for the alias."""
    try:
        completed = _run_bounded_process(
            ["ssh", "-G", "-F", str(config.ssh_config), "--", alias],
            input_text="",
            timeout_seconds=_SSH_G_TIMEOUT_SECONDS,
            max_output_bytes=_SSH_G_MAX_OUTPUT_BYTES,
            environment=_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    options: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition(" ")
        key = key.strip().lower()
        if key in _QUERY_KEYS:
            options[key] = value.strip()
    return options


def _timed_probe(
    alias: str, config: MonitorConfig, *, reuse: bool
) -> tuple[float | None, str | None]:
    """Run one bounded `true` over SSH; return (latency_ms, failure_reason)."""
    command = [
        "ssh",
        "-F",
        str(config.ssh_config),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        f"ConnectTimeout={config.connect_timeout_seconds}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "LogLevel=ERROR",
    ]
    if not reuse:
        command += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    command += ["--", alias, "true"]
    started = time.monotonic()
    try:
        completed = _run_bounded_process(
            command,
            input_text="",
            timeout_seconds=config.probe_timeout_seconds,
            max_output_bytes=_PROBE_MAX_OUTPUT_BYTES,
            environment=_environment(),
        )
    except subprocess.TimeoutExpired:
        return None, "SSH connection attempt timed out"
    except OSError:
        return None, "Local SSH client could not be started"
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    if completed.returncode != 0:
        return None, _safe_ssh_failure(completed.stderr)
    return latency_ms, None


def _reuse_findings(options: dict[str, str]) -> tuple[dict[str, object], list[str]]:
    warnings: list[str] = []
    master = options.get("controlmaster", "no")
    persist = options.get("controlpersist", "no")
    socket_path = options.get("controlpath", "")
    reuse_enabled = master in _REUSE_MODES

    socket_directory: str | None = None
    socket_directory_mode: str | None = None
    if reuse_enabled:
        if not socket_path or socket_path.lower() == "none":
            warnings.append("ControlMaster is enabled but no ControlPath is configured")
        else:
            socket_directory = os.path.dirname(socket_path) or "."
            try:
                directory_stat = os.stat(socket_directory)
            except OSError:
                warnings.append(
                    "control socket directory does not exist; "
                    "OpenSSH cannot create the multiplex socket"
                )
            else:
                mode = stat.S_IMODE(directory_stat.st_mode)
                socket_directory_mode = format(mode, "04o")
                if mode & 0o077:
                    warnings.append(
                        "control socket directory permits group/other access; "
                        "restrict it to 0700"
                    )
        if persist in {"no", "0"}:
            warnings.append(
                "ControlPersist is disabled; the master closes between probe "
                "cycles and reuse saves nothing"
            )
    else:
        warnings.append(
            "connection reuse is disabled; enable ControlMaster auto with "
            "ControlPersist to remove per-probe connection setup"
        )

    findings: dict[str, object] = {
        "controlMaster": master,
        "controlPersist": persist,
        "reuseEnabled": reuse_enabled,
        "socketDirectory": socket_directory,
        "socketDirectoryMode": socket_directory_mode,
    }
    return findings, warnings


def _diagnose_host(
    alias: str, config: MonitorConfig, *, probe_connection: bool
) -> dict[str, object]:
    report: dict[str, object] = {"alias": alias, "warnings": []}
    warnings: list[str] = report["warnings"]

    if not is_safe_alias(alias):
        report["reachable"] = False
        warnings.append("alias contains unsafe characters")
        return report

    options = _resolved_options(alias, config)
    if options is None:
        report["reachable"] = False
        warnings.append("ssh -G could not resolve the alias")
        return report

    reuse, reuse_warnings = _reuse_findings(options)
    report["reuse"] = reuse
    warnings.extend(reuse_warnings)
    report["proxyJump"] = bool(options.get("proxyjump") or options.get("proxycommand"))

    if probe_connection:
        cold_ms, cold_failure = _timed_probe(alias, config, reuse=False)
        reuse_ms, reuse_failure = _timed_probe(alias, config, reuse=True)
        report["coldLatencyMs"] = cold_ms
        report["reuseLatencyMs"] = reuse_ms
        failure = cold_failure if cold_ms is None else reuse_failure
        report["reachable"] = cold_ms is not None or reuse_ms is not None
        if not report["reachable"] and failure:
            warnings.append(f"unreachable: {failure}")
    return report


def run_doctor(
    config: MonitorConfig,
    *,
    host_filter: tuple[str, ...] = (),
    probe_connection: bool = True,
    as_json: bool = False,
    stdout: TextIO = sys.stdout,
) -> int:
    """Diagnose configured aliases; return 0 when every alias is usable."""
    remote_hosts = tuple(host for host in config.hosts if host != config.local_host)
    if host_filter:
        unknown = tuple(host for host in host_filter if host not in remote_hosts)
        if unknown:
            print(
                f"unknown monitored aliases: {', '.join(sorted(unknown))}",
                file=sys.stderr,
            )
            return 2
        remote_hosts = tuple(host for host in remote_hosts if host in host_filter)

    keepalive_interval = max(2, config.connect_timeout_seconds // 2)
    transport = {
        "connectTimeoutSeconds": config.connect_timeout_seconds,
        "probeTimeoutSeconds": config.probe_timeout_seconds,
        "serverAliveIntervalSeconds": keepalive_interval,
        "serverAliveCountMax": 2,
    }
    reports = [
        _diagnose_host(alias, config, probe_connection=probe_connection)
        for alias in remote_hosts
    ]
    failed = tuple(
        report["alias"] for report in reports if report.get("reachable") is False
    )

    if as_json:
        json.dump(
            {
                "transportDiscipline": transport,
                "localHost": config.local_host,
                "hosts": reports,
                "status": "ok" if not failed else "failed",
            },
            stdout,
            indent=2,
        )
        stdout.write("\n")
    else:
        stdout.write(
            "probe transport: ConnectTimeout="
            f"{transport['connectTimeoutSeconds']}s "
            f"ServerAlive={keepalive_interval}sx2 "
            f"probe timeout {transport['probeTimeoutSeconds']}s\n"
        )
        if config.local_host:
            stdout.write(f"{config.local_host}: local target, SSH not used\n")
        for report in reports:
            _write_text_report(report, stdout)
        if not remote_hosts:
            stdout.write("no remote SSH aliases are configured\n")

    return 1 if failed else 0


def _write_text_report(report: dict[str, object], stdout: TextIO) -> None:
    alias = report["alias"]
    reachable = report.get("reachable")
    if reachable is True:
        cold = report.get("coldLatencyMs")
        reuse_latency = report.get("reuseLatencyMs")
        cold_text = f"{cold:.0f} ms" if isinstance(cold, float) else "failed"
        reuse_text = (
            f"{reuse_latency:.0f} ms" if isinstance(reuse_latency, float) else "failed"
        )
        stdout.write(f"{alias}: reachable (cold {cold_text}, reuse {reuse_text})\n")
    elif reachable is False:
        stdout.write(f"{alias}: UNREACHABLE\n")
    else:
        stdout.write(f"{alias}: configuration inspected (connection test skipped)\n")
    reuse = report.get("reuse")
    if isinstance(reuse, dict):
        directory = reuse.get("socketDirectory")
        mode = reuse.get("socketDirectoryMode")
        socket_text = f" socket dir {directory} ({mode})" if directory and mode else ""
        stdout.write(
            f"  reuse: ControlMaster={reuse['controlMaster']} "
            f"ControlPersist={reuse['controlPersist']}{socket_text}\n"
        )
    if report.get("proxyJump"):
        stdout.write("  route: proxy jump or command configured\n")
    for warning in report.get("warnings", ()):
        stdout.write(f"  warning: {warning}\n")
