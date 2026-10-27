# ADR-0032: Preserve incident criteria and expiry order

Status: Accepted

Date: 2026-10-27

## Context

A disk can remain critical while its critical criterion changes from absolute
free-space headroom to percentage usage. Public incident transitions alone do
not capture that change. A clock rollback can also put a resolved transition
outside the retention window while its earlier escalation remains inside it.
Legacy GPU expiry needs its durable cutoff before raw rows are deleted.

## Decision

Extend [ADR-0031](0031-compatible-persistence-metadata.md) with a confirmed-state
projection after each tracker update, under the existing StateStore lock.
Forward transitions and current criteria as one immutable queue item and commit
them together. Read the tracker's confirmed conditions rather than deriving a
new condition from telemetry; pending severity changes retain their confirmed
criterion. Keep the last committed criterion per open condition in the writer;
skip unchanged updates in memory and publish cache changes only after the batch
commits. Resolutions remove their cache entries. Failed batches leave the prior
cache intact, so the next sample retries the update. Hydrate restored criteria
on first use through a host/condition index installed by companion migration.
Neither detection rules nor HTTP or notification events change.

Use `event_id` as the transition sequence. Retain every condition's latest
transition, resolved included, and the opening of an open generation. Restore
continues to choose the maximum sequence, rather than the latest timestamp.

Create and initialize a legacy GPU retention watermark before deleting expired
raw rows in the same transaction. If its retained pages cannot fit the cap,
roll back that expiry unit and retain the raw rows. Later companion creation
initializes the watermark and expires raw rows atomically before writes resume.

## Alternatives

- Inferring a critical criterion from a percentage loses absolute headroom.
- Emitting synthetic public transitions changes the incident and webhook API.
- Coalescing only inside SQLite still searches retained history on every sample.
- Updating the cache before commit can suppress a retry after a failed write.
- Using timestamps for transition order can resurrect incidents after a clock rollback.
- Deleting raw GPU rows before persisting the cutoff loses the evidence needed
  to reject a replay after migration and a clock rollback.

## Impact

StateStore's forwarding change is line-neutral at its existing 2,150-line ceiling.
Queue payloads and SQLite space reclamation become separate leaves, lowering the
writer and serialization line ceilings. Schema migration still dispatches from
`persistence.py`; companion DDL remains in `persistence_schema.py`.
Resolved markers consume one retained transition per closed condition until a
later generation replaces that marker. The configured size cap remains enforced.
