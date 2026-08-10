# Mocop architecture

## Goals and constraints

Mocop monitors 10 to 200 Linux servers through the operator's existing OpenSSH aliases and credentials. A target requires no resident agent, database, or inbound monitoring port. One failed or slow host must not delay completed results from other hosts.

The browser receives current state after one page load. It may persist a bounded projection of fleet collection policy and promote or remove aliases through the constrained configuration controller. SSH arguments, thresholds, remote commands, listeners, paths, and arbitrary destinations remain local administrative inputs.

The installed runtime contains no inventory and has no third-party Python dependency. A blank installation starts with an empty allowlist and a loopback listener. AI-native refers to the GPU capacity, VRAM, diagnosis, and scheduling workflows; collection does not require an external AI service.

## System view

```text
local JSON configuration
        │ explicit allowlist and optional OpenSSH alias discovery
        ▼
  HostSource Protocol
        ▼
   MonitorService ──────────────────┐
        │ bounded worker pool       │
        ▼                           │
 ResourceProbe Protocol             │
   openssh-linux-v4                 │
        │ fixed argv and script     │
        ▼                           │
 local shell or OpenSSH → /proc + /sys + df + nvidia-smi
        │ immutable results         │
        ▼                           │
     StateStore ◀───────────────────┘
      │       │        │
  snapshot  history  readiness
      │       └── IncidentPolicy → bounded transition ring → maintenance overlay
      ├── JSON / SSE → dashboard
      └── runtime config ← ConfigInventory ← bounded settings + eligible aliases
```

The dependency direction is `web → StateStore ← service → protocols/models/config`. The web layer has no knowledge of the SSH implementation. The scheduler consumes `HostSource` and `ResourceProbe` protocols through registries, which keeps environment-specific collection behind stable interfaces.

## Components

| Module | Responsibility |
|---|---|
| `config.py` | configuration discovery, strict schema validation, safe defaults |
| `discovery.py` | explicit inventory and optional OpenSSH alias discovery |
| `inventory.py` | typed dashboard configuration projection and private atomic mutation |
| `probe.py` | bounded process execution, fixed remote probe, protocol parsing |
| `service.py` | concurrent scheduling, failure backoff, state publication |
| `models.py` | immutable resource result types |
| `incidents.py` | condition evaluation, bounded transition history, and raw/actionable counts |
| `web.py` | fixed HTTP routes, JSON/SSE delivery, bounded configuration controls |
| `lifecycle.py` | private config creation and user-level systemd management |
| `static/` | dependency-free dashboard assets |

## Boundaries and canonical formats

Configuration uses JSON. Startup rejects unknown keys, invalid types, unsafe aliases, and values outside documented limits. Optional host overrides can pace a measured slow target and give only that target a longer complete-probe timeout; shared host groups attach bounded navigation metadata to explicit active aliases. Resolution prefers an explicit path, then the environment, the standard user configuration directory, a development-only local path, and finally the bundled empty configuration.

Collection produces immutable `ProbeResult`, `SystemMetrics`, `DiskMetrics`, `GpuMetrics`, `GpuHealthMetrics`, and `GpuProcess` values. The system section uses the versioned `MONITOR_V4` tab-separated protocol. NVIDIA device, compute-process, and optional hardware-health data use the stable CSV mode of `nvidia-smi`. Parsers validate versions, columns, text length, numeric ranges, record counts, process identifiers, and GPU indexes. An optional health-query failure never invalidates base resource telemetry. [ADR-0003](adr/0003-gpu-reliability-and-authoritative-incidents.md) records the agentless decision and rejected DCGM-first alternative.

The browser receives UTF-8 JSON snapshots through SSE. `/api/snapshot` supports cold start and diagnostics. History queries accept only discovered aliases and at most 300 points. Incident queries accept limits from 1 to 200. The cadence shortcut accepts one finite JSON number from 2 to 60. The collector route accepts exactly cadence, complete-probe timeout, and integer worker concurrency within documented bounds. The inventory route accepts one exact add/remove action and one validated alias. An add must match a fresh, eligible OpenSSH scan; a remove must match the current configuration. The host-group route accepts one explicit host and one bounded visible group or clear action. The maintenance route accepts one explicit host, a fixed finite duration or clear action, and a bounded reason; the service generates the UTC expiry. All write routes use the same bounded same-origin dashboard-request guard, serialize through `ConfigInventory`, validate the complete candidate configuration, and hot-apply the resulting immutable `MonitorConfig` after atomic persistence.

The optional `local_host` alias must be present in the explicit host allowlist. It executes the same repository-owned script through a local `sh` process; every other target uses a structured OpenSSH argument vector. Stdout and stderr are drained incrementally into buffers that share one configured byte limit. A timeout or limit violation terminates the isolated process group.

## State and collection lifecycle

Each host result is published as soon as it completes. A collection cycle writes authoritative completion time and duration when all scheduled work finishes. The state version increases on observable changes, which lets SSE clients reject older snapshots that arrive after a cadence update.

`StateStore` retains the current snapshot, bounded successful history per host, and a bounded incident ring. It does not persist telemetry. Trends and incident bodies are fetched only when needed; SSE snapshots carry the current state and compact incident metadata.

`IncidentPolicy` is the sole authority for connectivity, CPU, memory, swap, filesystem, GPU availability, pressure, temperature, and hardware-health conditions. `IncidentTracker` applies bounded activation and recovery cycles while preserving previous resource conditions across failed probes, so transient samples and missing telemetry are not mistaken for stable failure or recovery.

Time-bounded maintenance is an overlay on that authority, never an input to collection or condition state. Snapshots retain raw active and critical counts and add actionable counts that exclude currently silenced hosts. Maintenance changes and natural expiry advance the incident-view revision, so every browser refreshes the overlay without an invented incident transition. [ADR-0007](adr/0007-time-bounded-maintenance-overlay.md) records the rejected pause-collection and drop-incident alternatives.

## Dashboard rendering

GPU count, busy devices, and cluster VRAM form the first summary layer. The capacity matcher ranks same-node, same-model groups from the current snapshot by requested device count, per-device free VRAM, health, utilization, and CPU context; it excludes stale and maintained nodes and never triggers collection. The fleet rail can render config-backed host sections without changing telemetry order or collection. The scheduling heatmap follows, then system resources and native per-host GPU groups. GPU groups are collapsed by default. Search and status filters temporarily expand matching groups without losing the user's explicit expansion state.

SSE updates are coalesced with `requestAnimationFrame`. GPU groups, host rows, attention items, incidents, and heatmap cells reuse DOM when their input signature is unchanged. A successful SSE snapshot is authoritative for connection state; transient errors are debounced, and snapshot fetches provide bounded degraded-mode synchronization during recovery. Capacity matching and compute, VRAM, and temperature heatmap modes transform the in-memory snapshot without another request. CSV export is generated from visible rows in the browser.

Display-only preferences use a versioned, validated browser-local record. They control theme, background visibility, information density, fleet focus, server order, GPU sort, heatmap metric, and optional columns. A separately bounded raster background may be retained as one IndexedDB `Blob`; it is decoded before storage, rendered through a revocable object URL and never uploaded. These values never enter the server configuration or overwrite another viewer's choices. Cluster inventory and the narrow collector-policy projection cross the serialized configuration controller. The attention view consumes backend incident conditions and only groups them for presentation; threshold decisions are never duplicated in JavaScript. [ADR-0002](adr/0002-local-targets-and-dashboard-preferences.md) records the rejected server-persisted presentation and on-demand remote-query alternatives. [ADR-0004](adr/0004-dashboard-managed-ssh-inventory.md) records the constrained inventory boundary; [ADR-0005](adr/0005-dashboard-persisted-collector-settings.md) records the collector-settings allowlist and rejected general editor; [ADR-0006](adr/0006-browser-local-visual-assets.md) records the local visual-asset boundary and rejected server upload.

## Process and service model

Package installation and service management are separate operations. `mocop init` creates a non-overwriting `0600` user configuration. `mocop service install` validates that configuration, generates a unit for the active Python environment, enables the user service, and starts it.

The user service is intentional because OpenSSH configuration, `known_hosts`, keys, and agent sockets belong to that identity. The unit invokes `systemctl --user` without a shell and applies `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, restricted address families, and `UMask=0077`. `ReadWritePaths` grants write access only to the selected configuration directory; atomic replacement requires directory-level rename permission but does not make the rest of the home directory writable.

## Failure model

- Host sets are recalculated each cycle so configuration changes take effect without rebuilding state.
- Connection and complete-probe timeouts are independent and bounded.
- Repeated host failures use exponential backoff capped at 60 seconds; healthy hosts retain the normal cadence.
- A measured slow host may use a bounded longer timeout and slower cadence without changing the browser-controlled fleet cadence.
- A persisted cadence change wakes the scheduler and rebases existing retry deadlines; timeout and worker changes apply to future probe cycles.
- Maintenance windows never change scheduling; their UTC expiry automatically restores active conditions to the actionable view.
- Raw SSH stderr is classified locally and never crosses the browser boundary.
- Failed hosts keep their last successful data, marked stale and excluded from current totals.
- SSE sends a heartbeat every 15 seconds and relies on native `EventSource` reconnection.
- `/healthz` reports process liveness; `/readyz` requires a discovered target and one successful sample.
- The default listener is loopback. Remote access requires external TLS and authenticated authorization.

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
