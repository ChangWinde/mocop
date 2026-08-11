# ADR-0005: Persist bounded collector settings from the dashboard

## Status

Accepted

## Context

Operators can change Mocop's collection cadence in the dashboard, but the change currently affects only the running process and is lost after a restart. The settings surface also needs a small set of useful collection controls without becoming a general configuration editor. Presentation choices belong to an individual browser, while cluster collection policy must be durable and shared by the service.

## Driving factors

- Apply collection-policy changes without restarting or interrupting healthy nodes.
- Restore dashboard-managed collection policy on the next process start.
- Keep browser authority narrow and reject arbitrary configuration keys.
- Reuse strict configuration validation and private atomic file replacement.
- Avoid server-side state for preferences that differ between viewers.

## Candidates

### Option A: Keep every setting in browser storage or process memory

Pros: no new durable write surface and minimal backend code.

Cons: collection changes disappear after restart, browsers disagree about the active policy, and process-level settings cannot be restored from browser storage before collection begins.

### Option B: Expose the complete JSON configuration to the dashboard

Pros: every current and future field becomes editable with one generic endpoint.

Cons: grants unnecessary authority over SSH paths, listeners, thresholds, host overrides, and discovery policy; increases validation and conflict complexity; and makes safe UI guidance difficult.

### Option C: Extend the atomic configuration controller with a typed allowlist

Pros: persists only collection cadence, complete-probe timeout, and worker concurrency; validates the complete resulting configuration; hot-applies one immutable `MonitorConfig`; and reuses the existing private atomic write boundary.

Cons: each newly dashboard-managed field requires an explicit contract and migration-aware UI work.

## Decision

Choose Option C. The dashboard configuration controller accepts only `pollIntervalSeconds`, `probeTimeoutSeconds`, and `maxWorkers`, maps them to the canonical JSON fields, applies stricter dashboard bounds where appropriate, validates the complete candidate through `load_config`, and atomically replaces the selected user configuration. The service callback updates the active immutable configuration and wakes scheduling immediately. The existing cadence shortcut uses the same durable path.

Visual style, accent, density, ordering, default fleet filter, GPU sorting, heatmap metric, and optional columns remain in a versioned browser-local record. These values affect rendering only and therefore do not enter the cluster configuration or overwrite another viewer's preferences.

## Impact

- Collection policy survives service restart and remains visible in the settings response.
- A write failure leaves the previous configuration intact; a runtime callback failure is surfaced explicitly after persistence.
- Browser input cannot select paths, commands, destinations, listeners, discovery policy, thresholds, or arbitrary JSON keys.
- Inventory and collector changes share one serialized, validated, atomic file boundary.
