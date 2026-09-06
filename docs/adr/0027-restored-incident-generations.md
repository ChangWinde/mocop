# ADR-0027: Incident generations survive a service restart

## Status

Accepted

## Context

`IncidentTracker` is the sole authority for open conditions (ADR-0003). It
lived entirely in memory: after a restart, the first live sample of every host
re-evaluated the policy from scratch, and any condition that still held opened
again. With history persistence enabled, the previous generation's `opened`
was already in SQLite and had already been delivered to webhook receivers.

On a live deployment every restart therefore re-emitted `opened` for each
persisting condition — 17 per restart for 14 full filesystems and 3
unreachable hosts. Each duplicate reset `firstObservedAt`, produced a second
webhook delivery for the same real-world problem, and filled the bounded
transition ring with restart noise that displaced real transitions. Bound
acknowledgements, which are keyed to a generation, stopped applying because
the generation had changed.

## Driving factors

- One real-world problem must be one incident generation, across restarts.
- `firstObservedAt` must mean the first observation, not the last restart.
- Webhook receivers pair `resolved` with `opened` by condition; a restart
  must neither duplicate the `opened` nor orphan the eventual `resolved`.
- The first live sample must still be able to recover a condition that
  healed while the service was down, under the ordinary recovery rules.
- Persistence stays optional and bounded (ADR-0011): a deployment without a
  history database keeps working, and the restore reads the existing table.
- No second persistence format for tracker internals.

## Candidates

### Option A: Rebuild from live probes only

Keep the tracker in memory and accept a new generation after each restart.

Pros: no restore logic; the tracker has one construction path.

Cons: the observed duplicate `opened` per restart, the reset
`firstObservedAt`, doubled webhook deliveries, and acknowledgements that
silently stop applying. Without persistence this remains the only option, so
it stays as the fallback behaviour.

### Option B: Resume each condition from its latest persisted transition

At startup, read each condition's latest transition from the whole retained
`incident_events` table; a latest `opened`, `escalated`, or `deescalated`
means the condition was open with that severity. Seed the tracker with those
conditions, take `firstObservedAt` from the condition's last `opened`, and
let the first live sample confirm, recover, or freeze them like any later
sample. Hand the restored `(host, conditionKey)` pairs to the webhook workers
so their pairing state knows the `opened` was delivered by an earlier process.

Pros: no duplicate transition, `firstObservedAt` survives, acknowledgements
bound to the generation keep applying, the eventual `resolved` pairs and is
delivered, and the data source is the table persistence already writes.

Cons: the persisted transition carries no cycle counters, so a restored
condition's recovery follows the configured `recovery_cycles` rather than the
count it had reached before the stop; the restore adds one indexed query per
startup.

### Option C: Snapshot the tracker's in-memory state

Serialize candidates, recovery counters, and active conditions to a private
file on shutdown and load it on start.

Pros: exact continuity of every counter.

Cons: a second persistence format with its own validation, size, and
corruption story; a crash never writes the snapshot, which is exactly when
continuity matters; and the state duplicates what the transition table
already proves.

## Decision

Choose Option B. `persistence_restore.py` decides which conditions were open
from each condition's latest transition over the whole retained table — not
the bounded display window, because a disk that has been full for a week
must not reopen merely because `gpu_memory` churn pushed its `opened` out of
the last 500 events. It also reads each restored condition's last `opened`
to supply `firstObservedAt`. `IncidentTracker` accepts these `OpenIncident`
records, marks their hosts as initialized so the first sample is not treated
as a fresh baseline, and applies `recovery_cycles` from the policy. The
notification sink receives the restored keys as `known_conditions`, so an
unpaired `resolved` for one of them is delivered rather than suppressed.

Without persistence, active conditions are rebuilt from live probes (Option
A), and a durable generation-bound action gets one startup rebinding
opportunity for a matching condition; a healthy observation or a later
recovery consumes it, so the action cannot suppress a later recurrence.

## Impact

- A restart of a deployment with persistence emits no `opened` for
  conditions that still hold; the transition log and webhook stream record
  only real changes.
- `firstObservedAt` and bound acknowledgements are stable across restarts;
  the dashboard's "首次" age and the API's `firstObservedAt` mean the same
  thing before and after.
- Restored conditions recover under the configured `recovery_cycles`, which
  may be one or two samples later than an uninterrupted generation would.
- The restore path is one bounded query family over the existing schema;
  `persistence_schema.py` and the nine-column process contract are unchanged.
