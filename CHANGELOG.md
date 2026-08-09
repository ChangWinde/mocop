# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

## [Unreleased]

## [0.8.0] - 2026-08-09

### Added

- Initial public release of Mocop.
- GPU/VRAM-first dashboard, scheduling heatmap and collapsed per-server inventory.
- CPU, memory, Swap, disk, network, trends, incident history and safe CSV export.
- Explicit config-based server allowlists with portable config discovery.
- A safe bundled default and a publication-safe example configuration.
- A configurable hard limit for combined SSH stdout and stderr.
- Safe `mocop init` bootstrap and explicit user-level systemd lifecycle commands.
- Python 3.10–3.13 CI, populated browser smoke coverage and security contracts.

### Fixed

- Existing failed-host retry deadlines are rebased when the runtime polling cadence changes.
- Runtime cadence changes remain authoritative during initial snapshot and SSE races.

### Security

- Oversized remote process output now terminates the SSH process group instead of growing an unbounded in-memory buffer.
- SSH targets, command arguments, output, timeouts and browser writes are validated and bounded.
