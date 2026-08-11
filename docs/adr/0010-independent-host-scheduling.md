# ADR-0010: Independent host scheduling

## Status

Accepted

## Context

The original collector submitted one fleet batch and waited for every future before
starting the next batch. Results were published as they completed, but one probe that
used its full timeout still delayed every healthy host's next submission. Per-host
cadence and backoff therefore were not truly independent.

## Candidates

### Option A: Keep the fleet barrier and increase worker concurrency

This is small, but more workers do not remove the barrier. A single slow future still
delays the next fleet cycle and repeated executor creation adds avoidable churn.

### Option B: Use one bounded pool with a scheduler-owned deadline per host

This preserves the synchronous `--once` path while the service runtime tracks one
in-flight probe and one next deadline per host. Completion wakes the scheduler, updates
state immediately, and resubmits only that host when its cadence or retry deadline is
due. The current `max_workers` value limits active futures.

### Option C: Rewrite collection around `asyncio`

This could express deadlines directly, but OpenSSH and the local probe are bounded
subprocesses already. A rewrite would add migration and cancellation risk without
removing the remote round trip.

## Decision

Choose Option B. `MonitorService.run` owns the mutable schedule and a persistent pool;
probe workers own no scheduler state. A host cannot overlap itself. Due hosts are
ordered by the oldest deadline so a continuously slow first alias cannot starve later
aliases. Failure deadlines retain exponential backoff and deterministic jitter.

Configuration and inventory changes wake the scheduler. Healthy cadence deadlines are
rebased from the previous probe start; failure deadlines are rebased from the previous
failure. Discovery failures have their own bounded retry deadline. A fatal scheduler
failure ends the collector thread, and the main process exits non-zero so systemd can
restart it. `poll_once` remains a finite fleet barrier by design.

## Impact

- A slow host no longer changes a healthy peer's next cadence when worker capacity is
  available.
- `max_workers` remains the explicit resource bound; setting it below active demand can
  still queue due hosts.
- The concurrency contract is covered with synchronized fault injection rather than a
  timing-only assertion.
- No runtime dependency or remote agent is added.
