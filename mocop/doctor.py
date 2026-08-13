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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TextIO

from .config import MonitorConfig, is_safe_alias
from .lifecycle import user_unit_path
from .probe import (
    _BoundedProcessResult,
    _ProcessOutputLimitExceeded,
    _run_bounded_process,
    _safe_ssh_failure,
)
from .remote_script import _COMBINED_QUERY_FIELDS

_SSH_G_TIMEOUT_SECONDS = 10
_SSH_G_MAX_OUTPUT_BYTES = 262_144
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
        "serveraliveinterval",
        "serveralivecountmax",
        "stricthostkeychecking",
    }
)

# Doctor-owned, self-contained profiling script. It reproduces the fixed
# collection script's non-NVIDIA passes (same files and external commands,
# output discarded) and the combined NVIDIA telemetry query, bracketing each
# stage with remote nanosecond timestamps. The stages are mutually exclusive,
# so the transport share is the locally measured total minus the remote time.
_PROFILE_SCRIPT = r"""
LC_ALL=C
export LC_ALL
t0=$(date +%s%N)
awk '{ next }' /proc/stat /proc/meminfo /proc/loadavg /proc/uptime /proc/net/dev /sys/block/*/stat >/dev/null 2>&1
df -PTk 2>/dev/null | awk 'NR > 1 { next }' >/dev/null 2>&1
t1=$(date +%s%N)
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=__COMBINED_QUERY__ --format=csv,noheader,nounits >/dev/null 2>&1
  nvidia_status=$?
else
  nvidia_status=127
fi
t2=$(date +%s%N)
printf '__MARKER__\t%s\t%s\t%s\t%s\n' "$t0" "$t1" "$t2" "$nvidia_status"
""".replace("__COMBINED_QUERY__", ",".join(_COMBINED_QUERY_FIELDS)).replace(
    "__MARKER__", _PROFILE_MARKER
)


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    return environment


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


def _host_budget_seconds(alias: str, config: MonitorConfig) -> float:
    """Bound one host's total diagnosis time.

    The budget covers alias resolution plus three connection-bound stages
    (cold, reuse, profile) at the host's effective probe timeout.
    """
    return _SSH_G_TIMEOUT_SECONDS + 3 * _effective_probe_timeout_seconds(alias, config)


def _stage_timeout(deadline: float, stage_timeout_seconds: float) -> float | None:
    """Cap one stage's timeout by the host's remaining diagnosis budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(stage_timeout_seconds, remaining)


def _resolved_options(
    alias: str,
    config: MonitorConfig,
    *,
    timeout_seconds: float = _SSH_G_TIMEOUT_SECONDS,
) -> dict[str, str] | None:
    """Return the lowercase OpenSSH options `ssh -G` resolves for the alias."""
    try:
        completed = _run_bounded_process(
            ["ssh", "-G", "-F", str(config.ssh_config), "--", alias],
            input_text="",
            timeout_seconds=timeout_seconds,
            max_output_bytes=_SSH_G_MAX_OUTPUT_BYTES,
            environment=_environment(),
        )
    except (OSError, subprocess.TimeoutExpired, _ProcessOutputLimitExceeded):
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


def _run_remote(
    alias: str,
    config: MonitorConfig,
    remote_args: tuple[str, ...],
    *,
    timeout_seconds: float,
    input_text: str = "",
    reuse: bool = True,
    max_output_bytes: int = _PROBE_MAX_OUTPUT_BYTES,
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
        completed = _run_bounded_process(
            command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            environment=_environment(),
        )
    except subprocess.TimeoutExpired:
        return None, None, "SSH connection attempt timed out"
    except _ProcessOutputLimitExceeded:
        return None, None, "remote output exceeded the configured limit"
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
    )
    if failure is not None or completed is None:
        return None, failure
    reason = _remote_failure_reason(completed)
    if reason is not None:
        return None, reason
    return latency_ms, None


def _timed_probe(
    alias: str, config: MonitorConfig, *, reuse: bool, timeout_seconds: float
) -> tuple[float | None, str | None]:
    """Run one bounded `true` over SSH; return (latency_ms, failure_reason)."""
    return _timed_remote(
        alias, config, ("true",), reuse=reuse, timeout_seconds=timeout_seconds
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
    alias: str, config: MonitorConfig, *, timeout_seconds: float
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
    budget_seconds: float | None = None,
) -> dict[str, object]:
    if budget_seconds is None:
        budget_seconds = _host_budget_seconds(alias, config)
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
    options = _resolved_options(alias, config, timeout_seconds=resolve_timeout)
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

    if probe_connection:
        probe_timeout = _effective_probe_timeout_seconds(alias, config)
        report["probeTimeoutSeconds"] = probe_timeout

        def timed_probe(*, reuse_connection: bool) -> tuple[float | None, str | None]:
            stage = _stage_timeout(deadline, probe_timeout)
            if stage is None:
                return None, _BUDGET_EXHAUSTED
            return _timed_probe(
                alias, config, reuse=reuse_connection, timeout_seconds=stage
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
                    alias, config, timeout_seconds=profile_timeout
                )
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
        completed = _run_bounded_process(
            [str(python_path), "-c", _FIND_SPEC_CODE],
            input_text="",
            timeout_seconds=_SSH_G_TIMEOUT_SECONDS,
            max_output_bytes=_PROBE_MAX_OUTPUT_BYTES,
            environment=_environment(),
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
    completed = _run_bounded_process(
        [*show_command, "--timestamp=unix"],
        input_text="",
        timeout_seconds=_SSH_G_TIMEOUT_SECONDS,
        max_output_bytes=_PROBE_MAX_OUTPUT_BYTES,
        environment=_environment(),
    )
    if completed.returncode != 0:
        completed = _run_bounded_process(
            show_command,
            input_text="",
            timeout_seconds=_SSH_G_TIMEOUT_SECONDS,
            max_output_bytes=_PROBE_MAX_OUTPUT_BYTES,
            environment=_environment(),
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


def run_doctor(
    config: MonitorConfig,
    *,
    host_filter: tuple[str, ...] = (),
    probe_connection: bool = True,
    profile: bool = False,
    as_json: bool = False,
    stdout: TextIO = sys.stdout,
) -> int:
    """Diagnose configured aliases; return 0 when every alias is usable."""
    if profile and not probe_connection:
        print("--profile requires live connection tests", file=sys.stderr)
        return 2
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

    keepalive_interval = _keepalive_interval_seconds(config)
    transport = {
        "connectTimeoutSeconds": config.connect_timeout_seconds,
        "probeTimeoutSeconds": config.probe_timeout_seconds,
        "serverAliveIntervalSeconds": keepalive_interval,
        "serverAliveCountMax": _SERVER_ALIVE_COUNT_MAX,
    }
    reports: list[dict[str, object]] = []
    if remote_hosts:
        workers = max(1, min(config.max_workers, len(remote_hosts)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            reports = list(
                executor.map(
                    lambda alias: _diagnose_host(
                        alias,
                        config,
                        probe_connection=probe_connection,
                        profile=profile,
                    ),
                    remote_hosts,
                )
            )
    failed = tuple(report["alias"] for report in reports if _report_failed(report))
    service = _service_staleness()

    if as_json:
        document: dict[str, object] = {
            "transportDiscipline": transport,
            "localHost": config.local_host,
            "hosts": reports,
            "status": "ok" if not failed else "failed",
        }
        if service is not None:
            document["service"] = service
        json.dump(document, stdout, indent=2)
        stdout.write("\n")
    else:
        stdout.write(
            "probe transport: ConnectTimeout="
            f"{transport['connectTimeoutSeconds']}s "
            f"ServerAlive={keepalive_interval}sx{_SERVER_ALIVE_COUNT_MAX} "
            f"probe timeout {transport['probeTimeoutSeconds']}s\n"
        )
        if config.local_host:
            stdout.write(f"{config.local_host}: local target, SSH not used\n")
        for report in reports:
            _write_text_report(report, stdout)
        if not remote_hosts:
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
    """A host fails on unreachability or a fatal profile stage failure."""
    if report.get("reachable") is False:
        return True
    profile_data = report.get("profile")
    return isinstance(profile_data, dict) and "failure" in profile_data


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
