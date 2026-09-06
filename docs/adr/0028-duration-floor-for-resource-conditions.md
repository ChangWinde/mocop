# ADR-0028: Wall-clock duration floor for resource conditions

## Status

Accepted

## Context

Resource conditions — CPU, memory, swap, filesystem, pressure, GPU memory,
temperature, idle VRAM, hardware health — opened after
`incidents.resource_open_cycles` consecutive confirming samples (default 2).
A count of samples is a duration only at one poll cadence. The dashboard
lets a viewer change the fleet cadence (ADR-0005), and attended viewing
already tightens the process cadence (ADR-0015), so the same setting meant
"about a minute" on one deployment and "ten seconds" on another.

On a live deployment polling every five seconds, VRAM spikes of ten to
fifteen seconds satisfied two cycles and produced 43 `gpu_memory` incidents
in five hours; 42 of them lasted under ten minutes and most resolved within
twenty seconds. The same deployment logged sixteen two-minute
`gpu_idle_memory` incidents, each a checkpoint or evaluation pause during
which a training job held its VRAM at zero utilization, which is not the
held-but-unused device the condition exists to catch. Each spurious incident
was a webhook delivery, an attention-panel entry, and a transition in the
bounded ring.

## Driving factors

- Sensitivity to transient spikes must not depend on the poll interval.
- `IncidentPolicy` stays the single threshold authority; the fix belongs to
  confirmation, not to a browser-side or receiver-side filter.
- Connectivity and GPU-availability conditions are not spikes: an
  unreachable host or a missing device must still open immediately.
- The idle-VRAM condition already needs many cycles (`gpu_idle_memory_cycles`,
  default 12); its floor has to be independent and longer.
- An operator who wants the previous behaviour must be able to turn the
  floor off without touching cycles.
- Missing telemetry must not count as confirmation or as recovery.

## Candidates

### Option A: Tune `resource_open_cycles` per deployment

Raise the cycle count where the cadence is fast.

Pros: no new setting; no code change.

Cons: the number is only right for one cadence, and the cadence is a
dashboard-adjustable value; a viewer lowering the interval to watch a job
would silently make every resource condition more trigger-happy. Recovery
would also be delayed by the same count, and the idle-VRAM condition, with
its own cycle count, would need separate re-tuning.

### Option B: Require the confirming samples to also span a wall-clock floor

Keep the cycle count and add `incidents.resource_open_seconds`: a resource
condition opens or changes severity only once the consecutive confirming
samples cover at least that many seconds. Give the idle-VRAM condition its
own longer floor, `incidents.gpu_idle_memory_seconds`.

Pros: sensitivity is expressed in the unit the operator reasons about;
cycles keep their role at slow cadences (a 60-second cadence still needs two
samples); connectivity and availability are untouched; `0` restores
count-only confirmation.

Cons: two more configuration keys; a condition that is real opens up to one
floor later than before; the tracker carries the first confirming
observation time per candidate.

### Option C: Smooth the value instead of the confirmation

Evaluate thresholds against a moving average or require hysteresis bands.

Pros: no second time dimension in the tracker.

Cons: `value` would no longer be the observed value, so the dashboard and
API would show a number nobody measured; hysteresis duplicates threshold
semantics per condition; and a smoothed value still opens on a spike whose
length happens to match the window at a fast cadence.

## Decision

Choose Option B. `incidents.resource_open_seconds` defaults to 60 with a
range of 0 through 3600 and applies to CPU, memory, swap, filesystem,
pressure, GPU-memory, temperature, idle-memory, and hardware-health
conditions: a candidate must be confirmed by `resource_open_cycles`
consecutive samples whose first and last observations are at least that far
apart before it opens or changes severity. `incidents.gpu_idle_memory_seconds`
defaults to 300 over the same range and is the corresponding floor for the
idle-VRAM condition, in addition to its `gpu_idle_memory_cycles`.
Connectivity and GPU-availability conditions open on their existing rules
regardless of either floor. Setting a floor to `0` confirms by cycles alone.

Missing telemetry keeps its existing meaning: a failed probe or an
unavailable domain neither confirms a candidate nor recovers an open
condition, so the floor measures confirmed observation, not elapsed time.

## Impact

- At a five-second cadence, a resource condition needs about a minute of
  consecutive confirmation and idle VRAM about five minutes; at a
  one-minute cadence the behaviour is unchanged from before.
- Changing the fleet cadence from the dashboard no longer changes how
  spike-sensitive resource incidents are.
- Real conditions open up to `resource_open_seconds` later than they did;
  the trade is recorded in `CONFIGURATION.md` next to the keys.
- The floors are configuration bounds validated at load time and published
  through the configuration schema like every other `incidents` key.
