"""Fixed remote collection script: protocol constants, template, rendering.

The script is rendered once per (workload, process) combination at import
time; probes never build shell text from runtime values.
"""

from __future__ import annotations

_QUERY_FIELDS = (
    "index",
    "uuid",
    "name",
    "driver_version",
    "pstate",
    "temperature.gpu",
    "utilization.gpu",
    "utilization.memory",
    "memory.total",
    "memory.used",
    "memory.free",
    "power.draw",
    "power.limit",
)
_PROCESS_QUERY_FIELDS = ("gpu_uuid", "pid", "process_name", "used_gpu_memory")
_HEALTH_QUERY_FIELDS = (
    "uuid",
    "ecc.errors.uncorrected.volatile.total",
    "retired_pages.pending",
    "remapped_rows.pending",
    "clocks_event_reasons.hw_thermal_slowdown",
    "clocks_event_reasons.hw_power_brake_slowdown",
    "mig.mode.current",
)
_COMBINED_QUERY_FIELDS = _QUERY_FIELDS + _HEALTH_QUERY_FIELDS[1:]
_PROTOCOL_VERSION = "MONITOR_V7"
_SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {_PROTOCOL_VERSION, "MONITOR_V6", "MONITOR_V5", "MONITOR_V4"}
)
# PROCESS_SKIPPED markers exist since V6; workload records carry the optional
# start-epoch and command-line columns since V7.
_PROCESS_SKIP_CAPABLE_VERSIONS = frozenset({_PROTOCOL_VERSION, "MONITOR_V6"})

# The hostname is read inside the system awk pass with a bounded getline, so
# the default sample runs five external commands instead of six. A missing or
# unreadable hostname file degrades to "unknown" without aborting the pass.
_REMOTE_SCRIPT_TEMPLATE = r"""
LC_ALL=C
export LC_ALL
printf '__PROTOCOL_VERSION__\n'
workload_tier=__WORKLOAD_TIER__
process_enabled=__PROCESS_ENABLED__
awk '
  BEGIN {
    host = ""
    if ((getline host_line < "/proc/sys/kernel/hostname") > 0) {
      gsub(/[[:cntrl:]]/, " ", host_line)
      host = substr(host_line, 1, 255)
    }
    close("/proc/sys/kernel/hostname")
    if (host == "") host = "unknown"
    printf "HOST\t%s\n", host
  }
  FILENAME == "/proc/stat" {
    if ($1 == "cpu") {
      total=0
      for (i=2; i<=NF; i++) total += $i
      idle=$5+$6
    } else if ($1 ~ /^cpu[0-9]+$/) {
      cores++
    }
    next
  }
  FILENAME == "/proc/meminfo" {
    if ($1 == "MemTotal:") mt=$2
    else if ($1 == "MemAvailable:") ma=$2
    else if ($1 == "SwapTotal:") st=$2
    else if ($1 == "SwapFree:") sf=$2
    next
  }
  FILENAME == "/proc/loadavg" { load1=$1; load5=$2; load15=$3; next }
  FILENAME == "/proc/uptime" { uptime=$1; next }
  FILENAME == "/proc/net/dev" && FNR > 2 {
    gsub(/:/, "", $1)
    if ($1 != "lo") { rx += $2; tx += $10 }
    next
  }
  FILENAME ~ /\/sys\/block\/.*\/stat$/ && FILENAME !~ /\/(loop[0-9]+|ram[0-9]+|zram[0-9]+|dm-[0-9]+|md[0-9]+)\/stat$/ {
    read_bytes += $3 * 512
    write_bytes += $7 * 512
  }
  END {
    printf "CPU\t%.0f\t%.0f\n", total, idle
    printf "CORES\t%.0f\n", cores
    printf "MEM\t%.0f\t%.0f\t%.0f\t%.0f\n", mt, ma, st, sf
    printf "LOAD\t%s\t%s\t%s\n", load1, load5, load15
    printf "UPTIME\t%s\n", uptime
    printf "NET\t%.0f\t%.0f\n", rx, tx
    printf "IO\t%.0f\t%.0f\n", read_bytes, write_bytes
  }
' /proc/stat /proc/meminfo /proc/loadavg /proc/uptime /proc/net/dev /sys/block/*/stat 2>/dev/null
printf 'DISKS_BEGIN\n'
df -PTk 2>/dev/null | awk '
  NR > 1 && $2 !~ /^(tmpfs|devtmpfs|squashfs|overlay|proc|sysfs|cgroup2?|efivarfs|tracefs|debugfs|mqueue|fusectl|securityfs|pstore|configfs|autofs|binfmt_misc|ramfs|nsfs)$/ {
    pct=$6; gsub(/%/, "", pct)
    printf "DISK\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", $1, $2, $3, $4, $5, pct, $7
  }
'
printf 'DISKS_END\n'
printf 'GPUS_BEGIN\n'
gpu_health_inline=0
if command -v nvidia-smi >/dev/null 2>&1; then
  combined_output=$(nvidia-smi --query-gpu=__COMBINED_QUERY__ --format=csv,noheader,nounits 2>/dev/null)
  combined_status=$?
  if [ "$combined_status" -eq 0 ]; then
    gpu_health_inline=1
    [ -z "$combined_output" ] || printf '%s\n' "$combined_output"
  else
    nvidia-smi --query-gpu=__GPU_QUERY__ --format=csv,noheader,nounits 2>/dev/null || printf 'GPU_ERROR\t%s\n' "$?"
  fi
else
  printf 'GPU_UNAVAILABLE\n'
fi
printf 'GPUS_END\n'
printf 'PROCESSES_BEGIN\n'
process_output=''
process_status=0
if [ "$process_enabled" -eq 0 ]; then
  printf 'PROCESS_SKIPPED\n'
elif command -v nvidia-smi >/dev/null 2>&1; then
  process_output=$(nvidia-smi --query-compute-apps=__PROCESS_QUERY__ --format=csv,noheader,nounits 2>/dev/null)
  process_status=$?
  if [ "$process_status" -eq 0 ]; then
    [ -z "$process_output" ] || printf '%s\n' "$process_output"
  else
    printf 'PROCESS_ERROR\t%s\n' "$process_status"
  fi
fi
printf 'PROCESSES_END\n'
printf 'WORKLOADS_BEGIN\n'
if [ "$workload_tier" -ge 1 ] && [ "$process_status" -eq 0 ] && [ -n "$process_output" ]; then
  boot_epoch=$(awk '/^btime / { print $2; exit }' /proc/stat 2>/dev/null)
  clock_ticks=$(getconf CLK_TCK 2>/dev/null)
  case "$clock_ticks" in ''|*[!0-9]*) clock_ticks=100 ;; esac
  process_pids=$(printf '%s\n' "$process_output" | awk -F, '
    {
      pid=$2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", pid)
      if (pid ~ /^[0-9]+$/ && !seen[pid]++) print pid
    }
  ')
  for process_pid in $process_pids; do
    [ -r "/proc/$process_pid/status" ] || continue
    owner_uid=$(awk '/^Uid:/ { print $2; exit }' "/proc/$process_pid/status" 2>/dev/null)
    started_ticks=$(awk '{ sub(/^.*\) /, ""); print $20 }' "/proc/$process_pid/stat" 2>/dev/null)
    command_line=$(head -c 255 "/proc/$process_pid/cmdline" 2>/dev/null | tr '\000' ' ')
    if [ "$workload_tier" -ge 2 ]; then
      cgroup_value=$(head -c 16384 "/proc/$process_pid/cgroup" 2>/dev/null)
      environ_source="/proc/$process_pid/environ"
    else
      cgroup_value=""
      environ_source="/dev/null"
    fi
    if [ -r "$environ_source" ]; then
      head -c 65536 "$environ_source" 2>/dev/null
    fi | tr '\000' '\n' | awk -v pid="$process_pid" -v uid="$owner_uid" -v cgroup="$cgroup_value" -v boot="$boot_epoch" -v ticks="$started_ticks" -v clock="$clock_ticks" -v command_line="$command_line" '
      function clean(value) {
        gsub(/[[:cntrl:]]/, " ", value)
        return substr(value, 1, 255)
      }
      /^SLURM_JOB_ID=/ { slurm_id=substr($0, index($0, "=") + 1) }
      /^SLURM_JOB_NAME=/ { slurm_name=substr($0, index($0, "=") + 1) }
      /^SLURM_JOB_PARTITION=/ { slurm_queue=substr($0, index($0, "=") + 1) }
      /^POD_UID=/ { pod_id=substr($0, index($0, "=") + 1) }
      /^POD_NAME=/ { pod_name=substr($0, index($0, "=") + 1) }
      /^POD_NAMESPACE=/ { pod_namespace=substr($0, index($0, "=") + 1) }
      /^KUEUE_LOCAL_QUEUE_NAME=/ { pod_queue=substr($0, index($0, "=") + 1) }
      END {
        # A scheduler identifier is only trusted from an anchored cgroup
        # segment: systemd scopes like "job_1.scope" or "podcast.service" must
        # not be mistaken for Slurm jobs or Kubernetes pods.
        slurm_cgroup = (cgroup ~ /(\/|^)slurm/)
        kube_cgroup = (cgroup ~ /(\/|^)kubepods/)
        if (slurm_id == "" && slurm_cgroup && match(cgroup, /job_[0-9]+/)) {
          slurm_id=substr(cgroup, RSTART + 4, RLENGTH - 4)
        }
        if (pod_id == "" && kube_cgroup && match(cgroup, /pod[0-9A-Fa-f_-]+/)) {
          pod_id=substr(cgroup, RSTART + 3, RLENGTH - 3)
        }
        # The owner is the real UID resolved through the root-owned passwd
        # database; process environment values are attacker-controlled and
        # never define ownership.
        owner=uid
        if (uid != "") {
          while ((getline pwline < "/etc/passwd") > 0) {
            np=split(pwline, pf, ":")
            if (np >= 3 && pf[3] == uid) { owner=pf[1]; break }
          }
          close("/etc/passwd")
        }
        kind="process"
        workload_id=""
        workload_name=""
        workload_queue=""
        workload_namespace=""
        if (slurm_id != "" || slurm_cgroup) {
          kind="slurm"
          workload_id=slurm_id
          workload_name=slurm_name
          workload_queue=slurm_queue
        } else if (pod_id != "" || kube_cgroup) {
          kind="kubernetes"
          workload_id=pod_id
          workload_name=pod_name
          workload_queue=pod_queue
          workload_namespace=pod_namespace
        }
        started=""
        if (boot ~ /^[0-9]+$/ && ticks ~ /^[0-9]+$/ && clock ~ /^[0-9]+$/ && clock + 0 > 0) {
          started = boot + int(ticks / clock)
        }
        printf "WORKLOAD\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
          pid, kind, clean(workload_id), clean(workload_name), clean(owner),
          clean(workload_queue), clean(workload_namespace), started,
          clean(command_line)
      }
    '
  done
fi
printf 'WORKLOADS_END\n'
printf 'GPU_HEALTH_BEGIN\n'
[ "$gpu_health_inline" -eq 1 ] || printf 'GPU_HEALTH_ERROR\t1\n'
printf 'GPU_HEALTH_END\n'
"""


_WORKLOAD_TIERS = {"disabled": 0, "identity": 1, "auto": 2}


def _render_remote_script(workload_tier: int, process_enabled: bool) -> str:
    return (
        _REMOTE_SCRIPT_TEMPLATE.replace("__GPU_QUERY__", ",".join(_QUERY_FIELDS))
        .replace("__COMBINED_QUERY__", ",".join(_COMBINED_QUERY_FIELDS))
        .replace("__PROCESS_QUERY__", ",".join(_PROCESS_QUERY_FIELDS))
        .replace("__WORKLOAD_TIER__", str(workload_tier))
        .replace("__PROCESS_ENABLED__", "1" if process_enabled else "0")
        .replace("__PROTOCOL_VERSION__", _PROTOCOL_VERSION)
    )


_REMOTE_SCRIPTS = {
    (workload_tier, process_enabled): _render_remote_script(
        workload_tier,
        process_enabled,
    )
    for workload_tier in (0, 1, 2)
    for process_enabled in (False, True)
}


def _remote_script(workload_mode: str, process_enabled: bool = True) -> str:
    return _REMOTE_SCRIPTS[(_WORKLOAD_TIERS.get(workload_mode, 2), process_enabled)]
