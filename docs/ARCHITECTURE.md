# Mocop architecture

## Goals and constraints

Mocop monitors 10 to 200 Linux servers through the operator's existing OpenSSH aliases and credentials. A target requires no resident agent, database, or inbound monitoring port. One failed or slow host must not delay completed results from other hosts.

The browser receives current state after one page load. It may persist a bounded projection of fleet collection policy and promote or remove aliases through the constrained configuration controller. SSH arguments, thresholds, remote commands, listeners, paths, and arbitrary destinations remain local administrative inputs.

The installed runtime contains no inventory and has no third-party Python dependency. A blank installation starts with an empty allowlist and a loopback listener. AI-native refers to the GPU capacity, VRAM, diagnosis, and scheduling workflows; collection does not require an external AI service.

## System view

```text
local JSON configuration
        ├── explicit allowlist + optional OpenSSH alias/route discovery
        │
        ▼
  HostSource Protocol
        ▼
   MonitorService ──────────────────┐
        │ independent host deadlines│
        │ + bounded worker pool     │
        ▼                           │
 ResourceProbe Protocol             │
   OpenSshLinuxResourceProbe        │
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

 configured/cached resolved topology ── ConfigInventory ── `/api/topology` ── dashboard
```

The dependency direction is `web → StateStore ← service → protocols/models/config`.
The web layer has no knowledge of the SSH implementation. The entrypoint constructs
the one repository-owned host source and OpenSSH collector behind `HostSource` and
`ResourceProbe` protocols, keeping environment-specific collection behind stable
interfaces without a runtime plugin registry.

## Components

| Module | Responsibility |
|---|---|
| `config.py` | configuration discovery, strict schema validation, safe defaults |
| `privatefiles.py` | private lock and `0600` file primitives shared by the lifecycle and configuration controller |
| `hostnames.py` | canonical Host/Origin hostname normalization and the trusted web policy |
| `discovery_policy.py` | dependency-free SSH discovery policy parsing and bounds |
| `discovery.py` | explicit inventory and optional OpenSSH alias discovery |
| `ssh_topology.py` | bounded effective-route resolution, infrastructure classification, topology and grouping projection |
| `inventory.py` | typed dashboard configuration projection and private atomic mutation |
| `metrics.py` | deterministic OpenMetrics 1.0 snapshot exposition |
| `capacity.py` | server-side twin of the browser capacity matcher behind `GET /api/capacity` |
| `probe.py` | bounded process execution, fixed remote probe, protocol parsing |
| `remote_script.py` | the fixed `MONITOR_V8` collection script: protocol constants, template, rendering |
| `doctor.py` | read-only SSH reachability, connection-reuse, and collection diagnosis |
| `workloads.py` | strict workload-identity record parsing, including per-PID CPU/memory footprint |
| `service.py` | concurrent scheduling, failure backoff, state publication |
| `models.py` | immutable resource result types |
| `incidents.py` | condition evaluation, bounded transition history, and raw/actionable counts |
| `correlation.py` | possible shared-path grouping without changing incident truth |
| `diagnostics.py` | deterministic incident guidance and redacted support bundles |
| `persistence.py` | optional bounded asynchronous SQLite history |
| `notifications.py` | HTTPS webhook validation, deduplication, throttling, and delivery |
| `updates.py` | opt-in release polling, verified wheel-only self-update, restart gating |
| `api_manifest.py` | the machine-readable HTTP contract: routes, tiers, query schemas, body caps; `/api/meta` and the handlers share it |
| `web.py` | fixed HTTP routes, JSON/SSE delivery, bounded configuration controls |
| `static_assets.py` | static asset route table, strong ETags, and conditional-delivery validators |
| `lifecycle.py` | private config creation and user-level systemd management |
| `migration.py` | non-destructive cross-machine config transformation and private target creation |
| `static/` | dependency-free dashboard: `app.js` plus the browser leaves listed under [Maintainability boundary](#maintainability-boundary) |

## Boundaries and canonical formats

Configuration uses JSON. Startup rejects unknown keys, invalid types, unsafe aliases, and values outside documented limits. Optional host overrides can pace a measured slow target and give only that target a longer complete-probe timeout; shared host groups attach bounded navigation metadata to explicit active aliases. Resolution prefers an explicit path, then the environment, the standard user configuration directory, a development-only local path, and finally the bundled empty configuration.

The optional connection topology is a validated, directed tree of safe display aliases.
Links contain an enumerated logical transport and an optional bounded label. A topology
alias may describe the monitoring host, an excluded jump host, an FRP endpoint, or a
monitored server; it becomes a live resource node only when the independent active
inventory contains the same alias. Configured topology fields never enter a process
argument or command. With `ssh_discovery.mode: "topology"`, a cached `HostDiscoverySnapshot`
is instead built from bounded `ssh -G` output: raw proxy commands, users, and addresses
are discarded; inferred infrastructure aliases leave the automatic probe candidate set;
the closest known hop supplies a group, with shared numbered alias prefixes as the
fallback for direct targets. Explicit inventory, exclusions, groups, and a configured
topology override inference. The dedicated `/api/topology` projection remains
separate from telemetry SSE and never opens a remote connection. [ADR-0008](adr/0008-configured-ssh-connection-topology.md)
owns configured topology; [ADR-0022](adr/0022-resolved-ssh-topology-discovery.md) owns
resolved discovery.

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

The HTTP manifest in `api_manifest.py` assigns every route one of four explicit
tiers: public API discovery/health (P), Bearer-authenticated automation reads (A),
authenticated same-origin dashboard reads (R), or authenticated same-origin writes
(W). The same table declares each GET route's query parameters and each write's
body cap; the handlers validate against it and `/api/meta` serializes it, so an
agent can discover exactly how to call a deployment without the prose reference. The
per-install capability and browser delivery trade-off are recorded in
[ADR-0017](adr/0017-per-install-dashboard-capability.md). `/api/snapshot` supports
cold start and diagnostics. `/metrics` renders the same current snapshot as
authenticated OpenMetrics 1.0 with an exact media type and no collection side effect;
stale host resources are omitted from current resource series. Host and GPU history
queries accept only discovered telemetry identities and at most 300 points. The
redacted diagnostic projection requires a dashboard read marker and exposes neither
raw connection errors nor process identity. Incident queries accept limits from 1 to
200. The cadence shortcut accepts one finite JSON number from 2 to 60. The collector
route accepts exactly cadence, complete-probe timeout, and integer worker concurrency
within documented bounds. The inventory route accepts one exact add/remove action and
one validated alias. An add must match a fresh, eligible OpenSSH scan; a remove must
match the current configuration. The host-group route accepts one explicit host and
one bounded visible group or clear action. Maintenance and condition-action routes
accept only an explicit host, fixed finite duration or clear action, and bounded
reason; the service generates the UTC expiry. A manual-probe route accepts one active
alias and only advances that host's existing fixed probe deadline. The
notification-test route accepts an empty object and exercises only preconfigured
endpoints. All write routes use the same bounded same-origin dashboard-request guard,
serialize durable changes through `ConfigInventory`, and hot-apply the validated
immutable `MonitorConfig` after atomic persistence. The exact endpoint and error
contracts live in [API.md](API.md); JSON configuration fields and bounds live in
[CONFIGURATION.md](CONFIGURATION.md).

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

The HTTP boundary authenticates every private route with the per-install Bearer
capability. Browser writes additionally require an exact trusted backend Host and a
trusted Origin. Deployments with ephemeral Host-rewriting preview names may authorize
a bounded `*.example` HTTPS Origin suffix; suffix entries never authorize Host and no
`X-Forwarded-*` header participates in the trust decision.

`IncidentPolicy` is the sole authority for connectivity, CPU, memory, swap, filesystem, GPU availability, pressure, temperature, and hardware-health conditions. `IncidentTracker` applies bounded activation and recovery cycles while preserving previous resource conditions across failed probes, so transient samples and missing telemetry are not mistaken for stable failure or recovery.

Time-bounded maintenance and condition-level actions are overlays on that authority,
never inputs to collection or condition state. Acknowledgement records ownership while
retaining recovery delivery; silence suppresses new notifications for that condition.
Snapshots retain raw active and critical counts and add actionable counts that exclude
maintained, acknowledged, or silenced conditions. Action changes and natural expiry
advance the incident-view revision without inventing an incident transition.
Active conditions are rebuilt only from live post-start probes. A durable
generation-bound action gets one startup rebinding opportunity for a matching
condition; a healthy observation or subsequent recovery consumes it, preventing
the action from suppressing a later recurrence.
[ADR-0007](adr/0007-time-bounded-maintenance-overlay.md) records the rejected
pause-collection and drop-incident alternatives; [ADR-0013](adr/0013-operational-diagnostics-and-gpu-history.md)
records the condition-action, GPU-history, manual-probe, and diagnostic boundaries.

The topology correlator consumes only actionable connectivity conditions. It may emit
one possible shared-path projection for multiple descendants, but the raw active list
and transition log remain unchanged. Webhook workers consume actionable transitions
after this overlay and may include the current correlation context.

## Dashboard rendering

GPU count, busy devices, and cluster VRAM form the first summary layer. The capacity matcher ranks same-node, same-model groups from the current snapshot by requested device count, per-device free VRAM, health, utilization, and CPU context; it excludes stale and maintained nodes and never triggers collection. One optional capacity watch persists a saved demand in the browser and re-evaluates it on every accepted snapshot: the satisfaction edge raises an in-page banner, a title marker, and an opt-in browser notification under a bounded cooldown, then re-arms only after demand stops being satisfied. The watch is a browser-only projection and never adds an API call or SSH command. Agents and scripts get the identical ranking from `GET /api/capacity`, served by `capacity.py` from the same in-memory snapshot and active conditions; `tests/fixtures/capacity_match.json` pins the browser leaf and the Python module to one result so neither can drift. The fleet rail can render config-backed host sections without changing telemetry order or collection. The scheduling heatmap follows, then system resources and native per-host GPU groups. GPU groups are collapsed by default. Search and status filters temporarily expand matching groups without losing the user's explicit expansion state.

The unified inventory query also builds a bounded process result projection from
the authenticated in-memory snapshot. Its scope follows the selected host, so
the same control searches the fleet or one server without a new API request or
remote command. Literal NFKC-normalized terms match process identity, command,
PID, owner, workload, queue, and namespace; result DOM is capped while the full
match count remains visible. Results reuse keyed nodes across snapshots and open
the exact GPU with a dialog-local filter that runs before the 100-row display
limit. Stale cached process records and unavailable task telemetry remain
explicit. [ADR-0018](adr/0018-browser-process-search.md) records the rejected
GPU-row-only and server-side search alternatives.

The main GPU inventory also derives a weak-map-cached process summary for each
current GPU object, so active-process count, the largest known allocation, and
allocated process VRAM are visible without opening a device. The GPU detail dialog
is process-first: its bounded task workspace exposes attribution coverage, owner and
workload filtering, stable sorting, copy actions, and a transition back to fleet-wide
program search before showing historical charts. These are browser-only projections;
device utilization is never presented as per-process utilization, and no interaction
adds a remote NVIDIA command. [ADR-0020](adr/0020-process-centric-gpu-inventory.md)
records the rejected server-query and per-process sampling alternatives.

SSE updates are coalesced with `requestAnimationFrame`. GPU groups, host rows, attention items, incidents, and heatmap cells reuse DOM when their input signature is unchanged. A successful SSE snapshot is authoritative for connection state; transient errors are debounced, and snapshot fetches provide bounded degraded-mode synchronization during recovery. Capacity matching, process search, and compute, VRAM, and temperature heatmap modes transform the in-memory snapshot without another request. CSV export is generated from visible rows in the browser.

Display-only preferences use a versioned, validated browser-local record. They control one curated visual style, one independent palette, background visibility, information density, fleet focus, server order, GPU sort, heatmap metric, and optional columns. Six style families change layout, spacing, typography, geometry, and material; six palettes change emphasis, ambient, surface, line, and interaction colors without changing component structure. A separately bounded raster background may be retained as one IndexedDB `Blob`; it is decoded before storage, rendered through a revocable object URL and never uploaded. These values never enter the server configuration or overwrite another viewer's choices. Cluster inventory and the narrow collector-policy projection cross the serialized configuration controller. The attention view consumes backend incident conditions and only groups them for presentation; threshold decisions are never duplicated in JavaScript. [ADR-0009](adr/0009-orthogonal-visual-style-and-accent.md) records the visual model and rejected custom-CSS alternative. [ADR-0002](adr/0002-local-targets-and-dashboard-preferences.md) records the rejected server-persisted presentation and on-demand remote-query alternatives. [ADR-0004](adr/0004-dashboard-managed-ssh-inventory.md) records the constrained inventory boundary; [ADR-0005](adr/0005-dashboard-persisted-collector-settings.md) records the collector-settings allowlist and rejected general editor; [ADR-0006](adr/0006-browser-local-visual-assets.md) records the local visual-asset boundary and rejected server upload.

The connection-topology dialog builds its static tree only when requested and reuses
that DOM across telemetry updates. Health, staleness, CPU use, and GPU count come from
the current in-memory snapshot. Topology-only aliases are neutral infrastructure nodes;
unmapped monitored aliases remain visible as a configuration gap. Opening the dialog
never initiates collection.

## Process and service model

Package installation remains separate from local service deployment. On a fresh host,
`mocop deploy` composes configuration creation with verified service installation: it
uses the current hostname as the local target, enables resolved SSH discovery by default,
and refuses existing configuration or capability state. `mocop init` remains the
non-overwriting lower-level configuration command. `mocop service install` validates a
configuration, creates or validates the sibling private Bearer token, generates a unit
for the active Python environment, enables the user service, starts it, verifies active
state, and prints a fragment-bearing capability URL. [ADR-0024](adr/0024-fresh-host-fast-deployment.md)
records the composition and rejected remote-script alternative.

The user service is intentional because OpenSSH configuration, `known_hosts`, keys,
and agent sockets belong to that identity. The unit invokes `systemctl --user` without
a shell and applies only enforceable, portable hardening directives; the exact unit
contents, the reason it claims no mount-namespace sandbox, and the private
`environment` file for webhook secrets are owned by
[OPERATIONS.md](OPERATIONS.md#installed-state-and-ownership).

The generated unit also marks the process as supervised. Only this mode exposes the
bounded dashboard restart capability: the HTTP handler acknowledges an exact
same-origin request, signals a graceful exit, and lets systemd's existing restart
policy create the replacement. It never invokes `systemctl` or a shell. The browser
waits for the snapshot `startedAt` identity to change before reloading its static
assets. Foreground processes fail closed, and cancellable probes terminate active SSH
process groups during shutdown. [ADR-0012](adr/0012-supervised-dashboard-restart.md)
records the alternatives and security boundary.

The opt-in `updates` policy layers release currency on the same restart
authority: `check` polls the hardcoded official repository on a bounded
interval and the header pill reports it, while `self-update` also accepts one
fixed empty apply request that downloads the release wheel, verifies its
SHA-256 manifest entry, installs it with the environment's own toolchain,
proves the installed version through the target interpreter, and only then
signals the supervised restart. The browser never names a version; a failed
attempt leaves the running process serving.
[ADR-0026](adr/0026-dashboard-self-update.md) records the boundary and the
rejected notification-only and bootstrap-script alternatives.

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
- SSE sends a named heartbeat every 15 seconds. The dashboard consumes it through
  fetch streaming because native `EventSource` cannot attach the Bearer capability;
  the server has no unauthenticated mode.
- `/healthz` reports process liveness; `/readyz` requires a discovered target and one successful sample.
- The default listener is loopback. Remote access requires external TLS and authenticated authorization.
- A fatal collector scheduler failure exits the process non-zero so the user service restarts it.

[ADR-0010](adr/0010-independent-host-scheduling.md) records the scheduler alternatives;
[ADR-0011](adr/0011-bounded-operations-extensions.md) records persistence, correlation,
workload, and notification boundaries.

## Performance decision

SSH connection and network wait dominate current collection cost. Existing evidence does not justify a Rust rewrite because it would not remove those round trips. Re-evaluate the language or agent architecture only when one of the [architecture thresholds](PERFORMANCE.md#architecture-thresholds) that PERFORMANCE.md owns becomes real.

The reproducible measurement contract lives in [PERFORMANCE.md](PERFORMANCE.md).
Security boundaries live in [SECURITY.md](SECURITY.md); deployment, upgrade, and
rollback procedures live in [OPERATIONS.md](OPERATIONS.md).

## Repository layout

```text
.github/   contribution, conduct, security, and CI policy
docs/      governed references, decision records, assets, and localized onboarding
examples/  publication-safe operator examples
src/mocop/ runtime package and embedded dashboard
tests/     unit, contract, fixture, and browser coverage
```

The package lives under `src/` so the checkout can never shadow an installed
release and packaging stays isolated. `tests/__init__.py` prepends `src` to
`sys.path`, which preserves direct, dependency-free test execution from a
source checkout. Standard build and project entry files stay at the root; local
agent configuration, caches, build output, and dependency-solver state are not
repository structure. [The documentation portal](README.md) owns the audience
map and update triggers. [ADR-0025](adr/0025-src-package-layout.md) records the
`src/` migration; [ADR-0019](adr/0019-repository-and-documentation-governance.md)
still owns documentation governance and supersedes the earlier layout decision
in [ADR-0001](adr/0001-repository-layout.md).

## Maintainability boundary

Large orchestration modules have executable line ceilings; the ceilings are a
ratchet, not a target. A change that would cross one extracts a coherent leaf and
lowers the ceiling instead of increasing it. Browser leaves remain dependency-free
classic scripts loaded before `app.js`, expose one frozen namespace/factory, and
consume the authenticated snapshot rather than creating a second API or state
store. `app.js` keeps request transport, notification delivery, DOM rendering,
and dashboard lifecycle; each leaf owns one concern and is unit-tested in Node
by `tests/<leaf>_test.mjs`:

| Leaf | Owns |
|---|---|
| `dashboard-auth.js` | capability ingestion, fragment scrubbing, tab-scoped retention, the token prompt |
| `format.js` | pure numeric, memory, rate, and relative-time formatting; SSE chunk normalization |
| `api-contracts.js` | payload contracts: bounded normalizers for snapshot, incidents, inventory, collector, maintenance, group, and topology responses that throw on anything malformed |
| `process-search.js` | NFKC term normalization, bounded process/GPU matching, ranking and memory ordering |
| `gpu-tasks.js` | entry point, environment, footprint, and per-card summary projections of a GPU process |
| `capacity-match.js` | ranking same-host, same-model GPU candidates against a demand |
| `capacity-watch.js` | the durable watch, its armed/notified edge, cooldown, and presented text |
| `csv-export.js` | CSV cell escaping (including formula-injection defense) and row building |
| `update-pill.js` | release-currency polling cadence, pill state, and the fixed apply action |

Python extraction must keep each lock-owned invariant inside one module.

[ADR-0021](adr/0021-incremental-module-boundaries.md) compares immediate splitting,
incremental leaf extraction, and adding a build/registry layer. It selects the
incremental boundary and records the first extraction: bounded process search.
