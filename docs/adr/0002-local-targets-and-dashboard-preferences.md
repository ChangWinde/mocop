# ADR-0002: Local targets and dashboard preferences

## Status

Accepted

## Context

Operators need one dashboard to include the machine running Mocop, inspect compute processes on an individual GPU, reorder the server rail, and personalize presentation without weakening the existing fixed-command and explicit-inventory boundary. The local machine may not run an SSH server. Browser preferences must not silently mutate the administrator-owned cluster configuration.

## Driving factors

- Keep every collected target explicit and preserve one validated telemetry schema.
- Avoid an SSH round trip for the Mocop machine.
- Expose GPU compute processes without allowing browser-supplied commands.
- Keep display preferences per browser while cluster inventory remains operator-owned JSON.
- Preserve a dependency-free runtime and bounded collection costs.

## Candidates

### Option A: Require loopback SSH and persist dashboard settings in cluster JSON

Pros: reuses the existing transport unchanged and gives every browser the same view.

Cons: requires an unnecessary local SSH service, spends resources on local authentication, and lets presentation changes cross the administrative configuration boundary.

### Option B: Add one explicit local alias and browser-local preferences

Pros: runs the same fixed probe locally without SSH, keeps inventory reviewable, avoids new write APIs, and lets each operator choose ordering and columns independently.

Cons: local collection is intentionally limited to one alias, and preferences do not follow the user to another browser.

### Option C: Query GPU processes on demand through a new HTTP-to-SSH endpoint

Pros: avoids steady-state process queries when no GPU detail is open.

Cons: adds a browser-triggered remote-execution path, requires caching and rate limiting, delays the first detail view, and makes multiple browser sessions affect target load.

## Decision

Choose Option B. Add an optional `local_host` alias that must also appear in `hosts`. `ResourceProbe` selects local or OpenSSH transport from that validated value and feeds both through the same fixed script, parser, limits, and immutable result types. The fixed script adds one bounded `nvidia-smi --query-compute-apps` call so task data belongs to the same snapshot; browser input never changes the command.

Store display-only preferences in a versioned, validated `localStorage` record. Server ordering, GPU sorting, heatmap metric, and optional columns are browser concerns. Dragging a server switches the rail to custom order. Cluster targets, thresholds, cadence defaults, SSH arguments, and commands remain outside browser control.

Capacity matching also remains a browser concern. It transforms the already published snapshot only while its dialog is open, groups devices by host and model, and never becomes an on-demand probe or reservation endpoint.

## Impact

- A Mocop host can be monitored without running SSH by setting `local_host` to one alias in the explicit allowlist.
- Remote and local results share one protocol and UI data contract.
- GPU task freshness matches the normal host cadence at the cost of one additional `nvidia-smi` process query per successful probe.
- Invalid or stale browser preferences fall back to safe defaults and never prevent telemetry rendering.
- No new runtime dependency, server-side preference store, or browser-triggered probe route is introduced.
- Capacity requests add no network or target load and must be presented as placement guidance rather than resource reservation.
