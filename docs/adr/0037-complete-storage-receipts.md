# ADR-0037: Read complete storage measurements independently of scan deadlines

## Status

Accepted.

## Context

A full directory count on shared storage can take more than an hour. Repeated
short SSH scans terminate at the same unfinished tree, so a larger per-root
deadline alone does not establish eventual completeness. A separate maintenance
counter may already produce complete, serialized counts of the same roots.
Furthermore, `isdir()` hides disconnected FUSE errors as missing paths.

## Candidates

Increasing synchronous scan deadlines is simple but continues to duplicate the
existing counter, holds SSH workers for hours and loses work on disconnect.
A new resumable crawler adds queue, inode and concurrent-change state to own.
Reading the existing complete counter's receipt avoids a competing crawler and
allows polling, scan lifetime and measurement freshness to remain separate.

## Decision

Allow an administrative per-host `measurementFile` in the private storage policy.
The fixed remote program reads only that owner-private, bounded JSON document.
`storage_receipt.py` validates complete status, exact root byte counts and real
scan timestamps. It does not execute the file or invoke the producer. Hosts
without this option retain the bounded synchronous scanner.

Receipt-enabled hosts poll every minute and never launch `du`, even when a
receipt fails validation. Failure retains the last successful observation with
explicit stale status. Two receipt workers are separate from the two recursive
scan workers, so slow scans cannot hold up the receipt polling queue. Each host
has at most one queued or running collection.
The server and UI retain the actual measurement end time;
polling cannot renew it. Identical measurements replace the latest history entry
rather than consuming a sample per minute. Counts for different scopes or sources
are never mixed. The administrator owns the counter's exclusion policy and its
schedule; scheduling and locking the existing producer remain outside the reader.

Report ENOTCONN, ESTALE, EIO and ENODEV as `mount_unavailable`. An inaccessible
mount has unknown usage, never a complete zero or a missing-directory claim.

## Consequences

The main table can show complete local counts while a separate object mount is
unavailable. It shows completeness per directory and stale measurement age;
complete traversal is not an instantaneous snapshot during writes or archiving.
An external producer must publish atomically, exclude inappropriate user scopes
and run on an administrator-managed schedule. Its absence, failure or stalled
schedule remains visible. The monitor never starts cleanup, repairs mounts,
changes credentials, or invents totals by summing truncated top-child lists.
