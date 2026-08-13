# Mocop architecture

## Goals and constraints

Mocop monitors 10 to 200 Linux servers through the operator's existing OpenSSH aliases and credentials. A target requires no resident agent, database, or inbound monitoring port. One failed or slow host must not delay completed results from other hosts.

The browser receives current state after one page load. It may persist a bounded projection of fleet collection policy and promote or remove aliases through the constrained configuration controller. SSH arguments, thresholds, remote commands, listeners, paths, and arbitrary destinations remain local administrative inputs.

The installed runtime contains no inventory and has no third-party Python dependency. A blank installation starts with an empty allowlist and a loopback listener. AI-native refers to the GPU capacity, VRAM, diagnosis, and scheduling workflows; collection does not require an external AI service.

## System view

```text
local JSON configuration
        ├── explicit allowlist + optional OpenSSH alias discovery
        │
        ▼
  HostSource Protocol
        ▼
   MonitorService ──────────────────┐
        │ independent host deadlines│
        │ + bounded worker pool     │
        ▼                           │
 ResourceProbe Protocol             │
   openssh-linux-v6                 │
        │ fixed argv and script     │
        ▼                           │
 local shell or OpenSSH → /proc + /sys + df + nvidia-smi
        │ immutable results         │
        ▼                           │
     StateStore ◀───────────────────┘
      │       │        │
  snapshot  history  readiness
      │       ├── optional bounded SQLite writer
      │       └── IncidentPolicy → transition ring → maintenance overlay
      │                               ├── topology correlation
      │                               └── bounded webhook workers
      ├── JSON / SSE → dashboard
      └── runtime config ← ConfigInventory ← bounded settings + eligible aliases

 display-only topology ── ConfigInventory ── `/api/topology` ── dashboard
```

The dependency direction is `web → StateStore ← service → protocols/models/config`. The web layer has no knowledge of the SSH implementation. The scheduler consumes `HostSource` and `ResourceProbe` protocols through registries, which keeps environment-specific collection behind stable interfaces.

## Components

| Module | Responsibility |
|---|---|
| `config.py` | configuration discovery, strict schema validation, safe defaults |
| `discovery.py` | explicit inventory and optional OpenSSH alias discovery |
| `inventory.py` | typed dashboard configuration projection and private atomic mutation |
| `metrics.py` | deterministic OpenMetrics 1.0 snapshot exposition |
| `probe.py` | bounded process execution, fixed remote probe, protocol parsing |
| `service.py` | concurrent scheduling, failure backoff, state publication |
| `models.py` | immutable resource result types |
| `incidents.py` | condition evaluation, bounded transition history, and raw/actionable counts |
| `correlation.py` | possible shared-path grouping without changing incident truth |
| `diagnostics.py` | deterministic incident guidance and redacted support bundles |
| `persistence.py` | optional bounded asynchronous SQLite history |
| `notifications.py` | HTTPS webhook validation, deduplication, throttling, and delivery |
| `web.py` | fixed HTTP routes, JSON/SSE delivery, bounded configuration controls |
| `lifecycle.py` | private config creation and user-level systemd management |
| `static/` | dependency-free dashboard assets |

## Boundaries and canonical formats

Configuration uses JSON. Startup rejects unknown keys, invalid types, unsafe aliases, and values outside documented limits. Optional host overrides can pace a measured slow target and give only that target a longer complete-probe timeout; shared host groups attach bounded navigation metadata to explicit active aliases. Resolution prefers an explicit path, then the environment, the standard user configuration directory, a development-only local path, and finally the bundled empty configuration.

The optional connection topology is a validated, directed tree of safe display aliases.
Links contain an enumerated logical transport and an optional bounded label. A topology
alias may describe the monitoring host, an excluded jump host, an FRP endpoint, or a
monitored server; it becomes a live resource node only when the independent active
inventory contains the same alias. Topology fields never enter `HostSource`, a process
argument, or a command. The dedicated `/api/topology` projection is loaded without an
OpenSSH scan or remote probe and is fetched separately from the telemetry SSE.
[ADR-0008](adr/0008-configured-ssh-connection-topology.md) records the alternatives.

Collection produces immutable `ProbeResult`, `SystemMetrics`, `DiskMetrics`,
`GpuMetrics`, `GpuHealthMetrics`, `GpuProcess`, and optional `WorkloadMetadata`
values. The system section uses the versioned `MONITOR_V8` tab-separated protocol.
The parser accepts only that current version: the fixed script and its parser ship
in one process and the script is re-sent on every probe, so no emitter of an older
version can exist ([ADR-0016](adr/0016-single-version-protocol-and-agent-api.md)
records this single-version policy). NVIDIA device and
hardware-health fields share one CSV query at the host cadence. Compute processes use
a second query on the independent, config-bounded process cadence; skipped samples
reuse the last successful process snapshot and preserve its observation time. Optional workload mode reads
bounded `/proc` metadata only for those returned PIDs and recognizes Slurm or Kubernetes
identity without calling their control APIs. If the combined query is unsupported, the
fixed script falls back to base GPU fields and marks health unavailable. All sections
and record counts are bounded. [ADR-0003](adr/0003-gpu-reliability-and-authoritative-incidents.md)
and [ADR-0011](adr/0011-bounded-operations-extensions.md) record the health and workload
decisions. [ADR-0014](adr/0014-tiered-gpu-process-telemetry.md) records process pacing.

The browser receives UTF-8 JSON snapshots through SSE. `/api/snapshot` supports cold start and diagnostics. `/metrics` renders the same current snapshot as OpenMetrics 1.0 with an exact media type and no collection side effect; stale host resources are omitted from current resource series. Host and GPU history queries accept only discovered telemetry identities and at most 300 points. The redacted diagnostic projection requires a dashboard read marker and exposes neither raw connection errors nor process identity. Incident queries accept limits from 1 to 200. The cadence shortcut accepts one finite JSON number from 2 to 60. The collector route accepts exactly cadence, complete-probe timeout, and integer worker concurrency within documented bounds. The inventory route accepts one exact add/remove action and one validated alias. An add must match a fresh, eligible OpenSSH scan; a remove must match the current configuration. The host-group route accepts one explicit host and one bounded visible group or clear action. Maintenance and condition-action routes accept only an explicit host, fixed finite duration or clear action, and bounded reason; the service generates the UTC expiry. A manual-probe route accepts one active alias and only advances that host's existing fixed probe deadline. The notification-test route accepts an empty object and exercises only preconfigured endpoints. All write routes use the same bounded same-origin dashboard-request guard, serialize durable changes through `ConfigInventory`, and hot-apply the validated immutable `MonitorConfig` after atomic persistence.

The optional `local_host` alias must be present in the explicit host allowlist. It executes the same repository-owned script through a local `sh` process; every other target uses a structured OpenSSH argument vector. Stdout and stderr are drained incrementally into buffers that share one configured byte limit. A timeout or limit violation terminates the isolated process group.

## State and collection lifecycle

Each host has one scheduler-owned deadline and at most one in-flight probe. Results are
published as soon as they complete; later due work does not wait for an older slow
batch. A completed submission batch updates the latest collection time and duration.
The state version increases on observable changes, which lets SSE clients reject older
snapshots that arrive after a cadence update. Concurrent SSE readers share the same
read-only snapshot projection for one state revision; general snapshot callers receive
a deep copy, and the HTTP layer separately reuses the serialized bytes.

`StateStore` retains the current snapshot, bounded successful history per host and GPU,
bounded GPU-process transitions, a bounded last-good process sample per host, and a bounded incident ring. History uses immutable
slotted records internally and materializes API dictionaries only when read or queued
for enabled persistence. Process transitions require two consecutive samples with
available and actually sampled process telemetry. An intentional skip preserves the
comparison baseline; a failed probe, unavailable task query, or missing GPU
invalidates the comparison baseline instead of inventing starts and stops. Persistence
is disabled by default. When enabled, a dedicated writer stores successful trend points, GPU
samples, process transitions, and incident transitions in SQLite; collection threads
only attempt bounded, non-blocking queue insertions.
Startup restores bounded history and transition context, not stale current resource
state. Restored data for hosts outside the active configuration is discarded on
inventory initialization. Retention and database page limits are configuration bounds.
OpenMetrics remains current-state only.

`IncidentPolicy` is the sole authority for connectivity, CPU, memory, swap, filesystem, GPU availability, pressure, temperature, and hardware-health conditions. `IncidentTracker` applies bounded activation and recovery cycles while preserving previous resource conditions across failed probes, so transient samples and missing telemetry are not mistaken for stable failure or recovery.

Time-bounded maintenance and condition-level actions are overlays on that authority,
never inputs to collection or condition state. Acknowledgement records ownership while
retaining recovery delivery; silence suppresses new notifications for that condition.
Snapshots retain raw active and critical counts and add actionable counts that exclude
maintained, acknowledged, or silenced conditions. Action changes and natural expiry
advance the incident-view revision without inventing an incident transition.
[ADR-0007](adr/0007-time-bounded-maintenance-overlay.md) records the rejected
pause-collection and drop-incident alternatives; [ADR-0013](adr/0013-operational-diagnostics-and-gpu-history.md)
records the condition-action, GPU-history, manual-probe, and diagnostic boundaries.

The topology correlator consumes only actionable connectivity conditions. It may emit
one possible shared-path projection for multiple descendants, but the raw active list
and transition log remain unchanged. Webhook workers consume actionable transitions
after this overlay and may include the current correlation context.

## Dashboard rendering

GPU count, busy devices, and cluster VRAM form the first summary layer. The capacity matcher ranks same-node, same-model groups from the current snapshot by requested device count, per-device free VRAM, health, utilization, and CPU context; it excludes stale and maintained nodes and never triggers collection. The fleet rail can render config-backed host sections without changing telemetry order or collection. The scheduling heatmap follows, then system resources and native per-host GPU groups. GPU groups are collapsed by default. Search and status filters temporarily expand matching groups without losing the user's explicit expansion state.

SSE updates are coalesced with `requestAnimationFrame`. GPU groups, host rows, attention items, incidents, and heatmap cells reuse DOM when their input signature is unchanged. A successful SSE snapshot is authoritative for connection state; transient errors are debounced, and snapshot fetches provide bounded degraded-mode synchronization during recovery. Capacity matching and compute, VRAM, and temperature heatmap modes transform the in-memory snapshot without another request. CSV export is generated from visible rows in the browser.

Display-only preferences use a versioned, validated browser-local record. They control one curated visual style, one independent palette, background visibility, information density, fleet focus, server order, GPU sort, heatmap metric, and optional columns. Six style families change layout, spacing, typography, geometry, and material; six palettes change emphasis, ambient, surface, line, and interaction colors without changing component structure. A separately bounded raster background may be retained as one IndexedDB `Blob`; it is decoded before storage, rendered through a revocable object URL and never uploaded. These values never enter the server configuration or overwrite another viewer's choices. Cluster inventory and the narrow collector-policy projection cross the serialized configuration controller. The attention view consumes backend incident conditions and only groups them for presentation; threshold decisions are never duplicated in JavaScript. [ADR-0009](adr/0009-orthogonal-visual-style-and-accent.md) records the visual model and rejected custom-CSS alternative. [ADR-0002](adr/0002-local-targets-and-dashboard-preferences.md) records the rejected server-persisted presentation and on-demand remote-query alternatives. [ADR-0004](adr/0004-dashboard-managed-ssh-inventory.md) records the constrained inventory boundary; [ADR-0005](adr/0005-dashboard-persisted-collector-settings.md) records the collector-settings allowlist and rejected general editor; [ADR-0006](adr/0006-browser-local-visual-assets.md) records the local visual-asset boundary and rejected server upload.

The connection-topology dialog builds its static tree only when requested and reuses
that DOM across telemetry updates. Health, staleness, CPU use, and GPU count come from
the current in-memory snapshot. Topology-only aliases are neutral infrastructure nodes;
unmapped monitored aliases remain visible as a configuration gap. Opening the dialog
never initiates collection.

## Process and service model

Package installation and service management are separate operations. `mocop init` creates a non-overwriting `0600` user configuration. `mocop service install` validates that configuration, generates a unit for the active Python environment, enables the user service, and starts it.

The user service is intentional because OpenSSH configuration, `known_hosts`, keys,
and agent sockets belong to that identity. The unit invokes `systemctl --user` without
a shell and applies `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, restricted
address families, and `UMask=0077`. `ReadWritePaths` grants write access only to the
selected configuration directory; `StateDirectory=mocop` grants a private state path
for optional SQLite. An optional private `environment` file beside `config.json`
supplies webhook URL and signing-secret variables.

The generated unit also marks the process as supervised. Only this mode exposes the
bounded dashboard restart capability: the HTTP handler acknowledges an exact
same-origin request, signals a graceful exit, and lets systemd's existing restart
policy create the replacement. It never invokes `systemctl` or a shell. The browser
waits for the snapshot `startedAt` identity to change before reloading its static
assets. Foreground processes fail closed, and cancellable probes terminate active SSH
process groups during shutdown. [ADR-0012](adr/0012-supervised-dashboard-restart.md)
records the alternatives and security boundary.

## Failure model

- Host sets are refreshed independently of probe completion; configuration changes wake the scheduler immediately.
- Connection and complete-probe timeouts are independent and bounded.
- Repeated host failures use exponential backoff capped at 60 seconds. A bounded, deterministic per-host jitter disperses shared-path recovery load; healthy hosts retain the normal cadence.
- A stale OpenSSH multiplexed transport may retry once inside the original complete-probe deadline. Authentication, host-key, timeout, refusal, and routing failures are never retried immediately.
- A discovery failure uses a bounded retry deadline and cannot spin the scheduler.
- A measured slow host may use a bounded longer timeout and slower cadence without changing the browser-controlled fleet cadence.
- A persisted cadence change wakes the scheduler and rebases existing retry deadlines; timeout and worker changes apply to future probe cycles.
- A manual probe advances one known host's deadline without changing cadence, never overlaps an in-flight probe, coalesces duplicate requests, and enforces a per-host cooldown.
- Removing a host clears its retry, manual-probe cooldown, GPU-process and rate baselines, and restored-history state before the alias can be added again.
- Maintenance windows never change scheduling; their UTC expiry automatically restores active conditions to the actionable view.
- Raw SSH stderr is classified locally and never crosses the browser boundary.
- Failed hosts keep their last successful data, marked stale and excluded from current totals.
- SSE sends a heartbeat every 15 seconds and relies on native `EventSource` reconnection.
- `/healthz` reports process liveness; `/readyz` requires a discovered target and one successful sample.
- The default listener is loopback. Remote access requires external TLS and authenticated authorization.
- A fatal collector scheduler failure exits the process non-zero so the user service restarts it.

[ADR-0010](adr/0010-independent-host-scheduling.md) records the scheduler alternatives;
[ADR-0011](adr/0011-bounded-operations-extensions.md) records persistence, correlation,
workload, and notification boundaries.

## Performance decision

SSH connection and network wait dominate current collection cost. Existing evidence does not justify a Rust rewrite because it would not remove those round trips. Re-evaluate the language or agent architecture only after profiling a fixed workload that exceeds 200 hosts, requires a sustained interval below 2 seconds, saturates one CPU core, or exceeds 512 MiB resident memory.

The reproducible measurement contract lives in [PERFORMANCE.md](PERFORMANCE.md). Security boundaries live in [SECURITY.md](SECURITY.md).

## Repository layout

```text
.github/   contribution, conduct, security, and CI policy
docs/      architecture, decisions, performance, security, and release history
examples/  publication-safe operator examples
mocop/     runtime package and embedded dashboard
tests/     unit, contract, fixture, and browser coverage
```

The package remains at the repository root to preserve direct, dependency-free test execution. [ADR-0001](adr/0001-repository-layout.md) records the rejected `src/` alternative and the removal of duplicate packaging and service artifacts.
