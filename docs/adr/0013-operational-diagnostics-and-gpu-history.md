# ADR-0013: Operational diagnostics and per-GPU history

## Status

Accepted

## Context

Mocop already collects the current fleet state. Operators now need to acknowledge
or temporarily silence individual conditions, inspect a GPU's recent behavior,
request one bounded host probe, and export diagnostic context without increasing
remote collection cost or adding a required service.

## Driving Factors

- Preserve the zero-runtime-dependency and single-process deployment model.
- Keep the fixed read-only SSH probe as the only remote execution boundary.
- Never block collection on disk, webhook, or browser work.
- Bound memory, SQLite size, request bodies, retries, and manual-probe frequency.
- Keep configuration changes atomic and durable across service restarts.

## Candidates

### Option A: Extend the existing state and bounded adapters

- Keep current samples and action evaluation in `StateStore`.
- Persist operator actions through the existing atomic JSON controller.
- Add per-GPU samples and process transitions to the existing asynchronous SQLite
  writer through its `TelemetryPersistence` protocol.
- Add a narrow `ProbeControl` protocol for rate-limited manual probes.
- Pros: no new dependency, no second source of truth, collection remains
  non-blocking, deployment is unchanged.
- Cons: `StateStore` remains the central composition point and needs careful API
  boundaries.

### Option B: Add an operations database and background service

- Move incidents, actions, GPU history, probe requests, and diagnostics into a
  separate database-backed service.
- Pros: independent scaling and richer multi-user workflows.
- Cons: adds installation, authentication, migrations, IPC, failure modes, and
  resource cost that are not justified for the supported 10–200 host deployment.

## Decision

Chosen: Option A. Operator actions are validated configuration, current telemetry
remains in memory, and optional durable telemetry uses the existing bounded writer.
The manual-probe interface exposes one idempotent request operation; it cannot
accept commands or alter the fixed probe. Diagnostic output is an explicit
allowlist projection and never serializes raw SSH errors, process details, secrets,
or configuration values.

## Impact

- `config.py` and `inventory.py` own durable incident-action validation and writes.
- `service.py` owns action evaluation, GPU timelines, diagnostic projection, and
  manual-probe scheduling.
- `persistence.py` stores bounded per-GPU samples and process transitions without
  blocking collector threads.
- `web.py` exposes same-origin, size-bounded operational endpoints.
- Existing configuration remains valid; SQLite schema upgrades are automatic and
  additive.
