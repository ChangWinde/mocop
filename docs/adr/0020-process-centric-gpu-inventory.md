# ADR-0020: Process-centric GPU inventory and task workspace

## Status

Accepted

## Context

The authenticated snapshot already carries the active compute-process records for
each GPU, and ADR-0018 makes those records searchable across the fleet. The main GPU
inventory still exposes only device-level metrics, however, while the detail dialog
puts trends before a tall list of process cards. An operator cannot scan the fleet for
occupied devices, compare attribution coverage, or move from one process to all of its
peers without opening devices one at a time.

The NVIDIA compute-apps query used by the collector supplies PID, process name, and
allocated framebuffer memory. It does not supply a trustworthy per-process share of
SM utilization. Adding another high-frequency command solely to approximate that
value would increase remote work and would be inconsistent across drivers, MIG, and
short-lived processes.

## Driving factors

- Make process occupancy visible in the main GPU table without opening a dialog.
- Put active work before historical context inside the GPU detail view.
- Support rapid comparison by memory, runtime, attribution, owner, and workload.
- Keep search, filtering, summaries, and quick actions bounded browser-side
  transforms of the authenticated snapshot.
- Preserve process-sampling freshness and availability semantics.
- Add no remote command, collection cadence change, or new authorization surface.
- Never present device utilization as if it were per-process utilization.

## Candidates

### Option A: Browser-side process projections and a dense task workspace

Pros: reuses the current snapshot and ADR-0018 search index, adds no collection or
network work, can expose the same freshness markers, and remains available when the
optional workload-identity tier is disabled.

Cons: process detail remains limited to monitor-observed fields, and every parsed
snapshot must derive a small summary for each visible GPU.

### Option B: Add a server-side process query and aggregation API

Pros: could return small, purpose-built payloads and centralize grouping.

Cons: duplicates data already shipped in `/api/snapshot`, adds route, cache, version,
and authorization contracts, and does not improve the underlying telemetry. It also
makes the dialog dependent on another request during degraded connectivity.

### Option C: Collect per-process utilization with an additional NVIDIA command

Pros: could provide a best-effort instantaneous process activity signal on some
drivers.

Cons: adds remote work on the hottest cadence, has driver/MIG support gaps, samples
short windows rather than job occupancy, and risks implying accuracy the source does
not guarantee.

## Decision

Choose Option A. One pure, weak-map-cached GPU-process summary is shared by the main
inventory and detail dialog. It derives only bounded display facts from the current
GPU object: process count, known process-memory total and coverage, top process,
owner/identity coverage, and oldest available start signal.

The main GPU table gains a process column showing availability, count, the largest
known process, and allocated process memory, plus process-count sorting and an
active-process inventory filter. The detail dialog becomes a process-first workspace
with attribution filters, compact summary metrics, deterministic sorting,
owner/workload drill-down, copy actions, and a transition to fleet-wide search. Its
existing 100-row cap remains after filtering; keyed rows continue to update in place.
Historical charts and transitions remain available below the active workspace. CSV
export includes aggregate process count, known allocation, coverage, and freshness,
but continues to omit process names, PIDs, owners, and commands.

Process memory is labelled as allocated VRAM. GPU utilization remains a device-level
metric and is never copied onto a process row. Unknown process memory, unavailable
queries, cached sample timestamps, and unattributed processes stay explicit.

## Impact

- The operator can identify active work from the main inventory and answer common
  ownership and placement questions without a new request.
- Workload identity improves the view but is not required; unattributed processes
  receive a first-class filter rather than disappearing.
- Quick actions operate only on text already present in the authenticated snapshot.
  They do not form selectors, URLs, commands, or server requests from remote values.
- Browser tests cover process summaries, ordering, identity filters, drill-down,
  copy feedback, fleet-search transition, keyed-node reuse, and mobile overflow.
- A future server-side process API requires new measurement showing that snapshot
  transfer or browser derivation—not SSH/NVIDIA collection—is the active bottleneck.
