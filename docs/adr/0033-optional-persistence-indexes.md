# ADR-0033: Keep persistence indexes independent of metadata availability

Status: Accepted

Date: 2026-10-27

## Context

The incident criterion index from [ADR-0032](0032-incident-criteria-and-expiry-order.md)
can fail to fit an existing database's size cap. Treating that failure as missing
metadata disables existing GPU memory archives. Retention can then discard the
only evidence of an hourly maximum before a later replacement recomputes it.

## Decision

Create required companion tables and initialize legacy metadata in one
transaction. Create each companion index separately afterward. An index failure
logs a warning and leaves existing tables available; the next startup retries
the missing index. Neither detection rules nor the HTTP API changes.

Determine companion availability from the tables that exist after rollback,
rather than from whether every migration statement succeeded. Legacy retention
uses an existing memory archive even if another companion table is missing.

Capture expired GPU memory maxima before deleting raw samples. Commit the
archive, raw deletion, and retention cutoff together. Deleting rows before
inserting the archive allows their pages to be reused at the size cap; an
archive failure rolls back the entire expiry unit.

## Alternatives

- Grouping indexes with required metadata makes an optional index failure
  disable valid tables and can lose expired maxima.
- Skipping all retention while any companion is missing wastes space even
  when the memory archive is available.
- Inserting archive rows before freeing expired pages can fail at the cap
  despite enough reclaimable space in the same transaction.

## Impact

The released v3 table shapes remain unchanged. A missing index can make the
first lookup of a restored criterion slower until a later startup creates it.
Unchanged cached criteria still skip SQL. Existing companion tables continue
to support GPU replacement and retention without the optional indexes.
