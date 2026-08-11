# ADR-0014: Tiered GPU process telemetry

## Status

Accepted

## Context

GPU utilization, VRAM, temperature, and power are Mocop's freshness-critical data.
The agentless probe also starts a second `nvidia-smi` query for compute processes on
every host sample. Measurements on the monitored eight-GPU nodes put that process
query between 414 and 1,215 milliseconds. Running it every five seconds accounts for
half of the NVIDIA command count even though the task view does not require the same
freshness as core GPU telemetry.

## Driving factors

- Preserve the five-second default cadence for core GPU and system telemetry.
- Reduce steady-state remote NVIDIA command count by one third.
- Keep the fixed-command, Agentless SSH boundary and bounded output parser.
- Never interpret an intentionally skipped or unavailable task sample as process exit.
- Make cached task data and its observation time explicit to API consumers.

## Candidates

### Option A: Keep GPU devices and processes on one cadence

Pros: every field has the same observation time and the implementation has no cache.

Cons: performs two NVIDIA commands every cycle and repeats the measured slow process
query even when no task view is open.

### Option B: Query processes from a browser-triggered endpoint

Pros: removes the idle steady-state query and provides fresh data after a click.

Cons: adds an HTTP-to-SSH execution path, makes browser count affect target load, and
delays the task dialog behind a remote query.

### Option C: Use an independent bounded process cadence

Pros: retains background task visibility without adding a command surface; three
five-second core samples need three device queries but only one process query in steady
state.

Cons: task data can be up to one process interval old and requires a bounded last-good
cache with explicit sample semantics.

## Decision

Choose Option C. `gpu_process_poll_interval_seconds` is an operator-owned config value
between 2 and 3,600 seconds and defaults to 15 seconds. Every host's first successful
probe samples processes. Later probes continue the core GPU query at the host cadence
and execute the fixed process query only when its independent monotonic deadline is
due. A workload-mode change forces a new process sample.

`MONITOR_V6` adds `PROCESS_SKIPPED` inside the existing bounded process section. A
successful process sample is cached per host; skipped samples publish that immutable
last-good process set with `processes_sampled=false` and its original observation time.
An attempted but unavailable process query is not cached and is retried on the next
core sample. The transition state machine ignores skipped samples, while unavailable
queries and complete probe failures invalidate its comparison baseline.

## Impact

- At the default five-second and 15-second cadences, steady state falls from six to
  four NVIDIA commands per host per 15 seconds, a one-third reduction.
- GPU utilization, VRAM, temperature, power, health, and system resources remain on
  the five-second core cadence.
- API, OpenMetrics, diagnostics, and the task dialog expose process sample freshness.
- No browser request can enable a remote query, and all commands retain the existing
  timeout, output limit, cancellation, and host-key policy.
- `MONITOR_V5` and `MONITOR_V4` payloads remain readable for compatibility.
