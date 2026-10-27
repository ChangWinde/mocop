# ADR-0031: Add compatible persistence metadata

Status: Accepted

Date: 2026-10-27

## Context

Restoring only the public fields of an incident transition loses the numeric
critical line required by hysteresis. GPU rollups also need each retained raw
sample's original busy contribution to replace repeated timestamps correctly.
Retention needs a durable cutoff because a clock rollback must not admit an
expired sample into a rollup again.

Released v3 writers use positional inserts. Adding columns to their tables would
break package rollback. A legacy database can already occupy its configured cap.

## Decision

Keep `user_version = 3` and the released physical table shapes. Add companion
tables for incident critical lines, retained GPU busy contributions, expired
memory maxima, and the furthest raw-retention cutoff. Dispatch their creation
from `persistence.py` with DDL owned by `persistence_schema.py`. Commit all
companion creation and legacy memory baselines in one transaction under the cap.

If space is insufficient, restore existing history and report unavailable rollups.
Reject incident and GPU writes that require missing metadata and count them as
lost. Retry metadata creation on the next restart after retention frees space.

Preserve existing aggregate totals on upgrade rather than attempting to repair
older duplicate samples. A repeated legacy raw row keeps its aggregate contribution
unchanged because its original busy threshold cannot be recovered. New writer
contributions replace normally, including in a legacy hour.
Preserve legacy aggregate memory maxima as a floor:
expired raw samples no longer identify which sample supplied that maximum.
New writes keep replaceable contributions separate from expired memory maxima.

Extend [ADR-0027](0027-restored-incident-generations.md) by preserving each open
generation's opening and latest transitions during retention. Restore a legacy
critical line from its condition's policy formula; reset recovery counters as
before. [Operations](../OPERATIONS.md#history-continuity-and-durability) owns the
fallback values and flush acknowledgement contract.

## Alternatives

- Appending columns breaks the released positional insert contracts.
- Rebuilding every hourly aggregate from raw history loses expired samples.
- Raising the configured cap for migration violates the operator's size bound.
- Persisting the whole writer queue would add a second durability mechanism;
  the asynchronous queue instead reports losses while the process is running.

## Impact

Record serialization, incident retention, GPU replacement, and row validation
become separate Python leaves under the existing incremental extraction rule.
The writer keeps queue admission and loss accounting together in `persistence.py`.
The main writer, restore, and schema line ceilings decrease.

A package rollback can read the released tables, but an older writer does not
maintain the new metadata or provide these continuity guarantees. Follow the
pre-upgrade backup procedure before a downgrade.
