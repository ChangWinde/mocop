# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

## [Unreleased]

### Added

- GPU task rows now lead with the real entry point instead of a bare
  interpreter name: when argv0 is `python`, `torchrun`, `bash` or another
  generic launcher, the module after `-m` or the first script argument
  becomes the row title, with the interpreter and the detected Python
  environment (conda env, project venv, uv tool, poetry virtualenv) shown
  as chips. The fleet-wide program search, the per-card top-process label
  and the name sort follow the same display name.
- The workload identity pass now also reports each PID's host-side
  footprint — cumulative CPU seconds and resident memory from the same
  `/proc` reads it already performs — and the task row renders it as
  "CPU 均值 N 核 · 主机内存 N GiB", so a stalled data loader is visible
  without shelling into the node. Snapshot `workload` objects gain
  `cpu_seconds` and `rss_mib` (both nullable).
- The truncated command line in a task row expands in place on click
  instead of requiring copy-and-paste to read the training config.

- `mocop deploy --help`, `mocop migrate --help`, and `mocop service --help`
  now describe every flag and action, and help output names the program
  `mocop` instead of `__main__.py`. The operations runbook gained a command,
  flag, and exit-code reference (`0`, `1`, `2`, `75`).
- `GET /api/meta` is now a complete machine-readable contract: every GET
  route lists its accepted `query` parameters with type, bounds, and default,
  every write lists its `body` field schema and `bodyLimitBytes`, every route
  names its `responseType`, and the document also publishes `conventions`,
  writer requirements, the error-code catalog, and a `documentation` URL for
  the running release. The manifest is generated from the same declarative
  table (`api_manifest.py`) the handlers validate against, replacing five
  hand-written query parsers.
- `mocop --version` prints the package version. `init`, `deploy`, `migrate`,
  and every `service` action accept `--json` and write one `{ok, code?, ...}`
  document, matching `config check --json` and `doctor --json`. Refusals stay
  on that envelope so an agent never has to parse English from stderr.
- `GET /api/meta` capabilities now include `updateSupported`. Excess
  connections answer `503 CONNECTION_LIMIT` as JSON instead of an empty body.
- `403 AUTHENTICATION_REQUIRED` responses carry a `hint` naming the header to
  send and where the capability file lives, plus the `documentation` URL, so
  an agent can recover from a cold start without out-of-band knowledge.
- `mocop config check --json` writes the validation report as one JSON
  document on stdout (also for a rejected configuration), matching
  `doctor --json`.
- `GET /api/capacity?gpus=N&min_vram_gib=G&model=M` answers the placement
  question from one bounded authenticated call: ranked same-host, same-model
  GPU groups with the available devices, deficit, minimum free VRAM, and the
  maintenance and health exclusions, using exactly the dashboard's matching
  rules. A shared fixture pins the browser leaf and the new `capacity.py` to
  one ranking, and the browser leaf now orders hosts by code point instead of
  the locale so both agree everywhere.
- `tests/gpu_tasks_test.mjs` now runs in CI: the workflow globs every browser
  leaf and contract test instead of maintaining a list that had drifted.

### Changed

- `doctor --json` now includes the local host in `hosts[]` (with
  `local: true`) and adds `ok` alongside the existing `status` field.
  `--once` / `--strict` with a subcommand are rejected as usage errors.
  Inventory payloads must publish `infrastructureHosts` and
  `sshDiscoveryMode`; the dashboard no longer defaults those fields.

- Workload record parsing moved from `probe.py` into a new `workloads.py`
  module, and the GPU task projections moved from `app.js` into the
  `gpu-tasks.js` leaf ([ADR-0021](adr/0021-incremental-module-boundaries.md));
  both budgets ratcheted down.
- The dashboard's payload normalizers (snapshot and incidents envelopes,
  inventory, collector settings, maintenance windows, host groups, topology)
  moved from `app.js` into the `api-contracts.js` leaf, which now has its own
  Node contract test covering every accepted shape and rejection. The host
  trend and per-GPU history loaders, previously two copies of the same
  bounded-backoff state machine spread over sixteen `view` fields, now share
  the `keyed-loader.js` leaf, whose retry, single-flight, and stale-response
  rules are tested with fake timers; selecting another host now also drops a
  pending history retry. The `app.js` ceiling ratcheted down to 6,450 lines.
- Every HTTP server instance now requires the Bearer capability: the
  unauthenticated server mode that only tests used is gone, `GET /api/meta`
  no longer reports the constant `authenticationRequired` flag, and the
  dashboard prompts for a missing capability without a `/api/meta` round trip.
- `GET /api/update` now enforces the reader tier its manifest entry always
  advertised (a marker-less request answers `403 UNTRUSTED_ORIGIN`), and a
  test proves every manifested tier against its handler.
- Every GET route whose manifest `query` is empty now answers a query string
  with `400 QUERY_NOT_ALLOWED`; `/api/snapshot`, `/api/events`, `/healthz`,
  and `/readyz` used to ignore one silently while five other routes rejected
  it. The rule is enforced once in the dispatcher from the manifest, and a
  test checks it for every route.
- Every write route now validates its body through the same manifest the
  `/api/meta` `body` schemas are generated from, in one documented order:
  shape and JSON-type problems are `INVALID_SCHEMA`, well-typed values
  outside the published alias grammar, `values`, bounds, or text length are
  `INVALID_SETTINGS`, and only cross-field rules stay in the handlers.
  `POST /api/settings/hosts` and `POST /api/probe` therefore answer an unsafe
  alias or unknown action with `INVALID_SETTINGS` (previously
  `INVALID_SCHEMA`), and a non-integer `durationSeconds` is `INVALID_SCHEMA`
  (previously `INVALID_SETTINGS`). The manifest also publishes the text
  `maximum` of every reason, group, and condition-key field, `nullable` on
  `incidentStartedAt`, the three request-framing codes it had omitted, and
  drops the one-route `500 INTERNAL_ERROR`: a host-group persist failure is
  `503 SERVICE_UNAVAILABLE` like every other configuration write. The
  collector bounds and the dashboard duration set now have one owner
  (`api_manifest.py`) instead of copies in `web.py` and `inventory.py`, and
  `web.py` shrank by 230 lines.
- A managed unit that omits `--access-token-file` now exits `2` with a
  message pointing at `mocop service install` instead of minting a token
  nobody was shown; units generated since 0.9.0 always pass the flag.
- The dashboard search box re-renders only the program-search panel and GPU
  table, and GPU groups are keyed on the rows they show rather than on the
  query text, so typing no longer rebuilds every visible group. Active
  incidents are indexed by host on acceptance; the GPU history, capacity
  candidate, owner, webhook endpoint, and All-servers resource views rebuild
  their DOM only when the data they show changes; and the per-second tick
  compares before it writes and skips cosmetic updates while the tab is hidden.
- Documentation gives each fact one owner: CONTRIBUTING.md owns the quality
  gate list, OPERATIONS.md the systemd unit and pinned install command,
  PERFORMANCE.md the architecture thresholds, API.md the capability rules.
  The architecture table lists every module and browser leaf, QUALITY.md
  re-measures the runtime profile and coverage against the current tree, and
  six ADRs record how later releases changed their decisions.

### Fixed

- The capacity-watch notification and title marker now fire while the tab is
  in the background. They were evaluated inside the `requestAnimationFrame`
  render, which browsers pause for hidden documents, so the alert the feature
  exists for arrived only when the operator returned to the tab.
- The release pill now starts on the polling recovery path, not only when the
  first snapshot arrives over the live stream.
- The owners dialog shows when its data was last collected instead of a
  static "实时更新" label.
- The incident detail dialog no longer overwrites a reason the operator is
  typing every time a snapshot arrives, and a condition that recovers while
  the dialog is open is shown as resolved with its actions withdrawn instead
  of closing the dialog under the cursor.
- Fleet rail items are valid HTML: the rows inside each `<button>` are now
  phrasing content, so assistive technology no longer has to flatten them.

### Removed

- `GET /api/service` and `POST /api/settings/poll-interval`, deprecated in
  0.9.0. Use the `capabilities` block of `GET /api/meta` and
  `POST /api/settings/collector` (any non-empty subset) instead.
- Dashboard fallbacks for server shapes that cannot occur (older snapshot,
  collector, maintenance, and capability payloads, the native `EventSource`
  transport, browser-side threshold defaults, and the `createImageBitmap`
  and `indexedDB` feature probes), plus unused leaf exports, dead CSS, and
  Python methods, protocol duck-typing, and Linux flag probes with no callers
  or purpose.

## [0.11.0] - 2026-08-25

### Added

- Added an opt-in release-currency pill to the dashboard header
  ([ADR-0026](adr/0026-dashboard-self-update.md)). `updates.mode: "check"`
  polls the hardcoded official repository on a bounded interval and shows
  whether the running release is current; `"self-update"` also enables a
  one-click apply that downloads the release wheel, verifies it against the
  release SHA-256 manifest, installs it with the environment's own pip or
  uv, proves the installed version through the target interpreter, and
  restarts through the supervised-restart path before the page reloads
  itself. The shipped default stays `"off"` with no outbound requests; the
  browser can never name a version, and a failed attempt keeps the running
  version serving with a visible manual-recovery hint
  (`GET /api/update`, `POST /api/update/apply`).

## [0.10.2] - 2026-08-25

### Changed

- Service-install health verification no longer transmits the dashboard
  capability. It confirms liveness with an unauthenticated request the
  service must reject, closing a narrow window where the token could reach a
  process that had bound the loopback port while the real unit crash-looped,
  and it now also flags a service that started without a readable token.
- Webhook status reports `suppressedDeliveries` separately from
  `droppedDeliveries`, so a deliberately withheld recovery (an unpaired
  resolved event, for example inside a maintenance window) no longer inflates
  the failure count or marks an endpoint unhealthy.

### Fixed

- Fixed the generated systemd unit's `EnvironmentFile=` line, which a prior
  change had quoted; systemd treats the whole line as one unquoted path, so
  the quoted form failed the absolute-path check and every install silently
  dropped its webhook credentials. The path is now emitted bare (verified
  with `systemd-analyze`), and paths with spaces load correctly.
- A malformed `nvidia-smi` payload is now treated as unavailable GPU
  telemetry. It previously degraded to an authoritative empty inventory that,
  combined with identity-absence recovery, silently resolved every per-GPU
  hardware incident within two cycles without opening an availability alert.
- Resolved-topology grouping now follows ADR-0022 exactly: only a trailing
  numeric segment (`training-1`/`training-2`) forms an inferred group. Shared
  word prefixes (`paris-web`/`paris-db`) and embedded model numbers
  (`gpu-a100`) no longer group hosts that are not one numbered fleet;
  explicit `host_groups` remain the override.
- `mocop migrate` no longer rejects deploying the monitor onto a machine that
  is already a topology node when the source has no local host; the topology
  collision guard now fires only for an actual local-host rename.
- The dashboard capacity watch reconciles with shared browser storage on
  every evaluation, so a watch stopped or replaced in one tab is no longer
  resurrected or overwritten by another tab. A wall-clock rollback no longer
  freezes its notification cooldown, and a watch on a long (up to 256-char)
  GPU model name now saves instead of failing silently.
- GPU severity escalation now requires consecutive confirmable samples,
  matching incident opening: two critical observations split by a probe-
  failure gap no longer accumulate into an escalation.

## [0.10.1] - 2026-08-25

### Fixed

- One exotic literal `Host` entry in the OpenSSH configuration (an IPv6
  literal, a token carrying `:` or `#`) no longer stops discovery for every
  monitored host: unsafe aliases now leave the candidate set with a visible
  scan warning while explicit hosts stay monitored (ADR-0022).
- Proxy-chain expansion for resolved topology is now iterative, so a
  pathologically deep `ProxyJump` chain can no longer overflow the
  interpreter stack and stop the collector.
- `mocop migrate` now refuses a new local alias that already appears in the
  configured topology with a direct explanation instead of a downstream
  schema error about self-links.
- After the webhook pairing table hits its capacity bound, unpaired
  recoveries are delivered instead of suppressed, so a long-lived incident
  whose pairing record was evicted can still resolve on the receiver.

## [0.10.0] - 2026-08-25

### Added

- Added `mocop deploy` as the fresh-server fast path. After package installation, one
  non-overwriting command configures the current host, enables safe SSH alias/topology
  discovery, creates a fresh capability, and installs and verifies the user service.
- Added non-destructive `mocop migrate` configuration generation for moving the
  monitor to another machine. It rebinds local identity, drops reported machine-bound
  policy, upgrades bounded topology discovery, preserves host admission by default,
  refuses existing targets/copied capabilities, and never copies secrets or history.
- Added opt-in, cached SSH topology discovery from bounded `ssh -G` resolution.
  Effective `ProxyJump` chains and common SSH-backed `ProxyCommand` forms identify
  infrastructure aliases, keep automatically discovered jump hosts out of the probe
  inventory, build the display tree, and group targets by their closest known hop,
  with shared numbered alias prefixes as a fallback for direct targets.
  Explicit inventory, exclusions, groups, and configured topology remain authoritative;
  group metadata may predeclare auto-discovered aliases without authorizing probes;
  raw commands, users, and addresses are never published.
- Added HTTPS-only `trusted_web_hosts` suffix entries (for example,
  `*.preview.example`) for ephemeral Host-rewriting dashboard proxies. Suffix entries
  authorize browser Origin only; exact backend Host validation and Bearer
  authentication remain mandatory.
- Added an explicit dashboard token prompt for bare and forwarded URLs. Submitted
  credentials are retained only after successful API authentication; malformed or
  rejected tokens remain unstored and do not trigger automatic retries.
- Added a capacity watch to the capacity matcher: one saved demand
  (count/model/per-GPU free VRAM) is re-evaluated on every accepted snapshot in
  the browser. The ready edge shows an in-page banner and title marker, sends an
  opt-in browser notification with a 60-second cooldown, re-arms only after the
  demand stops being satisfied, and persists per browser. No new API or SSH
  work is involved.
- Added one-click `ssh <alias>` copy to capacity candidates and the GPU detail
  dialog. The copied command uses the configured OpenSSH alias; display names
  stay presentation-only.

### Changed

- Moved the runtime package to `src/mocop` ([ADR-0025](adr/0025-src-package-layout.md)).
  Package names and wheel contents are unchanged. A source checkout still runs
  tests without installation: `tests/__init__.py` prepends `src` to `sys.path`,
  and discovery now names the repository root (`python3 -m unittest discover
  -s tests -t .`). Contributor commands reference `src/mocop` paths.

### Fixed

- Applied configured host display names consistently across node navigation, resource
  inventory, heatmap, topology, GPU details, search placement, incidents, and alerts.
- Fixed managed-service shutdown when no host-specific probe timeout is configured.
- Kept the `nvidia-smi` fallback query from emitting an empty success line when the
  combined query is unsupported and the base query returns no rows.
- Fixed the collection script for hosts without block devices: an empty
  `/sys/block` glob aborted the system `awk` pass and dropped every core CPU,
  memory, load, network, and I/O row, permanently misreporting a healthy host
  as failed. Exotic mounts (for example FUSE backends reporting `-` capacity),
  a malformed `nvidia-smi` row, and malformed pressure rows now degrade only
  their own section instead of rejecting the whole host sample.
- Restoring SQLite history now drops rows whose required percentage columns are
  NULL (foreign writers or partial corruption) instead of crashing the
  collector on every start until the row was deleted by hand.
- Non-ASCII Bearer credentials now fail closed with the documented 403 instead
  of crashing the comparison and resetting the connection; trusted-host and
  Origin checks now canonicalize the absolute DNS form (trailing dot).
- The generated unit now quotes `EnvironmentFile=` so configuration paths with
  spaces load webhook credentials instead of being silently ignored, and
  service health polling tolerates malformed HTTP responses during startup.
- Resolved incident transitions now always reach the webhook queue, so an
  alert that recovers inside a maintenance window or silence no longer leaves
  the receiver hanging. Opening a condition now requires consecutively
  confirmable samples, so two isolated spikes separated by a probe-failure gap
  can no longer open an incident. Conditions bound to a GPU identity that left
  a fully observed inventory (a replaced card) now recover instead of pinning
  ghost alerts until a restart.
- The dashboard now establishes its event stream and corrects the connection
  badge after recovering from a failed first snapshot, rejects structurally
  broken snapshots before they can freeze rendering, and backs off snapshot
  polling up to 30 seconds during an outage.

## [0.9.0] - 2026-08-16

### Added

- Added process occupancy to the main GPU inventory and reorganized GPU detail as
  a process-first workspace with allocation/attribution/runtime insights,
  owned/unowned filters, owner/workload drill-down, PID/command copy actions, and
  a direct transition into fleet-wide search. Current-results CSV now includes
  aggregate process count, allocation coverage, and freshness without exporting
  process identity. The shared weak-map summary reuses authenticated snapshot data
  and adds no remote command or API surface.
- Added enforceable repository line budgets, release/version drift tests, a
  measured 85% branch-coverage floor, tag-driven GitHub release workflow,
  structured issue/PR intake, and ADR-0021's incremental module boundary.
- Extracted the bounded process-search projection into a dependency-free static
  leaf with an independent Node contract test while preserving the browser path.
- Added a reproducible synthetic runtime/resource profile and a cross-dimensional
  quality assessment covering backend/browser performance, bounded retention,
  lifecycle resources, robustness evidence, scale thresholds, and explicit
  unmeasured boundaries.
- Added global and selected-server GPU process search over the authenticated
  snapshot, including literal matching for command, PID, owner, workload,
  queue, and namespace; results open the exact GPU, preserve focus across live
  refreshes, report stale/unavailable coverage, and remain bounded to 200 DOM
  rows. The per-GPU dialog now filters before its 100-row display cap and adds
  program-name sorting alongside VRAM and runtime.
- Added a private per-install Bearer capability for every telemetry, SSE,
  OpenMetrics, and write route; service and foreground startup print a fragment
  capability URL that the dashboard scrubs immediately and retains in tab-scoped
  session storage for reload-safe operation.
- Added explicit P/A/R/W API access tiers, authenticated curl/SSE/OpenMetrics
  examples, ADR-0017's authentication trade-off, a complete configuration-boundary
  reference, and an upgrade/rollback/uninstall operations runbook.
- Added documentation contract tests for the exact route/tier manifest, stable error
  codes, Bearer examples, configuration fields, and current collection protocol, plus
  an installed-wheel smoke job in CI.
- Added pressure stall telemetry (kernel PSI): the collector reads `/proc/pressure/{cpu,memory,io}` in the existing awk pass, the snapshot carries per-resource `some`/`full` averages under `system.pressure`, the dashboard shows a PSI tile, and `/metrics` exports `mocop_host_pressure_some_ratio` / `mocop_host_pressure_full_ratio`. New `pressure` incidents fire on the memory and I/O `some avg60` windows (`thresholds.psi_memory_some_pct` default 20, `thresholds.psi_io_some_pct` default 30; twice the threshold escalates to critical), catching nodes that stall while utilization still looks normal. Kernels without PSI degrade silently. Protocol bumps to `MONITOR_V8`.
- Added Docker and Podman container attribution to the `auto` workload tier: anchored cgroup segments (`docker-<hex>.scope`, `/docker/<hex>`, `libpod-<hex>.scope`) map GPU processes to `kind: "docker"`/`"podman"` with the runtime's 12-character short container ID, so a busy device points at the container occupying it.
- Added `GET /api/usage`: a per-owner GPU occupancy rollup over a bounded window (1–720 hours) pairing the process timeline with utilization samples — occupancy seconds, classified idle seconds and idle share, hosts, GPUs, and workload kinds per owner, with explicit coverage (`earliestDataAt`) and dropped-record reporting. The workload owners dialog gains a matching usage-window section with a selectable window.
- Added `mocop config check`, which parses and validates the configuration without starting the web server or opening SSH connections and reports the resolved path, host count, subsystem state, and each webhook's environment-variable names with their set/unset status (exit 0 valid, 2 invalid).
- Added `mocop doctor --probe`, which runs one real production collection per alias and reports probe status, latency, GPU and process counts, and workload coverage; it requires live connection tests and conflicts with `--no-connect`.
- Added `mocop --once --strict` for scripts and cron jobs: it exits 1 unless every configured host produced an online sample and lists the failing hosts on stderr.
- Added `thresholds.disk_min_free_gib` (default 5) so filesystem severity reflects absolute headroom, not only percentage: an alerting mount with less free space than that becomes critical even below the percentage-critical mark, while partitions under the warning threshold are never escalated. Disk diagnoses now also carry `freeSpace` and `capacity` evidence in GiB, which previously left percentages as the only triage signal.
- Added a doctor warning when several aliases resolve to the same expanded `ControlPath`, since a shared multiplex socket can attach sessions to the wrong host.
- Added a self-describing `GET /api/meta` endpoint reporting the API version, app version, schema version, capability flags, and the complete endpoint manifest with access tiers.
- Added a stable machine-readable `code` to every API error envelope, JSON 404/405 responses (with an `Allow` header) for API-family paths, and a `Retry-After` header on manual-probe rate limits.
- Added per-endpoint webhook delivery status (health, queue depth, last attempt and success) to the notifications status and the settings page.
- Added weekly maintenance schedule visibility: the settings view lists every configured window with its `active` flag and a recurring badge even outside the active instance, while recurring windows stay editable only through the configuration JSON.
- Added strong `ETag` validators with `If-None-Match` revalidation (304) to the static dashboard assets.
- Added post-install verification to `mocop service install`: it waits for the unit to report active, then prints the dashboard URL and the log-follow command.
- Added optional per-host display names (`host_overrides.<alias>.display_name`) that label the fleet list without changing collection identity.
- Added weekly recurring maintenance windows (`maintenance_windows.<alias>.recurrence` with UTC weekday, start, and duration) that silence actionable alerts during every instance while collection continues; the dashboard badge shows the current instance's end time.
- Added a browser-local "workload owners" view that aggregates current GPU memory, device, host, and process counts per Slurm/Kubernetes/process owner from the existing snapshot without extra requests.
- Added the stale-transport retry flag to per-host trend history points, so link flapping is visible over time and survives optional persistence restarts.
- Added a read-only `mocop doctor` command that verifies non-interactive SSH reachability under the probe transport discipline, reports cold versus multiplexed connection latency, and flags disabled `ControlMaster`, missing or group-accessible control-socket directories, and ineffective `ControlPersist`.
- Added `mocop doctor --profile`, which decomposes each alias's collection latency into transport, fixed-script, and NVIDIA-query stages so slow hosts can be attributed to the network path or to remote execution.
- Added a best-effort doctor check that warns when the installed package is newer than the running user service, so an upgrade is not silently left unapplied.
- Added per-host stale-transport retry visibility (`transportRetried` in snapshots, `mocop_host_probe_transport_retried` in OpenMetrics) and a cumulative retry counter in `/healthz`.
- Added an optional `trusted_web_hosts` list so the dashboard accepts browser `Host`/`Origin` headers beyond the loopback and non-wildcard `listen_host` defaults; writes and protected reads now require a trusted `Host`, closing a DNS-rebinding path when the service is bound to a non-loopback address or fronted by a proxy.
- Added a `workloads.mode: "identity"` tier that reports each GPU process's owner (real UID via passwd), bounded command line, and true start time from `/proc` at roughly a third of the full tier's per-PID reads; protocol `MONITOR_V7` carries the new columns.
- Added per-process runtime to the GPU dialog: the true start time when a workload tier reports it, otherwise a monitor-observed first-seen lower bound tracked at zero remote cost; process rows also show the command line when available, sort by memory or runtime, and refresh in place without losing scroll position.

- Added a centered per-GPU workload view with aggregate and per-process VRAM, PID and optional job context; parsing is bounded and the browser renders at most the 100 largest processes.
- Added an explicit `local_host` target that uses the fixed resource probe without an SSH connection.
- Added draggable server ordering, GPU/CPU activity in the fleet list, and browser-local display preferences.
- Added optional GPU ECC, memory-repair, hardware-slowdown, and MIG telemetry without making base collection depend on the health query.
- Added validated expected GPU counts, VRAM pressure, sustained idle-VRAM detection, and configurable incident stability windows.
- Added validated per-host pacing and timeout overrides for empirically slow GPU nodes.
- Added a dashboard SSH-alias inventory scan with Git/GitHub/GitLab filtering, constrained add/remove, private atomic persistence, and live scheduler updates.
- Added six structurally distinct browser-local visual styles and six independent accent palettes.
- Added a centered, responsive settings workspace with browser-local density and fleet-focus preferences.
- Added durable dashboard controls for collection cadence, complete-probe timeout, and worker concurrency.
- Added a validated, browser-local custom background with visibility control.
- Added persistent, time-bounded maintenance windows with continuous collection, automatic expiry, and separate raw/actionable incident counts.
- Added a snapshot-only GPU capacity matcher with same-node/model constraints, free-VRAM requests, health exclusions, and ranked near matches.
- Added config-backed host groups with bounded dashboard editing, live state updates, grouped fleet navigation, and cached rendering.
- Added a dependency-free OpenMetrics 1.0 endpoint for current collection, cluster, host, GPU, and hardware-health gauges.
- Added low-cardinality per-host raw and maintenance-aware actionable incident gauges.
- Added bounded browser-local compression for background sources between 8 MiB and 32 MiB.
- Added a validated SSH/FRP connection map whose display-only infrastructure nodes stay outside the probe allowlist.
- Added optional bounded SQLite persistence for restart-safe trends and incident context.
- Added possible shared-path incident aggregation from the configured topology without hiding node incidents.
- Added opt-in read-only Slurm and Kubernetes identity for active GPU processes.
- Added environment-backed HTTPS webhooks with HMAC signing, deduplication, throttling, and bounded retries.
- Added dashboard and OpenMetrics health visibility for optional persistence and notification workers.
- Added a confirmed dashboard restart for the supervised user service with automatic browser recovery.
- Added condition-level acknowledgement and silence with durable expiry, scoped incident thresholds, and exact disk-mount exclusions.
- Added deterministic incident diagnosis, a redacted support bundle, and bounded per-GPU trends with process start/stop timelines.
- Added coalesced, rate-limited per-node probes and a guarded webhook delivery test from the dashboard.

### Changed

- Established a governed documentation portal and complete ADR index, moved the
  Simplified Chinese onboarding README under `docs/locales/zh-CN/`, kept stable
  reference URLs intact, and classified local agent/solver files as non-project
  workspace state.
- Replaced the authenticated dashboard's native `EventSource` connection with
  incremental fetch-based SSE parsing so every stream carries the explicit Bearer
  header; the named heartbeat and reconnect/fallback behavior remain unchanged.
- Accepted any non-empty subset of the collector settings on `POST /api/settings/collector`, exposed the read-only `connectTimeoutSeconds` floor in the inventory's collector settings, and deprecated `POST /api/settings/poll-interval` and `GET /api/service` (their responses now carry `Deprecation: true`).
- Restricted the workload owners view to online nodes; a footnote reports how many offline nodes were excluded and when the shown data was captured.
- Marked every dashboard read with the viewer header so any open dashboard view — not only the event stream — keeps probes on the attended process cadence.
- Capped workload identity collection at the first 512 distinct GPU process PIDs per sample and merged the per-PID `/proc` reads into one awk, cutting external processes per PID from seven to three in `identity` mode and from eight to six in `auto`.
- Folded scheduler batch completion into the final host result of that batch, so a completed batch publishes one state revision instead of two.
- Served HTTP snapshot reads from a shared read-only state projection instead of deep-copying the full state per request.
- Named the SSE keepalive frame (`event: heartbeat`) so EventSource clients can observe stream liveness instead of relying on unobservable comment frames.
- Stretched the process-telemetry cadence on devices that keep sampling zero processes (doubling per idle sample, capped at four times the base interval); activity in the five-second core telemetry cancels the stretch, so job pickup latency never exceeds the base interval while idle hosts run about one fifth fewer steady-state NVIDIA commands (13 instead of 16 per minute at the default 5-second core and 15-second process cadences).
- Stretched the process cadence of every device to sixteen times the base interval while no dashboard is open (no event stream and no marked read for 30 seconds); the first returning viewer forces a catch-up sample on the next core cycle, and core telemetry, trends, and incidents keep their cadence. An unwatched busy host drops from 16 to about 12.25 NVIDIA commands per minute.
- Enforced a bounded SSH keepalive (`ServerAliveInterval max(2, connect_timeout / 2)`, `ServerAliveCountMax 2`) on every remote probe, so a transport that dies mid-session is detected in seconds instead of consuming the whole probe timeout; measured dead-transport detection fell from the 30-second production probe timeout to 8.9 seconds (70%).
- Distinguished collection timeouts that produced no transport output from timeouts that stalled after partial output, and classified keepalive-detected dead transports with a dedicated redacted failure reason.
- Merged the remote hostname read into the fixed system `awk` pass with a bounded in-process `getline`, reducing default remote helper invocations from six to five; measured controllable local overhead is 0.79 ms of a 31.43 ms sample, so remaining collection cost is NVIDIA query wall time.
- Replaced the bounded quarter-second cancellation poll in probe process supervision with a wake-up descriptor registered in the probe selector, so shutdown interrupts waits immediately without idle wake-ups.
- Moved the fixed remote collection script and its rendering into a dedicated `remote_script` module, keeping probe parsing and process supervision separable from script content.
- Refined all six visual styles around a shared rounded-corner language while preserving distinct density, typography, layout, and material systems.
- Made English the default README and added a synchronized Simplified Chinese guide.
- Added a first-run path from OpenSSH aliases to an explicit Mocop cluster allowlist.
- Consolidated engineering documentation, community policy, and examples into dedicated directories.
- Removed the duplicate static systemd unit; `mocop service install` remains the tested service path.
- Adopted Forge commit subjects with repository-owned hook and CI enforcement.
- Removed the heatmap legend and reduced redundant SSE snapshot publication at poll start.
- Made backend incident conditions authoritative for both the attention queue and transition history.
- Limited generated user-service hardening claims to directives enforceable across the
  supported user-manager baseline: private state/modes, `NoNewPrivileges`, restricted
  address families, and `UMask=0077`. Filesystem access relies on validated operator
  ownership/modes so required SSH agent and ControlMaster paths remain available.
- Routed the header cadence control through validated atomic configuration persistence while preserving immediate scheduler updates.
- Refined the system and monospace font stacks and applied tabular typography consistently to live metrics.
- Combined base GPU and hardware-health fields into one `nvidia-smi` query with a base-metrics fallback, reducing measured local collection median by 27.5%.
- Added bounded deterministic per-host jitter to failure backoff so shared SSH paths do not trigger synchronized reconnect bursts.
- Replaced the fleet collection barrier with independently paced host deadlines on one bounded persistent worker pool.
- Added a private user-service state directory and optional private environment file for storage and webhook secrets.
- Reduced default remote helper invocations from 14 to 6 and made active probe processes cancellable during shutdown.
- Reused one snapshot projection and immutable JSON serialization across concurrent SSE readers of the same state revision.
- Replaced dictionary-heavy host/GPU trend storage with compact immutable records and skipped persistence serialization entirely when persistence is disabled.
- Packed retained trend values, removed idle-process container churn, parsed combined GPU telemetry once, and reused incident decoration maps per snapshot.
- Disabled Nagle buffering for HTTP responses so reused browser connections do not incur delayed-ACK stalls between headers and JSON payloads.
- Added one total-deadline-preserving retry for stale OpenSSH multiplex transports without retrying hard connection or trust failures.
- Indexed immutable host policy lookups, batched GPU-history writes, and bounded due-host selection to reduce configuration, persistence, and large-cluster scheduler overhead.
- Removed repeated numeric normalization from optional GPU telemetry parsing.
- Kept core GPU telemetry on the host cadence while moving compute-process telemetry to a validated 15-second default cadence, reducing steady-state NVIDIA commands by one third without inventing process transitions during skipped samples.

### Fixed

- Split workload records on ASCII newlines only, so a Unicode line separator inside a command line or environment-derived field can no longer break record framing and drop the workload overlay.
- Passed the captured command line and cgroup data to the per-PID awk through the environment instead of `-v` assignments, preserving backslashes verbatim.
- Joined the `/proc/PID/stat` content into one logical record before extracting fields, so a process name containing newlines can no longer shift the start-time column.
- Treated a reused PID whose workload start time changed as a new process instance: the service emits a stop/start event pair and restarts the first-seen timestamp instead of inheriting the old one.
- Showed an explicit retry affordance when the GPU dialog history request or the incident panel load fails, instead of failing silently.
- Excluded acknowledged conditions from topology correlation, matching the actionable definition used by counts and notifications.
- Kept the dashboard's snapshot-poll fallback carrying the viewer marker, so a browser in degraded SSE mode is not mistaken for an unattended system.
- Stopped reporting a healthy collection state when zero nodes are configured; the dashboard shows a guided empty state pointing at inventory setup instead.
- Collected the container root filesystem again: the disk filter dropped every `overlay` mount, so a probe running inside a container reported no root partition at all and could not warn about it filling up. An overlay mounted at `/` is now reported, while overlay mounts elsewhere stay excluded so a Docker host does not inherit its containers' layers.
- Dropped the single-file bind mounts that container runtimes inject (`/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf`); they report the host's filesystem under a file path, which added phantom disks and diluted the host disk total, in one monitored container reporting 65% instead of its real 99%.
- Enforced the persistence size limit on the runtime writer connection (not only at startup), let retention pruning run during idle periods and after disk-full write failures, made schema creation and migration atomic and idempotent, and actually persisted the `transportRetried` history flag across restarts (schema v3).
- Stopped an active recurring maintenance window from invalidating the dashboard inventory response, rejected full-week recurrences that would silence alerts permanently, and serialized maintenance state from one clock sample per snapshot so an instance boundary cannot mix the active decision with the next instance's end time.
- Froze incident recovery while the corresponding telemetry domain is missing instead of counting blind samples toward resolution, emitted `opened` for immediate conditions on the first online sample, debounced warning/critical flapping, and delivered severity changes whose `opened` was suppressed instead of silently dropping them.
- Bounded every webhook delivery attempt with one monotonic deadline covering DNS, connect, TLS, send, and body read; failed over across all validated addresses; and re-checked maintenance and silence state before each attempt, including retries.
- Made the stale-multiplex retry open a fresh SSH connection (`ControlMaster=no`) instead of re-binding the dead control socket, stopped retrying hard authentication/host-key/refusal failures that mention mux markers, classified partial-output stalls as remote errors rather than connectivity loss, and survived EPIPE from fast-exiting SSH clients.
- Kept new-job discovery within the base process cadence: unknown GPU utilization or memory now counts as activity, an activity hint latches until the next successful process sample instead of being erased by a later idle sample, and a failed process query forces a retry on every core cycle until it succeeds.
- Hardened remote-payload parsing: Unicode line separators inside GPU or process names can no longer forge protocol records, a malformed process or workload row degrades only that view instead of discarding the whole host sample, and empty or duplicated GPU UUIDs can no longer misattribute processes across devices.
- Exported GPU metrics for online hosts whose system section is missing, omitted MiB samples that overflow when scaled to bytes instead of failing the whole OpenMetrics render, and declared the cumulative dropped-write/delivery series as counters.
- Rebuilt `doctor --profile` around one instrumented remote call with mutually exclusive transport/script/NVIDIA stages that sum to the total, stopped reporting non-255 remote exits as SSH failures, warmed up the control master before timing reuse latency, aligned doctor's SSH options and per-host timeouts with the production probe, and bounded per-host diagnosis time with concurrent execution.
- Located the installed package through the service interpreter (`importlib.util.find_spec`) and preferred the systemd realtime start timestamp, so stale-service detection works for user installs, dist-packages, and editable layouts and survives NTP clock steps.
- Backed off failed dashboard incident polling instead of re-requesting in a loop, kept existing trends when a history request fails, treated silenced or not-yet-loaded hardware alerts as blocking for capacity matching, deduplicated multi-GPU processes and reported unknown VRAM honestly in the owners view, broke trend lines at data gaps on a true time axis, and refreshed fleet rows when a display name changes.
- Recorded unknown GPU memory in history as missing rather than 0%, bounded per-host GPU identity growth with reclamation of stale telemetry, closed a scheduler shutdown race that could delay stop by one wait cycle, and stopped submitting probes from a configuration snapshot that a live update had already replaced.
- Isolated the commit-policy repository test from operator-level git configuration such as a global `core.hooksPath`, which made the suite fail on machines with global commit hooks.
- Debounced transient EventSource failures and added snapshot fallback so a healthy dashboard no longer sticks on a reconnecting state.
- Stabilized incident activation and recovery so transient SSH or resource samples do not repeatedly open and resolve the same condition.
- Prevented failed inventory discovery from spinning the scheduler and made fatal collector exits restartable by systemd.
- Kept expected browser disconnects from emitting misleading HTTP server tracebacks while preserving unexpected handler errors.
- Removed orphaned incident overrides atomically when their node or final group is deleted from the dashboard.
- Prevented SSH or unavailable GPU-process telemetry gaps from generating false task start/stop transitions.
- Cleared removed-node manual-probe cooldowns, process/rate baselines, policy caches, and restored history so dynamic inventory changes do not retain stale state.
- Registered a node as in flight before starting its worker so a concurrent manual probe cannot be accepted and then lost during task submission.

### Removed

- Removed read compatibility for the V4 through V7 collection protocol payloads; the parser accepts only the current `MONITOR_V8`, because the fixed script and its parser ship in one process and no emitter of an older version can exist.
- Removed the legacy probe aliases and registry names (`GpuProbe`, `OpenSshNvidiaSmiProbe`, `openssh-nvidia-smi`, and `openssh-linux-v1` through `openssh-linux-v5`).
- Removed the probe and host-source registry indirection entirely; the entrypoint constructs the single OpenSSH collector and host source directly.
- Removed the unused `--config` option from `mocop service status` and `mocop service uninstall`, which operate on the fixed user unit.
- Removed dead resolved-option query keys from doctor's `ssh -G` inspection.
- Removed the browser-local migration of pre-release legacy theme values; current style and accent preferences are unaffected.

### Security

- Required exactly one valid Bearer capability on all non-public API routes and
  `/metrics`; only `/api/meta`, `/healthz`, and `/readyz` remain unauthenticated.
- Required a trusted `Host` header (loopback, the non-wildcard `listen_host`, or `trusted_web_hosts`) for dashboard writes and protected reads, closing a DNS-rebinding path; added connection and SSE concurrency caps with socket timeouts, rejected request bodies on bodyless methods, and answered `HEAD` without leaking handler tracebacks on malformed input.
- Derived GPU workload ownership from the real process UID resolved through the root-owned passwd database instead of attacker-controlled environment variables, and anchored Slurm/Kubernetes classification to real scheduler cgroup segments so `job_1.scope` or `podcast.service` are no longer misidentified.

## [0.8.0] - 2026-08-09

### Added

- Initial public release of Mocop.
- GPU/VRAM-first dashboard, scheduling heatmap and collapsed per-server inventory.
- CPU, memory, Swap, disk, network, trends, incident history and safe CSV export.
- Explicit config-based server allowlists with portable config discovery.
- A safe bundled default and a publication-safe example configuration.
- A configurable hard limit for combined SSH stdout and stderr.
- Safe `mocop init` bootstrap and explicit user-level systemd lifecycle commands.
- Python 3.10–3.14 CI, populated browser smoke coverage and security contracts.

### Fixed

- Existing failed-host retry deadlines are rebased when the runtime polling cadence changes.
- Runtime cadence changes remain authoritative during initial snapshot and SSE races.

### Security

- Oversized remote process output now terminates the SSH process group instead of growing an unbounded in-memory buffer.
- SSH targets, command arguments, output, timeouts and browser writes are validated and bounded.
