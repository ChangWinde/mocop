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
import re
import stat
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import TextIO

from . import probe
from .config import MonitorConfig, is_safe_alias
from .discovery import OpenSshConfigHostSource
from .lifecycle import user_unit_path
from .probe import (
    OpenSshLinuxResourceProbe,
    ResourceProbe,
    _ActiveProcessRegistry,
    _BoundedProcessResult,
    _ProcessCancelled,
    _ProcessOutputLimitExceeded,
    _safe_ssh_failure,
    ssh_environment,
)
from .remote_script import _COMBINED_QUERY_FIELDS
from .ssh_topology import resolve_ssh_options

_SSH_G_TIMEOUT_SECONDS = 10


def _refuse(message: str, code: str, as_json: bool, stdout: TextIO) -> int:
    """Exit 2 for a usage or discovery refusal; JSON stays on stdout."""
    if as_json:
        json.dump(
            {"ok": False, "code": code, "error": message},
            stdout,
            ensure_ascii=False,
            indent=2,
        )
        stdout.write("\n")
    else:
        print(message, file=sys.stderr)
    return 2


_PROBE_MAX_OUTPUT_BYTES = 65_536
_REUSE_MODES = frozenset({"auto", "autoask", "yes", "ask"})
_SERVER_ALIVE_COUNT_MAX = 2
_CONTROL_PERSIST_MARGIN_SECONDS = 2.0
_BUDGET_EXHAUSTED = "host diagnosis time budget exhausted"
_PROFILE_MARKER = "MOCOP_PROFILE_V1"

_QUERY_KEYS = frozenset(
    {
        "controlmaster",
        "controlpath",
        "controlpersist",
        "proxycommand",
        "proxyjump",
    }
)

# Doctor-owned, self-contained profiling script. It reproduces the fixed
# collection script's non-NVIDIA passes (same files and external commands,
# output discarded) and the combined NVIDIA telemetry query, bracketing each
# stage with Linux monotonic timestamps. The stages are mutually exclusive,
# so the transport share is the locally measured total minus the remote time.
_PROFILE_SCRIPT = r"""
LC_ALL=C
export LC_ALL
monotonic_ns() {
  awk '{ printf "%.0f\n", $1 * 1000000000; exit }' /proc/uptime
}
t0=$(monotonic_ns)
awk '{ next }' /proc/stat /proc/meminfo /proc/loadavg /proc/uptime /proc/net/dev /sys/block/*/stat >/dev/null 2>&1
df -PTk 2>/dev/null | awk 'NR > 1 { next }' >/dev/null 2>&1
t1=$(monotonic_ns)
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=__COMBINED_QUERY__ --format=csv,noheader,nounits >/dev/null 2>&1
  nvidia_status=$?
else
  nvidia_status=127
fi
t2=$(monotonic_ns)
printf '__MARKER__\t%s\t%s\t%s\t%s\n' "$t0" "$t1" "$t2" "$nvidia_status"
""".replace("__COMBINED_QUERY__", ",".join(_COMBINED_QUERY_FIELDS)).replace(
    "__MARKER__", _PROFILE_MARKER
)


def _effective_probe_timeout_seconds(alias: str, config: MonitorConfig) -> float:
    """Per-host effective probe timeout, matching the production probe."""
    override = config.host_override(alias)
    if override is not None and override.probe_timeout_seconds is not None:
        return override.probe_timeout_seconds
    return config.probe_timeout_seconds


def _effective_poll_interval_seconds(alias: str, config: MonitorConfig) -> float:
    """Per-host effective collection interval, matching the scheduler."""
    override = config.host_override(alias)
    if override is not None and override.poll_interval_seconds is not None:
        return override.poll_interval_seconds
    return config.poll_interval_seconds


def _keepalive_interval_seconds(config: MonitorConfig) -> int:
    """Mirror the production probe's ServerAliveInterval derivation."""
    return max(2, config.connect_timeout_seconds // 2)


def _host_budget_seconds(
    alias: str, config: MonitorConfig, *, collect: bool = False
) -> float:
    """Bound one host's total diagnosis time.

    The budget covers alias resolution plus three connection-bound stages
    (cold, reuse, profile) at the host's effective probe timeout, and one
    more stage when a production collection run (--probe) is requested.
    """
    stages = 4 if collect else 3
    return _SSH_G_TIMEOUT_SECONDS + stages * _effective_probe_timeout_seconds(
        alias, config
    )


def _stage_timeout(deadline: float, stage_timeout_seconds: float) -> float | None:
    """Cap one stage's timeout by the host's remaining diagnosis budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(stage_timeout_seconds, remaining)


def _run_remote(
    alias: str,
    config: MonitorConfig,
    remote_args: tuple[str, ...],
    *,
    timeout_seconds: float,
    input_text: str = "",
    reuse: bool = True,
    max_output_bytes: int = _PROBE_MAX_OUTPUT_BYTES,
    process_registry: _ActiveProcessRegistry | None = None,
) -> tuple[float | None, _BoundedProcessResult | None, str | None]:
    """Run one bounded remote command under the production transport options.

    Returns (latency_ms, completed, failure_reason). The failure reason is
    set only for local failures: spawn errors, timeouts, and output limits.
    """
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
        f"ServerAliveInterval={_keepalive_interval_seconds(config)}",
        "-o",
        f"ServerAliveCountMax={_SERVER_ALIVE_COUNT_MAX}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "LogLevel=ERROR",
    ]
    if not reuse:
        command += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    command += ["--", alias, *remote_args]
    started = time.monotonic()
    try:
        completed = probe._run_bounded_process(
            command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            environment=ssh_environment(),
            process_registry=process_registry,
        )
    except subprocess.TimeoutExpired:
        return None, None, "SSH connection attempt timed out"
    except _ProcessOutputLimitExceeded:
        return None, None, "remote output exceeded the configured limit"
    except _ProcessCancelled:
        return None, None, "host diagnosis was cancelled"
    except OSError:
        return None, None, "Local SSH client could not be started"
    return round((time.monotonic() - started) * 1000, 1), completed, None


def _remote_failure_reason(completed: _BoundedProcessResult) -> str | None:
    """Classify one completed remote command without echoing remote output.

    Exit status 255 is the OpenSSH client's own transport failure; any other
    non-zero status came from the remote command itself.
    """
    if completed.returncode == 0:
        return None
    if completed.returncode == 255:
        return _safe_ssh_failure(completed.stderr)
    return f"remote command failed (exit {completed.returncode})"


def _timed_remote(
    alias: str,
    config: MonitorConfig,
    remote_args: tuple[str, ...],
    *,
    timeout_seconds: float,
    input_text: str = "",
    reuse: bool = True,
    max_output_bytes: int = _PROBE_MAX_OUTPUT_BYTES,
    process_registry: _ActiveProcessRegistry | None = None,
) -> tuple[float | None, str | None]:
    """Run one bounded remote command; return (latency_ms, failure_reason)."""
    latency_ms, completed, failure = _run_remote(
        alias,
        config,
        remote_args,
        timeout_seconds=timeout_seconds,
        input_text=input_text,
        reuse=reuse,
        max_output_bytes=max_output_bytes,
        process_registry=process_registry,
    )
    if failure is not None or completed is None:
        return None, failure
    reason = _remote_failure_reason(completed)
    if reason is not None:
        return None, reason
    return latency_ms, None


def _timed_probe(
    alias: str,
    config: MonitorConfig,
    *,
    reuse: bool,
    timeout_seconds: float,
    process_registry: _ActiveProcessRegistry | None = None,
) -> tuple[float | None, str | None]:
    """Run one bounded `true` over SSH; return (latency_ms, failure_reason)."""
    return _timed_remote(
        alias,
        config,
        ("true",),
        reuse=reuse,
        timeout_seconds=timeout_seconds,
        process_registry=process_registry,
    )


def _parse_profile_marker(stdout: str) -> tuple[float, float, int] | None:
    """Extract (script_ms, nvidia_ms, nvidia_status) from the marker line."""
    for line in stdout.splitlines():
        if not line.startswith(f"{_PROFILE_MARKER}\t"):
            continue
        fields = line.split("\t")
        if len(fields) != 5 or not all(field.isdigit() for field in fields[1:]):
            return None
        t0, t1, t2, nvidia_status = (int(field) for field in fields[1:])
        if not t0 <= t1 <= t2:
            return None
        return (t1 - t0) / 1e6, (t2 - t1) / 1e6, nvidia_status
    return None


def _profile_host(
    alias: str,
    config: MonitorConfig,
    *,
    timeout_seconds: float,
    process_registry: _ActiveProcessRegistry | None = None,
) -> dict[str, object]:
    """Decompose one alias's collection latency into exclusive stages.

    A single remote invocation runs the doctor-owned profiling script on the
    operator's configured connection-reuse path. Remote nanosecond markers
    separate the non-NVIDIA fixed collection pass from the NVIDIA telemetry
    query; the transport share is the remainder, so the three stages sum to
    the measured total.
    """
    total_ms, completed, failure = _run_remote(
        alias,
        config,
        ("sh", "-s"),
        timeout_seconds=timeout_seconds,
        input_text=_PROFILE_SCRIPT,
        process_registry=process_registry,
    )
    profile: dict[str, object] = {"totalMs": total_ms}
    if failure is None and completed is not None:
        failure = _remote_failure_reason(completed)
    if failure is not None:
        profile["failure"] = failure
        return profile
    stages = _parse_profile_marker(completed.stdout)
    if stages is None:
        profile["failure"] = "remote profiling output was not recognized"
        return profile
    script_ms, nvidia_ms, nvidia_status = stages
    assert total_ms is not None
    profile["transportMs"] = round(max(0.0, total_ms - script_ms - nvidia_ms), 1)
    profile["scriptMs"] = round(script_ms, 1)
    profile["nvidiaQueryMs"] = round(nvidia_ms, 1)
    if nvidia_status == 127:
        profile["nvidiaFailure"] = "nvidia-smi is unavailable"
    elif nvidia_status != 0:
        profile["nvidiaFailure"] = f"nvidia-smi query failed (exit {nvidia_status})"
    return profile


def _collection_report(
    alias: str, config: MonitorConfig, probe: ResourceProbe
) -> dict[str, object]:
    """Run one production collection and summarize its already-redacted result.

    The probe is the exact production path: the same fixed remote script,
    transport discipline, and per-host timeouts the monitor service uses.
    ProbeResult.message is redacted by the probe itself, so it is safe to
    report verbatim.
    """
    result = probe.probe(alias, config)
    processes = tuple(process for gpu in result.gpus for process in gpu.processes)
    coverage: float | None = None
    if processes and config.workloads.mode != "disabled":
        matched = sum(1 for process in processes if process.workload is not None)
        coverage = round(100.0 * matched / len(processes), 1)
    return {
        "status": result.status,
        "latencyMs": result.latency_ms,
        "gpuCount": len(result.gpus),
        "processCount": len(processes),
        "workloadCoveragePct": coverage,
        "message": result.message,
    }


def _parse_openssh_duration_seconds(value: str) -> float | None:
    """Parse an OpenSSH TIME FORMAT value ("600", "30s", "1h30m") to seconds.

    Returns None when the value is not a finite duration.
    """
    normalized = value.strip().lower()
    if not normalized or not normalized[0].isdigit():
        return None
    multipliers = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = 0.0
    position = 0
    while position < len(normalized):
        match = re.match(r"(\d+)([smhdw]?)", normalized[position:])
        if match is None or match.end() == 0:
            return None
        total += int(match.group(1)) * multipliers[match.group(2)]
        position += match.end()
    return total


def _reuse_findings(
    options: dict[str, str], poll_interval_seconds: float
) -> tuple[dict[str, object], list[str]]:
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
                if not stat.S_ISDIR(directory_stat.st_mode):
                    warnings.append(
                        "control socket parent is not a directory; "
                        "OpenSSH cannot create the multiplex socket"
                    )
                else:
                    mode = stat.S_IMODE(directory_stat.st_mode)
                    socket_directory_mode = format(mode, "04o")
                    if directory_stat.st_uid != os.geteuid():
                        warnings.append(
                            "control socket directory is not owned by the "
                            "current user; use a private 0700 directory"
                        )
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
        elif persist.lower() != "yes":
            persist_seconds = _parse_openssh_duration_seconds(persist)
            if (
                persist_seconds is not None
                and persist_seconds
                < poll_interval_seconds + _CONTROL_PERSIST_MARGIN_SECONDS
            ):
                warnings.append(
                    f"ControlPersist ({persist}) is shorter than the "
                    f"{poll_interval_seconds:g}s collection interval; the "
                    "master closes between probe cycles and reuse saves nothing"
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
    alias: str,
    config: MonitorConfig,
    *,
    probe_connection: bool,
    profile: bool = False,
    collection_probe: ResourceProbe | None = None,
    budget_seconds: float | None = None,
    process_registry: _ActiveProcessRegistry | None = None,
) -> dict[str, object]:
    if budget_seconds is None:
        budget_seconds = _host_budget_seconds(
            alias, config, collect=collection_probe is not None
        )
    deadline = time.monotonic() + budget_seconds
    report: dict[str, object] = {"alias": alias, "warnings": []}
    warnings: list[str] = report["warnings"]

    if not is_safe_alias(alias):
        report["reachable"] = False
        warnings.append("alias contains unsafe characters")
        return report

    resolve_timeout = _stage_timeout(deadline, _SSH_G_TIMEOUT_SECONDS)
    if resolve_timeout is None:
        report["reachable"] = False
        warnings.append(_BUDGET_EXHAUSTED)
        return report
    options = resolve_ssh_options(
        alias,
        config,
        _QUERY_KEYS,
        timeout_seconds=resolve_timeout,
        process_registry=process_registry,
    )
    if options is None:
        report["reachable"] = False
        warnings.append("ssh -G could not resolve the alias")
        return report

    reuse, reuse_warnings = _reuse_findings(
        options, _effective_poll_interval_seconds(alias, config)
    )
    report["reuse"] = reuse
    warnings.extend(reuse_warnings)
    report["proxyJump"] = bool(options.get("proxyjump") or options.get("proxycommand"))
    control_path = options.get("controlpath", "")
    if control_path and control_path.lower() != "none":
        # Internal only: run_doctor compares expanded paths across aliases
        # and strips this key before any report is written.
        report["_controlPath"] = control_path

    if probe_connection:
        probe_timeout = _effective_probe_timeout_seconds(alias, config)
        report["probeTimeoutSeconds"] = probe_timeout

        def timed_probe(*, reuse_connection: bool) -> tuple[float | None, str | None]:
            stage = _stage_timeout(deadline, probe_timeout)
            if stage is None:
                return None, _BUDGET_EXHAUSTED
            return _timed_probe(
                alias,
                config,
                reuse=reuse_connection,
                timeout_seconds=stage,
                process_registry=process_registry,
            )

        cold_ms, cold_failure = timed_probe(reuse_connection=False)
        if reuse["reuseEnabled"]:
            # Untimed warm-up: with no existing master, the first reuse-path
            # request pays master establishment; the timed request below must
            # measure actual multiplexed reuse instead.
            timed_probe(reuse_connection=True)
        reuse_ms, reuse_failure = timed_probe(reuse_connection=True)
        report["coldLatencyMs"] = cold_ms
        report["reuseLatencyMs"] = reuse_ms
        failure = cold_failure if cold_ms is None else reuse_failure
        report["reachable"] = cold_ms is not None or reuse_ms is not None
        if not report["reachable"] and failure:
            warnings.append(f"unreachable: {failure}")
        if profile and report["reachable"]:
            profile_timeout = _stage_timeout(deadline, probe_timeout)
            if profile_timeout is None:
                report["profile"] = {"failure": _BUDGET_EXHAUSTED}
            else:
                report["profile"] = _profile_host(
                    alias,
                    config,
                    timeout_seconds=profile_timeout,
                    process_registry=process_registry,
                )
        if collection_probe is not None and report["reachable"]:
            # The production probe bounds itself by the host's effective
            # probe timeout, which is exactly the extra stage the budget
            # reserves; only start it while budget remains.
            if _stage_timeout(deadline, probe_timeout) is None:
                report["probe"] = {"failure": _BUDGET_EXHAUSTED}
            else:
                report["probe"] = _collection_report(alias, config, collection_probe)
    return report


def _system_uptime_seconds() -> float:
    return float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])


_FIND_SPEC_CODE = (
    "import importlib.util\n"
    "spec = importlib.util.find_spec('mocop')\n"
    "locations = list(spec.submodule_search_locations or ()) if spec else []\n"
    "print(locations[0] if locations else '')\n"
)


def _installed_package_dir(python_path: Path) -> Path | None:
    """Ask the unit's interpreter where the mocop package is installed."""
    try:
        completed = probe._run_bounded_process(
            [str(python_path), "-c", _FIND_SPEC_CODE],
            input_text="",
            timeout_seconds=_SSH_G_TIMEOUT_SECONDS,
            max_output_bytes=_PROBE_MAX_OUTPUT_BYTES,
            environment=ssh_environment(),
        )
    except (OSError, subprocess.TimeoutExpired, _ProcessOutputLimitExceeded):
        return None
    if completed.returncode != 0:
        return None
    location = completed.stdout.strip()
    if not location or "\n" in location:
        return None
    package_dir = Path(location)
    return package_dir if package_dir.is_dir() else None


def _newest_package_mtime(python_path: Path) -> float | None:
    """Return the newest file mtime inside the installed mocop package.

    The unit's own interpreter locates the real installation (pip --user,
    dist-packages, and editable installs included); the fixed virtualenv
    layout scan remains as a fallback.
    """
    package_dir = _installed_package_dir(python_path)
    if package_dir is not None:
        package_dirs: tuple[Path, ...] = (package_dir,)
    else:
        tool_root = python_path.parent.parent
        package_dirs = tuple(tool_root.glob("lib/python*/site-packages/mocop"))
    newest: float | None = None
    for directory in package_dirs:
        for entry in directory.rglob("*"):
            if not entry.is_file():
                continue
            mtime = entry.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
    return newest


_UNIX_TIMESTAMP = re.compile(r"@(\d+)(?:\.(\d+))?")


def _service_start_epoch() -> tuple[float, str] | None:
    """Resolve the service start time as (epoch_seconds, source).

    systemd's own realtime timestamp is immune to NTP steps that happened
    after boot; the wall-clock-minus-uptime estimate remains as a fallback
    for systemd releases without `--timestamp=unix` (before 247).
    """
    show_command = [
        "systemctl",
        "--user",
        "show",
        "mocop.service",
        "--property=ActiveState,ExecMainStartTimestamp,ExecMainStartTimestampMonotonic",
    ]
    completed = probe._run_bounded_process(
        [*show_command, "--timestamp=unix"],
        input_text="",
        timeout_seconds=_SSH_G_TIMEOUT_SECONDS,
        max_output_bytes=_PROBE_MAX_OUTPUT_BYTES,
        environment=ssh_environment(),
    )
    if completed.returncode != 0:
        completed = probe._run_bounded_process(
            show_command,
            input_text="",
            timeout_seconds=_SSH_G_TIMEOUT_SECONDS,
            max_output_bytes=_PROBE_MAX_OUTPUT_BYTES,
            environment=ssh_environment(),
        )
    if completed.returncode != 0:
        return None
    properties: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    if properties.get("ActiveState") != "active":
        return None
    realtime = _UNIX_TIMESTAMP.fullmatch(properties.get("ExecMainStartTimestamp", ""))
    if realtime is not None:
        seconds = float(realtime.group(1))
        if realtime.group(2):
            seconds += float(f"0.{realtime.group(2)}")
        return seconds, "systemd"
    start_monotonic_usec = int(properties.get("ExecMainStartTimestampMonotonic", ""))
    started_epoch = (
        time.time() - _system_uptime_seconds() + start_monotonic_usec / 1_000_000
    )
    return started_epoch, "uptime-estimate"


def _service_staleness() -> dict[str, object] | None:
    """Best-effort check that the running user service executes current code.

    Returns None whenever the unit, systemd state, or installation layout
    cannot be resolved; a failed check must never break the diagnosis.
    """
    try:
        unit_text = user_unit_path().read_text(encoding="utf-8")
        match = re.search(r'^ExecStart="([^"]+)"', unit_text, re.MULTILINE)
        if match is None:
            return None
        python_path = Path(match.group(1))
        start = _service_start_epoch()
        if start is None:
            return None
        started_epoch, start_source = start
        newest_mtime = _newest_package_mtime(python_path)
        if newest_mtime is None:
            return None
    except (
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
        _ProcessOutputLimitExceeded,
    ):
        return None
    return {
        "staleCode": newest_mtime > started_epoch + 2.0,
        "serviceStartedEpoch": round(started_epoch, 1),
        "startedEpochSource": start_source,
        "installedCodeMtime": round(newest_mtime, 1),
    }


def _warn_on_shared_control_paths(reports: list[dict[str, object]]) -> None:
    """Warn every alias whose expanded ControlPath another alias also uses.

    `ssh -G` has already expanded %h/%p/%r tokens into concrete values, so
    two aliases resolving to the same string would share one multiplex
    socket and could attach their sessions to the wrong host. The raw path
    is stripped here and never written to a report.
    """
    by_path: dict[str, list[dict[str, object]]] = {}
    for report in reports:
        control_path = report.pop("_controlPath", None)
        if isinstance(control_path, str) and control_path:
            by_path.setdefault(control_path, []).append(report)
    for sharers in by_path.values():
        if len(sharers) < 2:
            continue
        aliases = [str(report["alias"]) for report in sharers]
        for report in sharers:
            others = ", ".join(alias for alias in aliases if alias != report["alias"])
            warnings = report["warnings"]
            assert isinstance(warnings, list)
            warnings.append(
                f"{len(aliases)} aliases share one expanded ControlPath; "
                f"multiplexed sessions may reach the wrong host: {others}"
            )


def run_doctor(
    config: MonitorConfig,
    *,
    host_filter: tuple[str, ...] = (),
    probe_connection: bool = True,
    profile: bool = False,
    collect: bool = False,
    as_json: bool = False,
    stdout: TextIO = sys.stdout,
) -> int:
    """Diagnose configured aliases; return 0 when every alias is usable."""
    if profile and not probe_connection:
        return _refuse(
            "--profile requires live connection tests",
            "INVALID_ARGUMENTS",
            as_json,
            stdout,
        )
    if collect and not probe_connection:
        return _refuse(
            "--probe requires live connection tests",
            "INVALID_ARGUMENTS",
            as_json,
            stdout,
        )
    try:
        monitored_hosts = OpenSshConfigHostSource().hosts(config)
    except (OSError, ValueError) as exc:
        return _refuse(
            f"host discovery failed: {exc}",
            "HOST_DISCOVERY_FAILED",
            as_json,
            stdout,
        )
    if host_filter:
        unknown = tuple(host for host in host_filter if host not in monitored_hosts)
        if unknown:
            return _refuse(
                f"unknown monitored aliases: {', '.join(sorted(unknown))}",
                "UNKNOWN_HOST",
                as_json,
                stdout,
            )
        selected_hosts = tuple(host for host in monitored_hosts if host in host_filter)
    else:
        selected_hosts = monitored_hosts
    selected_local_host = (
        config.local_host if config.local_host in selected_hosts else None
    )
    remote_hosts = tuple(host for host in selected_hosts if host != selected_local_host)

    keepalive_interval = _keepalive_interval_seconds(config)
    transport = {
        "connectTimeoutSeconds": config.connect_timeout_seconds,
        "probeTimeoutSeconds": config.probe_timeout_seconds,
        "serverAliveIntervalSeconds": keepalive_interval,
        "serverAliveCountMax": _SERVER_ALIVE_COUNT_MAX,
    }
    reports: list[dict[str, object]] = []
    # One shared probe instance is the production arrangement: the monitor
    # service drives every host through a single probe as well.
    collection_probe = OpenSshLinuxResourceProbe() if collect and remote_hosts else None
    process_registry = _ActiveProcessRegistry()
    executor: ThreadPoolExecutor | None = None
    futures = []
    try:
        if remote_hosts:
            workers = max(1, min(config.max_workers, len(remote_hosts)))
            executor = ThreadPoolExecutor(max_workers=workers)
            futures = [
                executor.submit(
                    _diagnose_host,
                    alias,
                    config,
                    probe_connection=probe_connection,
                    profile=profile,
                    collection_probe=collection_probe,
                    process_registry=process_registry,
                )
                for alias in remote_hosts
            ]
            indexed = {future: index for index, future in enumerate(futures)}
            ordered_reports: list[dict[str, object] | None] = [None] * len(futures)
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    ordered_reports[indexed[future]] = future.result()
            reports = [report for report in ordered_reports if report is not None]
        if selected_local_host is not None:
            local_report = {
                "alias": selected_local_host,
                "local": True,
                "reachable": True,
                "transport": "local",
            }
            reports.insert(selected_hosts.index(selected_local_host), local_report)
    except BaseException:
        process_registry.cancel()
        if collection_probe is not None:
            collection_probe.cancel()
        for future in futures:
            future.cancel()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
            executor = None
        raise
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        if collection_probe is not None:
            collection_probe.close()
        process_registry.close()
    _warn_on_shared_control_paths(reports)
    failed = tuple(report["alias"] for report in reports if _report_failed(report))
    service = _service_staleness()

    if as_json:
        document: dict[str, object] = {
            "ok": not failed,
            "transportDiscipline": transport,
            "localHost": selected_local_host,
            "hosts": reports,
            "status": "ok" if not failed else "failed",
        }
        if service is not None:
            document["service"] = service
        json.dump(document, stdout, ensure_ascii=False, indent=2)
        stdout.write("\n")
    else:
        stdout.write(
            "probe transport: ConnectTimeout="
            f"{transport['connectTimeoutSeconds']}s "
            f"ServerAlive={keepalive_interval}sx{_SERVER_ALIVE_COUNT_MAX} "
            f"probe timeout {transport['probeTimeoutSeconds']}s\n"
        )
        if selected_local_host:
            stdout.write(f"{selected_local_host}: local target, SSH not used\n")
        for report in reports:
            if report.get("local"):
                continue
            _write_text_report(report, stdout)
        if not remote_hosts and selected_local_host is None:
            stdout.write("no remote SSH aliases are configured\n")
        if service is not None:
            source_note = (
                " (start time estimated from uptime)"
                if service.get("startedEpochSource") != "systemd"
                else ""
            )
            if service["staleCode"]:
                stdout.write(
                    "service: installed code is newer than the running service"
                    " — restart to apply (systemctl --user restart mocop)"
                    f"{source_note}\n"
                )
            else:
                stdout.write(
                    f"service: running code matches the installation{source_note}\n"
                )

    return 1 if failed else 0


def _report_failed(report: dict[str, object]) -> bool:
    """A host fails on unreachability or a fatal profile/collection failure."""
    if report.get("reachable") is False:
        return True
    profile_data = report.get("profile")
    if isinstance(profile_data, dict) and "failure" in profile_data:
        return True
    probe_data = report.get("probe")
    if isinstance(probe_data, dict):
        if "failure" in probe_data:
            return True
        # A missing NVIDIA tool is represented as an online host with zero
        # GPUs; only transport and collection errors fail the diagnosis.
        if probe_data.get("status") in ("unreachable", "error"):
            return True
    return False


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
    profile = report.get("profile")
    if isinstance(profile, dict):
        failure = profile.get("failure")
        if failure:
            stdout.write(f"  profile failure: {failure}\n")
        else:
            stdout.write(f"  profile: {_format_profile(profile)}\n")
            nvidia_failure = profile.get("nvidiaFailure")
            if nvidia_failure:
                stdout.write(f"  profile warning: {nvidia_failure}\n")
    collection = report.get("probe")
    if isinstance(collection, dict):
        failure = collection.get("failure")
        if failure:
            stdout.write(f"  probe failure: {failure}\n")
        else:
            coverage = collection.get("workloadCoveragePct")
            coverage_text = f"{coverage:.0f}%" if isinstance(coverage, float) else "n/a"
            stdout.write(
                f"  probe: {collection['status']} ({collection['latencyMs']} ms, "
                f"{collection['gpuCount']} GPUs, {collection['processCount']} "
                f"processes, workload coverage {coverage_text})\n"
            )
            if collection.get("message"):
                stdout.write(f"  probe message: {collection['message']}\n")
    for warning in report.get("warnings", ()):
        stdout.write(f"  warning: {warning}\n")


def _format_profile(profile: dict[str, object]) -> str:
    def ms(key: str) -> str:
        value = profile.get(key)
        return f"{value:.0f} ms" if isinstance(value, float) else "failed"

    return (
        f"total {ms('totalMs')} = transport {ms('transportMs')} "
        f"+ script {ms('scriptMs')} + nvidia {ms('nvidiaQueryMs')}"
    )
