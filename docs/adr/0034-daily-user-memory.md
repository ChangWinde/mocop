# ADR-0034: Daily user RAM snapshots

## Status
Accepted

## Context
Operators need a separate per-server, per-user RAM view refreshed daily,
including CPU-only processes absent from GPU workload accounting.

## Candidates

| Approach | Benefit | Cost |
|---|---|---|
| Extend the fast GPU probe | Shares one connection and snapshot | Expands the frequent protocol and scheduler for a daily need |
| Separate daily worker with a read-only cache | Isolates latency and cadence, includes all visible processes | One extra SSH sample per host per day |

## Decision
Use the separate worker, with the existing bounded process runner and strict
SSH trust policy. A `MemorySource` protocol separates HTTP from the worker.
The fixed versioned command remains in `remote_script.py`. Group by UID,
report RSS and its shared-page limitation, and preserve old samples on failure.
The requested 24-hour interval is a fixed product contract; existing configuration
provides SSH paths and connection/probe deadlines. Do not add a plugin registry.

## Impact
The entrypoint starts and stops the worker; the manifest adds one authenticated
GET; a browser leaf renders the dialog. Memory-only caching avoids retaining
user telemetry on disk, at the cost of re-sampling on restart. A failed host
waits until its next daily attempt or a service restart.
