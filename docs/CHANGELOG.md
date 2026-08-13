# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

## [Unreleased]

### Added

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
- Expanded the user-service write sandbox only to the selected Mocop configuration directory so atomic dashboard inventory updates work under `ProtectSystem=strict`.
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

- Removed read compatibility for the V4 through V6 collection protocol payloads; the parser accepts only the current `MONITOR_V7`, because the fixed script and its parser ship in one process and no emitter of an older version can exist.
- Removed the legacy probe aliases and registry names (`GpuProbe`, `OpenSshNvidiaSmiProbe`, `openssh-nvidia-smi`, and `openssh-linux-v1` through `openssh-linux-v5`).
- Removed the probe and host-source registry indirection entirely; the entrypoint constructs the single OpenSSH collector and host source directly.
- Removed the unused `--config` option from `mocop service status` and `mocop service uninstall`, which operate on the fixed user unit.
- Removed dead resolved-option query keys from doctor's `ssh -G` inspection.
- Removed the browser-local migration of pre-release legacy theme values; current style and accent preferences are unaffected.

### Security

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
