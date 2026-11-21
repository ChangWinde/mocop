# ADR-0034: Transition ids do not repeat across restarts

## Status

Accepted

## Context

Webhook receivers deduplicate deliveries on `eventId`, as
[API.md](../API.md#webhook-deliveries) recommends: a retry resends the same id,
so a receiver that recorded it drops the duplicate. `IncidentTracker` numbered
transitions from the largest restored id + 1. Without history persistence, which
is the default, nothing is restored, so every restart numbered from 1 again.
With persistence, the same happened to every id whose transition was delivered
but never committed: the writer queue was full, the database was at its size
cap, or the process was killed before the next flush.

A receiver that kept the ids of the previous run therefore treated the new
run's first alerts as duplicates and dropped them, until the new run passed
the previous run's largest id. Mocop restarts on self-update, on a dashboard
restart request, and on every crash under the user service.

## Driving factors

- An id a receiver has seen must never name a different transition.
- Receivers must not change: the id stays one integer, and it keeps increasing.
- Without persistence there is no durable place to store a counter, and adding
  one would write to disk on every transition.

## Candidates

### Option A: Persist a counter file even without history persistence

Exact, but it adds a second durable state file, a write per transition, and its
own corruption and permission handling to a mode whose point is to write nothing.

### Option B: Add a per-start instance id to the payload

Exact, but receivers would have to key on two fields. Every existing receiver
that follows the documented contract would keep dropping alerts until changed.

### Option C: Start numbering at Unix milliseconds

A starting tracker numbers from the larger of the largest restored id + 1 and
the Unix time in milliseconds at startup. One integer field, still increasing,
no new state. It fails only if a run assigned more ids than milliseconds passed
before the next start, or the wall clock moved backwards between two starts.

## Decision

Option C. `IncidentTracker` receives the store's start time and takes the
floor from `incident_rules.event_id_floor`. Ids stay below 2^53, so browsers
and JSON receivers read them exactly.

## Impact

- The first id after upgrading jumps from a small number to about 1.8 × 10^12
  and keeps increasing from there; receivers that compare ids for order are
  unaffected.
- `incidentVersion` is unchanged; it counts revisions, not transitions.
- A wall clock stepped backwards between two starts can still produce a
  repeated id; NTP-synchronized hosts avoid that.
