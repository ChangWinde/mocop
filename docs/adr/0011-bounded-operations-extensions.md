# ADR-0011: Bounded operational extensions

## Status

Accepted

## Context

Operators need restart-safe trends, shared-path failure context, GPU process ownership,
and outbound incident delivery. These features cross storage, remote-process privacy,
and outbound-network trust boundaries. They must remain optional and must not turn
Mocop into a scheduler or let storage and notification latency enter the collection
path.

## Candidates

### Option A: Require Prometheus, Alertmanager, Slurm APIs, and Kubernetes APIs

This provides mature durable history and rich scheduler metadata, but adds several
services, credentials, network endpoints, and sources of truth to an otherwise
agentless local tool.

### Option B: Add one embedded operational control plane

A shared database and privileged scheduler clients could correlate, query, notify, and
eventually mutate jobs. This is powerful but couples unrelated failure domains and
expands Mocop beyond read-only resource monitoring.

### Option C: Add narrow optional adapters behind typed protocols

SQLite receives bounded asynchronous history writes; topology correlation transforms
only active incident projections; workload identity reads bounded `/proc` metadata;
and each webhook target owns an independent bounded delivery worker.

## Decision

Choose Option C.

- Persistence is disabled by default. When enabled, standard-library SQLite stores
  successful trend points and incident transitions under the user state directory.
  Retention and database size are validated, writes use a bounded non-blocking queue,
  and startup restores only bounded views. Current telemetry remains in memory.
- Correlation groups actionable connectivity incidents under the deepest configured
  non-root shared path. It reports `confidence: possible`; it never resolves, hides, or
  rewrites the authoritative per-host incidents.
- Workload mode is disabled by default. `auto` reads bounded cgroup, status, and selected
  environment metadata for PIDs already returned by `nvidia-smi`. It recognizes Slurm
  and Kubernetes identity but never invokes `scontrol`, `kubectl`, or a write API.
- Webhook URLs and optional HMAC secrets come from environment variables, not JSON.
  Targets require HTTPS, reject non-public addresses unless explicitly allowed, and
  are DNS-validated again before a request is pinned to that address. Each endpoint has
  bounded queueing, event deduplication, throttling, and finite jittered retries.
  Maintenance-silenced transitions are not delivered.

The storage and notification implementations conform to `TelemetryPersistence` and
`IncidentNotificationSink`; disabled implementations preserve the same call sites.

## Impact

- The default installation keeps zero third-party runtime dependencies and no durable
  telemetry.
- Explicitly enabled SQLite adds local telemetry at rest and therefore inherits the
  dashboard's confidentiality requirements.
- Workload owner, job, and namespace metadata becomes visible to dashboard readers only
  when the operator opts in.
- Webhook delivery is asynchronous and best effort within the configured retry budget;
  its status is visible in snapshots, but delivery is not a persistent outbox.
- Topology remains operator-authored context, not proof of network root cause.
