# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

## [Unreleased]

### Added

- Added a centered per-GPU workload view with aggregate and per-process VRAM, PID and optional job context; parsing is bounded and the browser renders at most the 100 largest processes.
- Added an explicit `local_host` target that uses the fixed resource probe without an SSH connection.
- Added draggable server ordering, GPU/CPU activity in the fleet list, and browser-local display preferences.
- Added optional GPU ECC, memory-repair, hardware-slowdown, and MIG telemetry without making base collection depend on the health query.
- Added validated expected GPU counts, VRAM pressure, sustained idle-VRAM detection, and configurable incident stability windows.
- Added validated per-host pacing and timeout overrides for empirically slow GPU nodes.
- Added a dashboard SSH-alias inventory scan with Git/GitHub/GitLab filtering, constrained add/remove, private atomic persistence, and live scheduler updates.
- Added six structurally distinct browser-local visual styles and six independent accent palettes, including migration from the original theme choices.
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

- Debounced transient EventSource failures and added snapshot fallback so a healthy dashboard no longer sticks on a reconnecting state.
- Stabilized incident activation and recovery so transient SSH or resource samples do not repeatedly open and resolve the same condition.
- Prevented failed inventory discovery from spinning the scheduler and made fatal collector exits restartable by systemd.
- Kept expected browser disconnects from emitting misleading HTTP server tracebacks while preserving unexpected handler errors.
- Removed orphaned incident overrides atomically when their node or final group is deleted from the dashboard.
- Prevented SSH or unavailable GPU-process telemetry gaps from generating false task start/stop transitions.
- Cleared removed-node manual-probe cooldowns, process/rate baselines, policy caches, and restored history so dynamic inventory changes do not retain stale state.
- Registered a node as in flight before starting its worker so a concurrent manual probe cannot be accepted and then lost during task submission.

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
