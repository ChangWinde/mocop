# ADR-0018: Browser-side process search over the authenticated snapshot

## Status

Accepted

## Context

Operators need to find an active GPU process across the fleet or within one
selected server, then move directly to the owning GPU. The current inventory
search only matches host, GPU model, and UUID. The GPU detail dialog shows rich
process identity, command, workload, owner, runtime, and VRAM data, but it can
only be reached by opening GPUs one at a time and it truncates the visible list
before an operator can narrow a busy device.

The authenticated snapshot already contains the bounded, last-observed process
records required for search. Process sampling has an independent attended
cadence under [ADR-0014](0014-tiered-gpu-process-telemetry.md) and
[ADR-0015](0015-attended-cadence-and-identity-tier.md); a search interaction must
not add an HTTP-to-SSH execution path or imply that cached data is live.

## Driving factors

- Search process name, command, PID, owner, workload identity, queue, and
  namespace globally or inside the selected host.
- Preserve the existing authenticated data boundary and never trigger remote
  commands from a browser search.
- Keep rendering bounded and responsive for the maximum supported snapshot.
- Label stale cached records honestly and keep unavailable process telemetry
  distinct from an empty result.
- Use text-only DOM sinks for remote process and workload values.
- Let a result open the exact GPU and narrow its process list without hiding a
  match below the normal 100-row detail limit.

## Candidates

### Option A: Extend only the existing GPU-row predicate

Pros: minimal code and no new surface.

Cons: a process match would reveal only the containing GPU row; the operator
would still need to open devices one by one to discover the matching PID,
command, owner, or workload. It does not provide a useful result count or direct
navigation.

### Option B: Build a bounded browser-side process result view

Pros: reuses the already authenticated snapshot, adds no network or SSH work,
supports global and selected-host scopes, can expose freshness and workload
context, and can open the exact GPU with a prefilled local process filter.

Cons: the result is limited to the most recently observed snapshot and the UI
must bound its DOM result set independently of the backend payload bound.

### Option C: Add a server-side `/api/processes` query endpoint

Pros: the server could centralize ranking and return a small result payload.

Cons: duplicates data already present in `/api/snapshot`, expands the public API
and authorization surface, introduces query parsing and cache/version semantics,
and risks creating a browser-triggered remote collection expectation.

## Decision

Choose Option B. The browser exposes one unified inventory query. Its pure search
boundary is conceptually:

```text
searchProcessRecords(snapshot, {query, host})
  -> {matches, total, unavailableGpuCount, truncated}
```

The query is normalized once with Unicode NFKC, lower-cased, bounded to 120
characters, and split into whitespace-delimited terms. Every term must occur in
the record's bounded text projection. Matching uses literal substring checks,
not regular expressions. Results are ranked deterministically, report the full
match count, and render at most 200 records. Remote values are assigned only
through `textContent`, attributes with fixed names, and property setters; they
are never parsed as HTML, CSS, URLs, or selectors.

The selected fleet host is the process-search scope: `all` searches the current
fleet snapshot and a concrete alias searches only that server. A matching
process also keeps its GPU visible in the existing inventory filter. Activating
a process result opens that GPU, copies the query into a dialog-local process
filter, and focuses the exact process row. The dialog filter runs before its
100-row display cap, so a low-VRAM match cannot be hidden by truncation. Closing
the dialog clears its transient query.

## Impact

- Search results reflect only monitor-observed process data and display stale
  server/process freshness explicitly; unavailable process telemetry is counted
  and explained instead of being treated as zero processes.
- Search remains an in-browser transform of the authenticated snapshot and adds
  no API route, persistent search history, remote command, or secret-bearing URL.
- Result nodes are keyed and reconciled across live snapshots so keyboard focus
  is not destroyed by telemetry-only updates.
- The GPU detail dialog gains a local literal filter and a name sort alongside
  the existing memory and runtime sorts.
- Browser end-to-end tests cover global and host scope, direct navigation,
  focus preservation, matching beyond the normal row cap, unavailable data,
  and text-only rendering of hostile-looking process values.
