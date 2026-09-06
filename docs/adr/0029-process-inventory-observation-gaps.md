# ADR-0029: Observation-gap semantics for the GPU process inventory

## Status

Accepted

## Context

The process-centric inventory (ADR-0020) derives `started` and `stopped`
transitions by comparing consecutive process samples per device, and the
usage rollup behind `GET /api/usage` pairs those transitions into occupancy
runs. Both assumed that a sample is either present or absent; they had no
notion of a sample the monitor could not take.

Three failures of that assumption showed up on a live deployment behind an
SSH relay:

- A single failed probe closed the host's whole inventory: every process
  received a hidden `stopped`, was re-seeded a few seconds later with a fresh
  `first_seen_at` (the dashboard's observed runtime), and the retained
  timeline filled with these pairs. A relay hiccup looked like every job on
  the node restarting.
- A 24-hour usage report dropped 644 records, 592 of them orphan `stopped`
  events. The restore keeps the last 500 transitions per device, and devices
  that churn hundreds of processes a day had lost the `started` edge of runs
  whose `stopped` was still retained; without an anchor those runs were
  discarded rather than guessed, undercounting the owners who used the
  busiest cards most.
- `earliestDataAt` reported the earliest record across all devices, so a
  quiet device made a report look complete while a busy device's retained
  timeline reached back only an hour.

## Driving factors

- A failed probe is a blind spot, not evidence that processes ended.
- The definition of "stale" must be the one the rest of a host's data already
  uses (`collection_stale_cycles`), not a second threshold for processes.
- Occupancy is only ever counted over confirmed observation; a gap is never
  billed, and a guessed start is never used.
- The SQLite process table keeps its nine-column contract; a restore from an
  older database must keep working.
- The report has to say when its totals are incomplete, per device, because
  owner comparison is the point of the report.
- Process transitions still require two consecutive, actually sampled
  process observations (ADR-0014); this decision only defines what happens
  around a gap.

## Candidates

### Option A: Close the inventory on every failed probe

The previous behaviour: treat an absent sample as "no processes".

Pros: no state survives a failure; the timeline is always derived from the
last two samples.

Cons: the observed flood of `stopped`/`started` pairs, reset first-seen
stamps, and a usage rollup that counted the re-seeded runs as new occupancy.

### Option B: Keep the inventory as a blind spot until the host is stale, and make stops self-anchored

Keep a failing host's process table and first-seen stamps until the host has
failed for `collection_stale_cycles` consecutive cycles, then close the
confirmed occupancy at its last successful sample. Carry the monitor's first
observation of each process on each device (`firstSeenAt`) on every
transition, so an orphan `stopped` is a complete run by itself. Report the
devices whose retained timeline is full yet begins inside the window.

Pros: a short gap changes nothing; a real process change across a short gap
is attributed to the first sample after it; a long outage closes runs at the
last time they were actually seen; truncated timelines stop dropping runs
without inventing a start; the schema is unchanged because `firstSeenAt`
rides inside the existing `workload_json` column.

Cons: the process table of a failing host is shown as its last confirmed
state for up to `collection_stale_cycles` cycles; a stop whose
`firstSeenAt` predates this monitor's first observation still anchors on the
observation, so a run that began before the monitor did is counted from the
monitor's start.

### Option C: Persist explicit occupancy runs

Write a separate start/stop pairs table alongside transitions.

Pros: a run is one row; no pairing at read time.

Cons: a second write path and table with its own retention; a run open at
shutdown still needs the same anchoring rules; and the transition-window
truncation problem moves rather than disappears, because bounded retention
would still cut the run table.

## Decision

Choose Option B. In `service.py`, a failed probe leaves the host's process
inventory and first-seen stamps in place; only when
`state.consecutive_failures + 1 >= collection_stale_cycles` (default 3,
range 2–12) is the inventory invalidated and each confirmed process closed
with a `stopped` observed at the last successful process sample. These
closing transitions are bookkeeping for the rollup and stay out of the
dashboard's event list, because nothing was observed to stop. A GPU that
disappears from an online host (an XID fault or a bus drop) closes its
processes the same way. The usage rollup receives, for every host that is
not online, the last confirmed process observation per device
(`observed_until`), and its live processes occupy their devices only up to
that point.

Every transition carries `firstSeenAt`, this monitor's first observation of
the process on the device. `occupancy.py` anchors an orphan `stopped` on it
and a live process without a retained `started` on its `first_seen_at`;
anything else is counted in `droppedRecords` rather than guessed. A process
start time is never used as an anchor because it is not a GPU-occupancy
observation. `usage.py` reports `partialGpus`: devices whose retained
timeline holds the cap of transitions yet begins after the window start, so
their occupancy before that point is missing from the totals;
`earliestDataAt` continues to describe the earliest record overall.

The dashboard reflects the same rules: a finished run's timeline entry states
how long it held the device from `firstSeenAt`, the owners view excludes
hosts that are not online from current attribution and says so, and the usage
summary names the `partialGpus` count.

## Impact

- A transient probe failure produces no process transitions and preserves
  observed runtimes; the timeline records only process changes.
- Occupancy is never billed across a gap: a failing host's processes count
  up to the last confirmed sample, and a long outage closes them there.
- Orphan stops become complete runs; on the deployment above the dropped
  count falls from 644 toward the records that genuinely lack an anchor.
- Agents reading `/api/usage` must read `partialGpus` and `droppedRecords`
  before comparing owners; the API reference's usage playbook says how.
- The SQLite process table's contract is unchanged; databases written before
  this decision restore with `firstSeenAt` absent, which the pairing treats
  as no anchor.
