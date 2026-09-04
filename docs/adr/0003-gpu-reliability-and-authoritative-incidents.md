# ADR-0003: GPU reliability signals and authoritative incidents

## Status

Accepted

## Context

Mocop reports current GPU utilization and temperature, but it cannot distinguish a healthy zero-GPU host from a failed NVIDIA query, detect an unexpected GPU-count change, or surface ECC and hardware slowdown signals. Incident conditions are also evaluated independently in Python and JavaScript. A transient SSH failure can therefore create repeated open and resolved events, while the attention view can disagree with the authoritative incident history.

Production evidence from the current 11-host cluster showed that 194 of the latest 200 events came from one oscillating SSH target. Representative RTX 4090 and A100 hosts both support a bounded `nvidia-smi` query for ECC, retired or remapped memory state, hardware slowdown reasons, and MIG mode.

## Driving factors

- Keep remote hosts agentless and preserve one logical SSH round trip per cycle.
- Never let optional health telemetry invalidate base GPU and system telemetry.
- Keep alert decisions deterministic, configurable, testable, and authoritative.
- Detect GPU subsystem loss even when Linux system collection remains healthy.
- Bound remote commands, output, state, and browser payload growth.

## Candidates

### Option A: Extend the fixed probe and incident policy

Add a versioned optional health section to the existing fixed script, explicit expected GPU counts in JSON configuration, and per-condition activation and recovery cycles in the backend incident state machine. The browser consumes backend conditions and only groups them for presentation.

Pros: preserves zero runtime dependencies, one SSH round trip, strict command ownership, bounded memory, and the existing `ResourceProbe` and `IncidentPolicy` interfaces. Optional health-query failure cannot hide base telemetry.

Cons: does not provide kernel Xid event streaming or multi-day durable hardware history, and adds one short `nvidia-smi` process per host cycle.

### Option B: Require NVIDIA DCGM and Prometheus

Deploy DCGM Exporter on GPU nodes and use Prometheus and Alertmanager as the source of health state.

Pros: provides broad NVIDIA hardware telemetry, durable queryable history, and mature alert routing.

Cons: adds resident agents, inbound endpoints, operational dependencies, storage, and a second source of truth. It conflicts with Mocop's current agentless installation and is disproportionate for the measured 11-host workload.

### Option C: Keep browser-derived warnings and add more visual rules

Continue calculating attention conditions from each snapshot in JavaScript and leave the transition tracker unchanged.

Pros: smallest backend change and immediate visual flexibility.

Cons: preserves duplicated policy, inconsistent severity, alert flapping, and the inability to distinguish collection failures from real zero-GPU inventory.

## Decision

Choose Option A. Advance the fixed collection contract to `MONITOR_V4` with an independently fallible `GPU_HEALTH` section keyed by UUID. Represent health as an optional immutable value nested under each GPU. Keep the existing base GPU query authoritative when the health query is unavailable.

> **Update:** `MONITOR_V4` records the historical decision; the protocol is now `MONITOR_V8` and, per [ADR-0016](0016-single-version-protocol-and-agent-api.md), the parser accepts only the current version.

Add validated `expected_gpu_counts` and `incidents` objects to configuration. `ThresholdIncidentPolicy` remains the only condition evaluator. `IncidentTracker` applies condition-specific activation and recovery cycles: connectivity opens immediately but requires stable recovery, ordinary resource conditions require repeated samples, and idle-with-VRAM requires a longer sustained window. Active conditions retain the value that opened or changed severity until resolution, keeping the incident version transition-based and avoiding an extra HTTP request per telemetry update.

The browser consumes `/api/incidents` for the attention view and event history. It may group conditions that share a backend-provided `groupKey`, but it does not decide whether a condition exists or what its severity is.

## Impact

- NVIDIA subsystem loss, expected-count mismatch, unavailable process telemetry, high VRAM pressure, sustained idle VRAM, ECC errors, pending memory repair, and hardware slowdown can become explicit incidents.
- Optional health fields add no browser-controlled command path and remain under the existing process timeout and output limit.
- Alert opening and recovery become stable across transient samples without delaying the first connectivity warning.
- New configuration fields are optional and safe for existing installations; expected counts remain opt-in because silently learned baselines could normalize an already degraded restart.
- DCGM integration remains an alternative behind `ResourceProbe` if the deployment later requires kernel-event fidelity or more than the documented architecture thresholds.
