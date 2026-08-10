# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

## [Unreleased]

### Added

- Added per-GPU CUDA compute-task details with bounded process parsing and per-process VRAM.
- Added an explicit `local_host` target that uses the fixed resource probe without an SSH connection.
- Added draggable server ordering, GPU/CPU activity in the fleet list, and browser-local display preferences.
- Added optional GPU ECC, memory-repair, hardware-slowdown, and MIG telemetry without making base collection depend on the health query.
- Added validated expected GPU counts, VRAM pressure, sustained idle-VRAM detection, and configurable incident stability windows.
- Added validated per-host pacing and timeout overrides for empirically slow GPU nodes.
- Added a dashboard SSH-alias inventory scan with Git/GitHub/GitLab filtering, constrained add/remove, private atomic persistence, and live scheduler updates.
- Added five browser-local interface themes with refined system-font typography and clearer settings language.
- Added a centered, responsive settings workspace with browser-local density and fleet-focus preferences.
- Added durable dashboard controls for collection cadence, complete-probe timeout, and worker concurrency.
- Added a validated, browser-local custom background with visibility control.
- Added persistent, time-bounded maintenance windows with continuous collection, automatic expiry, and separate raw/actionable incident counts.
- Added a snapshot-only GPU capacity matcher with same-node/model constraints, free-VRAM requests, health exclusions, and ranked near matches.

### Changed

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

### Fixed

- Debounced transient EventSource failures and added snapshot fallback so a healthy dashboard no longer sticks on a reconnecting state.
- Stabilized incident activation and recovery so transient SSH or resource samples do not repeatedly open and resolve the same condition.

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
