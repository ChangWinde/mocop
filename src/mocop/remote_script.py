"""Fixed remote collection script: protocol constants, template, rendering.

The script is rendered once per (workload, process) combination at import
time; probes never build shell text from runtime values.
"""

from __future__ import annotations

from pathlib import Path

PERSONAL_STORAGE_SCRIPT = (
    Path(__file__).with_name("storage_probe.py").read_text(encoding="utf-8")
)

# TERMINAL_ENV_V1: keep native capabilities when the target has their entry.
# This only changes the launched session; remote startup files are not edited.
TERMINAL_ENVIRONMENT_SCRIPT = r"""
if command -v infocmp >/dev/null 2>&1; then
  if [ -z "${TERM:-}" ] || [ "$TERM" = dumb ] || ! infocmp "$TERM" >/dev/null 2>&1; then
    for mocop_terminal_type in xterm-256color xterm vt100; do
      if infocmp "$mocop_terminal_type" >/dev/null 2>&1; then
        TERM=$mocop_terminal_type
        export TERM
        break
      fi
    done
    unset mocop_terminal_type
  fi
fi
"""

# Separate daily RAM protocol; no browser or configuration text is interpolated.
USER_MEMORY_SCRIPT = r"""
LC_ALL=C
export LC_ALL
printf 'USER_MEMORY_V1\n'
awk '/^MemTotal:/ { print "TOTAL", $2; found=1 } END { if (!found) exit 1 }' /proc/meminfo || exit 1
ps -eo uid=,user:64=,rss= || exit 1
printf 'END\n'
"""

# Fixed disk programs: no client or configuration text is interpolated.
_DIRECTORY_DISK_PROGRAM = r"""
import itertools, json, os, re, shutil, subprocess, time

PSEUDO = {'proc', 'sysfs', 'tmpfs', 'devtmpfs', 'devpts', 'cgroup', 'cgroup2',
          'mqueue', 'securityfs', 'pstore', 'debugfs', 'tracefs', 'hugetlbfs',
          'configfs', 'fusectl', 'rpc_pipefs', 'autofs', 'binfmt_misc', 'efivarfs',
          'bpf', 'ramfs', 'nsfs', 'squashfs', 'fuse.portal', 'fuse.gvfsd-fuse'}
INTERNAL = ('/proc', '/sys', '/dev', '/etc', '/usr', '/run', '/snap',
            '/var/lib/docker', '/var/lib/containerd', '/tmp/.ssh', '/tmp/RustDesk',
            '/opt/bc-tools', '/mnt/.credential-0', '/mnt/.alibabacloud', '/mlplatform/.ssh')
FALLBACK_ROOTS = ('/home', '/data', '/mnt/data', '/workspace', '/personal_fast_storage')


def under(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def flatten(items):
    for item in items:
        yield item
        yield from flatten(item.get('children', []))


def discover_mounts():
    result = subprocess.run(['findmnt', '-J', '-o', 'TARGET,SOURCE,FSTYPE,FSROOT,MAJ:MIN'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12, check=True)
    groups = {}
    for mount in sorted(flatten(json.loads(result.stdout)['filesystems']), key=lambda m: (len(m['target']), m['target'])):
        target, kind = mount['target'], mount['fstype']
        internal = any(under(target, prefix) for prefix in INTERNAL) and not under(target, '/run/media')
        if kind in PSEUDO or internal or not os.path.isdir(target):
            continue
        key = (kind, mount['source'], mount.get('fsroot', '/'), mount.get('maj:min', target) if kind == 'overlay' else None)
        if key in groups:
            if target not in groups[key]['aliases']:
                groups[key]['aliases'].append(target)
            continue
        if len(groups) >= 128:
            raise ValueError('too many mounted filesystems')
        groups[key] = {'mountPoint': target, 'aliases': [target], 'fsType': kind,
            'kind': 'network' if kind in ('nfs', 'nfs4', 'cifs', 'smb3', 'ceph', 'glusterfs', 'lustre', 'gpfs', 'beegfs', 'vepfs') or kind.startswith('fuse') else 'local',
            'scanRoot': target != '/' and not under(target, '/boot') and not under(target, '/nix'),
            'totalKiB': None, 'usedKiB': None, 'availableKiB': None, 'percentUsed': None}
    notes = []
    try:
        df = subprocess.run(['df', '-P', '-k', '-a'], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=12, check=False)
        usage = {}
        for line in df.stdout.decode('utf-8', 'replace').splitlines():
            match = re.match(r'^.+?\s+(\d+)\s+(\d+)\s+(-?\d+)\s+(\d+)%\s+(.+)$', line)
            if match:
                size, used, free, percent, target = match.groups()
                usage[target] = dict(totalKiB=int(size), usedKiB=int(used), availableKiB=int(free), percentUsed=int(percent))
        for mount in groups.values():
            values = next((usage[p] for p in mount['aliases'] if p in usage), None)
            if values:
                mount.update(values)
        if df.returncode:
            notes.append('Some df capacity readings failed')
    except (OSError, subprocess.TimeoutExpired):
        notes.append('Filesystem capacity query timed out or failed')
    return list(groups.values()), notes


def candidate_directories(mounts, notes):
    aliases = {p for mount in mounts for p in mount['aliases']}
    roots = [m['mountPoint'] for m in mounts if m['scanRoot']]
    roots += [p for p in FALLBACK_ROOTS if p not in aliases and os.path.isdir(p) and not os.path.islink(p)]
    roots = list(dict.fromkeys(roots))
    excluded = aliases | set(roots)
    groups, seen = [], set()
    for root in roots:
        rows = []
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if entry.path in excluded or not entry.is_dir(follow_symlinks=False):
                        continue
                    real = os.path.realpath(entry.path)
                    if real in seen:
                        continue
                    seen.add(real)
                    rows.append(entry.path)
                    if len(rows) == 256:
                        notes.append('Directory listing limit reached: ' + root)
                        break
        except OSError:
            notes.append('Cannot list ' + root)
        groups.append(sorted(rows))
    if os.path.isdir('/root') and '/root' not in aliases:
        groups.append(['/root'])
    candidates = [p for bundle in itertools.zip_longest(*groups) for p in bundle if p]
    if len(candidates) > 256:
        notes.append('Directory limit reached; inventory is incomplete')
    return candidates[:256]


def main(scan):
    mounts, notes = discover_mounts()
    rows = []
    deadline = time.monotonic() + 1800
    if scan:
        for path in candidate_directories(mounts, notes):
            print(" ", flush=True)  # Detect a closed SSH pipe before starting more work.
            parent = max((m for m in mounts if any(under(path, p) for p in m['aliases'])), key=lambda m: max(len(p) for p in m['aliases'] if under(path, p)), default=None)
            row = {'path': path, 'mountPoint': parent['mountPoint'] if parent else '/', 'sizeKiB': None, 'status': 'skipped'}
            remaining = deadline - time.monotonic()
            if remaining > 0:
                command = ['du', '-P', '-x', '-k', '-s', '--null']
                for mount in mounts:
                    for nested in mount['aliases']:
                        if nested != path and under(nested, path):
                            pattern = ''.join('[' + c + ']' if c in '*?[' else c for c in nested)
                            command.append('--exclude=' + pattern)
                command += ['--', path]
                if shutil.which('nice'):
                    command = ['nice', '-n', '19'] + command
                if shutil.which('ionice'):
                    command = ['ionice', '-c', '3'] + command
                try:
                    result = subprocess.run(command, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, timeout=min(90, remaining), check=False)
                    value = result.stdout.split(b'\t', 1)[0]
                    if result.returncode == 0 and value.isdigit():
                        row.update(sizeKiB=int(value), status='ok')
                    else:
                        row['status'] = 'unreadable'
                except subprocess.TimeoutExpired:
                    row['status'] = 'timeout'
                except OSError:
                    row['status'] = 'unreadable'
            rows.append(row)
    print(json.dumps({'protocol': 'DIRECTORY_DISK_V1', 'filesystems': mounts,
        'directories': rows, 'notes': notes[:32]}))
"""
DIRECTORY_DISK_INVENTORY_SCRIPT = (
    "LC_ALL=C python3 - --mocop-directory-disk <<'MOCOP_DISK_PY'\n"
    + _DIRECTORY_DISK_PROGRAM
    + "\nmain(False)\nMOCOP_DISK_PY\n"
)
DIRECTORY_DISK_SCRIPT = (
    "LC_ALL=C python3 - --mocop-directory-disk <<'MOCOP_DISK_PY'\n"
    + _DIRECTORY_DISK_PROGRAM
    + "\nmain(True)\nMOCOP_DISK_PY\n"
)

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
_PROTOCOL_VERSION = "MONITOR_V8"
# The fixed script and its parser ship inside one process and are re-sent on
# every probe, so no deployed emitter of an older protocol version can exist;
# accepting only the current version keeps the parser honest and small.
_SUPPORTED_PROTOCOL_VERSIONS = frozenset({_PROTOCOL_VERSION})

# POSIX awk does not require interval expressions such as ``{12,64}``; mawk,
# the default awk on common Debian/Ubuntu hosts, therefore treats the previous
# container pattern literally. Keep this function separately executable so
# tests exercise the exact portable detector embedded in the remote script.
_CONTAINER_IDENTITY_AWK = r"""
  function valid_container_id(value) {
    return length(value) >= 12 && length(value) <= 64 && value !~ /[^0-9a-f]/
  }
  function detect_container(value,    lines, line_count, fields, field_count, i, j, segment, candidate) {
    container_kind = ""
    container_id = ""
    line_count = split(value, lines, "\n")
    for (i = 1; i <= line_count; i++) {
      field_count = split(lines[i], fields, "/")
      for (j = 1; j <= field_count; j++) {
        segment = fields[j]
        if (segment == "docker" && j < field_count && valid_container_id(fields[j + 1])) {
          container_kind = "docker"
          container_id = fields[j + 1]
          return
        }
        if (substr(segment, 1, 7) == "docker-" && substr(segment, length(segment) - 5) == ".scope") {
          candidate = substr(segment, 8, length(segment) - 13)
          if (valid_container_id(candidate)) {
            container_kind = "docker"
            container_id = candidate
            return
          }
        }
        if (substr(segment, 1, 7) == "libpod-" && substr(segment, length(segment) - 5) == ".scope") {
          candidate = substr(segment, 8, length(segment) - 13)
          if (valid_container_id(candidate)) {
            container_kind = "podman"
            container_id = candidate
            return
          }
        }
      }
    }
  }
"""

# The hostname is read inside the system awk pass with a bounded getline, so
# the default sample runs five external commands instead of six. A missing or
# unreadable hostname file degrades to "unknown" without aborting the pass.
_REMOTE_SCRIPT_TEMPLATE = r"""
LC_ALL=C
export LC_ALL
printf '__PROTOCOL_VERSION__\n'
workload_tier=__WORKLOAD_TIER__
process_enabled=__PROCESS_ENABLED__
# Expand block-device stat files in the shell: when the glob has no match
# (diskless or sandboxed guests), awk would receive the literal pattern,
# fail to open it, and abort before END emits the core CPU/MEM/NET rows.
set -- /sys/block/*/stat
[ -e "$1" ] || set --
awk '
  # Pressure stall information (kernel 4.20+): report the some/full avg10 and
  # avg60 windows per resource. A missing or unreadable file emits nothing, so
  # kernels without CONFIG_PSI degrade silently instead of failing the pass.
  function emit_psi(resource, file,    line, parts, count, i, sep, key, value, s10, s60, f10, f60) {
    s10 = ""; s60 = ""; f10 = ""; f60 = ""
    while ((getline line < file) > 0) {
      count = split(line, parts, " ")
      for (i = 2; i <= count; i++) {
        sep = index(parts[i], "=")
        if (sep <= 1) continue
        key = substr(parts[i], 1, sep - 1)
        value = substr(parts[i], sep + 1)
        if (parts[1] == "some" && key == "avg10") s10 = value
        else if (parts[1] == "some" && key == "avg60") s60 = value
        else if (parts[1] == "full" && key == "avg10") f10 = value
        else if (parts[1] == "full" && key == "avg60") f60 = value
      }
    }
    close(file)
    if (s10 != "" && s60 != "") {
      printf "PSI\t%s\t%s\t%s\t%s\t%s\n", resource, s10, s60, f10, f60
    }
  }
  BEGIN {
    host = ""
    if ((getline host_line < "/proc/sys/kernel/hostname") > 0) {
      gsub(/[[:cntrl:]]/, " ", host_line)
      host = substr(host_line, 1, 255)
    }
    close("/proc/sys/kernel/hostname")
    if (host == "") host = "unknown"
    printf "HOST\t%s\n", host
    emit_psi("cpu", "/proc/pressure/cpu")
    emit_psi("memory", "/proc/pressure/memory")
    emit_psi("io", "/proc/pressure/io")
  }
  FILENAME == "/proc/stat" {
    if ($1 == "cpu") {
      total=0
      # guest and guest_nice are already included in user and nice. Ignore
      # them and future columns; older kernels may omit the trailing fields.
      for (i=2; i<=9 && i<=NF; i++) total += $i
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
' /proc/stat /proc/meminfo /proc/loadavg /proc/uptime /proc/net/dev "$@" 2>/dev/null
printf 'DISKS_BEGIN\n'
df -PTk 2>/dev/null | awk '
  NR > 1 {
    # Locate the type and numeric columns without splitting the source or
    # mount point: both can contain spaces, including repeated spaces.
    if (!match($0, /[[:space:]]+[^[:space:]]+[[:space:]]+[0-9]+[[:space:]]+[0-9]+[[:space:]]+[0-9]+[[:space:]]+[0-9]+%[[:space:]]+/)) next
    source = substr($0, 1, RSTART - 1)
    mountpoint = substr($0, RSTART + RLENGTH)
    split(substr($0, RSTART, RLENGTH), columns, " ")
    type = columns[1]
    pct = columns[5]; sub(/%$/, "", pct)
    if (source == "" || mountpoint == "") next
    # An overlay mounted at / is a container root with real backing storage, so
    # it must be reported; overlay mounts elsewhere belong to containers running
    # on a Docker host and are not this target'"'"'s capacity.
    if (type !~ /^(tmpfs|devtmpfs|squashfs|overlay|proc|sysfs|cgroup2?|efivarfs|tracefs|debugfs|mqueue|fusectl|securityfs|pstore|configfs|autofs|binfmt_misc|ramfs|nsfs)$/ || (type == "overlay" && mountpoint == "/"))
      printf "DISK\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", source, type, columns[2], columns[3], columns[4], pct, mountpoint
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
    fallback_output=$(nvidia-smi --query-gpu=__GPU_QUERY__ --format=csv,noheader,nounits 2>/dev/null)
    fallback_status=$?
    if [ "$fallback_status" -eq 0 ]; then
      [ -z "$fallback_output" ] || printf '%s\n' "$fallback_output"
    else
      printf 'GPU_ERROR\t%s\n' "$fallback_status"
    fi
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
  # Identity covers at most the first 512 distinct PIDs per sample so the
  # workload payload stays far inside the collector's output budget.
  process_pids=$(printf '%s\n' "$process_output" | awk -F, '
    {
      pid=$2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", pid)
      if (pid ~ /^[0-9]+$/ && !seen[pid]++ && ++emitted <= 512) print pid
    }
  ')
  for process_pid in $process_pids; do
    [ -r "/proc/$process_pid/status" ] || continue
    command_line=$(head -c 255 "/proc/$process_pid/cmdline" 2>/dev/null | tr '\000' ' ')
    cgroup_value=""
    if [ "$workload_tier" -ge 2 ]; then
      cgroup_value=$(head -c 16384 "/proc/$process_pid/cgroup" 2>/dev/null)
    fi
    if [ "$workload_tier" -ge 2 ] && [ -r "/proc/$process_pid/environ" ]; then
      # Hex encoding keeps NUL delimiters distinct from newlines embedded in
      # attacker-controlled environment values and works with BusyBox awk,
      # whose multi-byte record separators do not support RS="\0" reliably.
      head -c 65536 "/proc/$process_pid/environ" 2>/dev/null | od -An -v -tx1
    fi | MOCOP_CGROUP="$cgroup_value" MOCOP_COMMAND="$command_line" awk -v pid="$process_pid" -v boot="$boot_epoch" -v clock="$clock_ticks" '
__CONTAINER_IDENTITY_AWK__
      function hex_digit(value) {
        value=tolower(value)
        return index("0123456789abcdef", value) - 1
      }
      function environment_record() {
        if (environment ~ /^SLURM_JOB_ID=/) slurm_id=substr(environment, index(environment, "=") + 1)
        else if (environment ~ /^SLURM_JOB_NAME=/) slurm_name=substr(environment, index(environment, "=") + 1)
        else if (environment ~ /^SLURM_JOB_PARTITION=/) slurm_queue=substr(environment, index(environment, "=") + 1)
        else if (environment ~ /^POD_UID=/) pod_id=substr(environment, index(environment, "=") + 1)
        else if (environment ~ /^POD_NAME=/) pod_name=substr(environment, index(environment, "=") + 1)
        else if (environment ~ /^POD_NAMESPACE=/) pod_namespace=substr(environment, index(environment, "=") + 1)
        else if (environment ~ /^KUEUE_LOCAL_QUEUE_NAME=/) pod_queue=substr(environment, index(environment, "=") + 1)
        environment=""
      }
      function clean(value) {
        gsub(/[[:cntrl:]]/, " ", value)
        return substr(value, 1, 255)
      }
      {
        for (field=1; field<=NF; field++) {
          if ($field == "00") environment_record()
          else environment=environment sprintf("%c", hex_digit(substr($field, 1, 1)) * 16 + hex_digit(substr($field, 2, 1)))
        }
      }
      END {
        if (environment != "") environment_record()
        cgroup = ENVIRON["MOCOP_CGROUP"]
        command_line = ENVIRON["MOCOP_COMMAND"]
        # One awk owns every per-PID /proc read: the Uid line from status and
        # the stat line joined into a single record, so a comm containing
        # newlines cannot break the start-time field position.
        uid=""
        rss_kib=""
        status_file = "/proc/" pid "/status"
        while ((getline proc_line < status_file) > 0) {
          if (proc_line ~ /^Uid:/) { split(proc_line, uid_fields); uid=uid_fields[2] }
          else if (proc_line ~ /^VmRSS:/) { split(proc_line, rss_fields); rss_kib=rss_fields[2] }
        }
        close(status_file)
        stat_buffer=""
        stat_file = "/proc/" pid "/stat"
        while ((getline proc_line < stat_file) > 0) {
          stat_buffer = stat_buffer proc_line " "
        }
        close(stat_file)
        ticks=""
        cpu_ticks=""
        if (sub(/^.*\) /, "", stat_buffer)) {
          split(stat_buffer, stat_fields, " ")
          ticks = stat_fields[20]
          # utime/stime are stat fields 14/15; the "comm) " strip removes two.
          if (stat_fields[12] ~ /^[0-9]+$/ && stat_fields[13] ~ /^[0-9]+$/) {
            cpu_ticks = stat_fields[12] + stat_fields[13]
          }
        }
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
        # Standalone container runtimes: "docker-<hex>.scope" (cgroup v2),
        # "/docker/<hex>" (cgroup v1) and "libpod-<hex>.scope" (Podman). The
        # segment anchor plus the 12-to-64 hex-digit requirement keeps look-
        # alike unit names out; the reported short identifier matches the
        # container ID the runtime CLI displays.
        detect_container(cgroup)
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
        } else if (container_kind != "") {
          kind=container_kind
          workload_id=substr(container_id, 1, 12)
        }
        started=""
        if (boot ~ /^[0-9]+$/ && ticks ~ /^[0-9]+$/ && clock ~ /^[0-9]+$/ && clock + 0 > 0) {
          started = boot + int(ticks / clock)
        }
        cpu_seconds=""
        if (cpu_ticks != "" && clock ~ /^[0-9]+$/ && clock + 0 > 0) {
          cpu_seconds = int(cpu_ticks / clock)
        }
        rss_mib=""
        if (rss_kib ~ /^[0-9]+$/) { rss_mib = int(rss_kib / 1024) }
        printf "WORKLOAD\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
          pid, kind, clean(workload_id), clean(workload_name), clean(owner),
          clean(workload_queue), clean(workload_namespace), started,
          clean(command_line), cpu_seconds, rss_mib
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
        .replace("__CONTAINER_IDENTITY_AWK__", _CONTAINER_IDENTITY_AWK)
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
