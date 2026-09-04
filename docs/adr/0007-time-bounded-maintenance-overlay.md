# ADR-0007: Time-bounded maintenance as an incident overlay

## Status

Accepted

## Context

Planned driver, kernel, power, or hardware work can make a healthy monitoring system report expected failures. Operators need to remove those failures from the immediate attention queue without disabling collection, erasing incident truth, or leaving an indefinite forgotten silence. The dashboard already has a constrained, atomic configuration controller, but it must not become a general alert editor.

## Driving factors

- Preserve continuous telemetry, incident activation, recovery, and transition history.
- Distinguish raw active failures from work that is currently actionable.
- Make every dashboard-created silence finite, visible, attributable, and reversible.
- Persist through restart and hot-apply without another scheduler or remote operation.
- Keep browser authority limited to explicitly configured hosts and fixed durations.

## Candidates

### Option A: Stop probing a host during maintenance

Pros: removes expected failures and reduces collection work.

Cons: creates a monitoring blind spot, loses recovery evidence, and cannot reveal unrelated failures during the window.

### Option B: Drop or resolve incidents when maintenance starts

Pros: keeps collection running and simplifies the attention count.

Cons: corrupts incident history, fabricates recovery, and requires conditions to be rediscovered after maintenance.

### Option C: Overlay a finite silence on authoritative incidents

Pros: preserves raw state and transition history, supports separate actionable counts, hot-applies through existing configuration synchronization, and expires without a write.

Cons: consumers must understand the difference between active and actionable incidents, and expiry must invalidate the incident view.

## Decision

Choose Option C. `maintenance_windows` maps an explicit host alias to a strict UTC expiry and a bounded reason. The dashboard can set only 1 hour, 4 hours, 24 hours, or 7 days, or clear an existing window. The service generates the expiry from its own clock. It continues normal collection and incident tracking, adds maintenance metadata to the server snapshot, marks incident views as silenced, and publishes raw plus actionable counts.

The active maintenance signature participates in the incident-view revision. Starting, changing, clearing, or naturally expiring a window therefore causes consumers to refresh the overlay even when no underlying condition transitions. The canonical configuration remains the durable source; expired records are inert and may be replaced or removed by the next operator change.

> **Update:** configuration-authored windows may alternatively carry a weekly `recurrence` (`{weekday, start, duration_minutes}`, UTC); the dashboard still creates only fixed-duration windows, and every window is delivered with an `active` flag so planned recurrences stay visible outside their live period.

## Impact

- A planned outage remains visible as a real connectivity or resource condition but leaves the attention queue until the window ends.
- Recovery during maintenance is still recorded normally.
- Dashboard writes accept one exact host, fixed duration, and short visible reason; the host must still be explicitly configured.
- No new collector process, timer thread, database, dependency, or remote command is introduced.
- External notification delivery, if added later, must consume the actionable view at dispatch time rather than mutate incident truth.
