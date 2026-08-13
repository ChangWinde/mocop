# Mocop HTTP API

This is the complete reference for Mocop's HTTP API. It is written for both
human operators and automation ("agents"): every route, header requirement,
response field, and stable error code below is verified against the route
manifest (`API_ROUTES` in `mocop/web.py`) by a repository test, so the
document and the running server cannot drift apart silently.

## Scope and compatibility

- **API version:** `1` (`apiVersion` in [GET /api/meta](#get-apimeta)). Within
  one API version, existing fields keep their name, type, and unit; new
  optional fields and new endpoints may appear at any time. Clients must
  ignore unknown fields.
- **Schema version:** `1` (`schemaVersion` in `GET /api/meta`) tracks the
  shape of the payloads themselves.
- **Deprecation policy:** a deprecated endpoint keeps working and answers
  with a `Deprecation: true` response header. Deprecations and removals are
  announced in [CHANGELOG.md](CHANGELOG.md). Currently deprecated:
  `GET /api/service` and `POST /api/settings/poll-interval`.
- **Self-description:** `GET /api/meta` returns the exact endpoint manifest
  (method, path, access tier) plus capability flags, so an agent can discover
  what this deployment supports before calling anything else.

## Base URL and transport conventions

- Default base URL: `http://127.0.0.1:8787` (configuration keys
  `listen_host` / `listen_port`; the default listener is loopback-only).
- Protocol: HTTP/1.1. JSON responses are UTF-8
  (`Content-Type: application/json; charset=utf-8`) with compact separators.
- **Timestamps** are RFC 3339 / ISO 8601 UTC strings with second precision
  and a `Z` suffix, for example `2026-08-13T06:10:00Z`. There are no local
  timezones and no sub-second precision anywhere in the API.
- **Caching:** JSON, SSE, and OpenMetrics responses are `Cache-Control:
  no-store`. Static dashboard assets (`/`, `/index.html`, `/app.js`,
  `/styles.css`, `/favicon.svg`) are `no-cache` with a strong content
  `ETag`; a matching `If-None-Match` yields `304 Not Modified`.
- `HEAD` is supported on every `GET` route except `GET /api/events`
  (the event stream answers `405` with `Allow: GET`).
- A `GET`/`HEAD` request that declares a body is rejected with
  `400 REQUEST_BODY_NOT_ALLOWED`.
- `OPTIONS` always answers `403 UNTRUSTED_ORIGIN`: the API intentionally has
  no cross-origin contract and never grants CORS permission.
- **Connection bounds:** at most 64 concurrent connections (excess
  connections receive a bare `503` with an empty body, not JSON) and at most
  16 concurrent event-stream clients (the 17th receives a JSON `503`).
- Unknown paths and wrong methods under the API family (`/api/...`,
  `/healthz`, `/readyz`, `/metrics`) answer with the JSON error envelope:
  `404 NOT_FOUND` or `405 METHOD_NOT_ALLOWED` with an `Allow` header.
  Non-API paths keep the default HTML error page.

## Access tiers

Every endpoint belongs to one of three tiers (the `access` value in
`GET /api/meta`):

| Tier | `access` | Requirements |
|---|---|---|
| **L** | `listener` | None. Any client that can reach the listener may read. |
| **R** | `reader` | A trusted `Host` header **and** `X-Monitor-Request: dashboard`. If the browser sends `Sec-Fetch-Site`, it must be `same-origin` or `none`. |
| **W** | `writer` | Everything in R, **plus** an `Origin` header whose scheme is `http`/`https`, whose hostname is trusted, whose path is empty or `/`, and which carries no credentials, query, or fragment, **plus** `Content-Type: application/json` and a body within the route's byte cap. |

Trusted hostnames are: `localhost`, `127.0.0.1`, `::1`, the configured
non-wildcard `listen_host`, and every entry of the optional
`trusted_web_hosts` configuration list. A failed tier check answers
`403 UNTRUSTED_ORIGIN`.

There are no accounts or tokens: reachability of the (loopback by default)
listener is the authorization boundary, and the R/W header checks exist to
keep browsers from being confused deputies (CSRF, DNS rebinding). A local
non-browser client can always construct these headers; that is inside the
existing trust boundary. See [SECURITY.md](SECURITY.md).

### Viewer side effect of the marker header (important for agents)

The `X-Monitor-Request: dashboard` header is not just an access marker — it
is a **presence signal**:

- Any read request carrying the header, and every wake of a connected
  `GET /api/events` stream, marks the monitor as *attended* for the next
  30 seconds.
- While attended, GPU **process** telemetry keeps its baseline cadence
  (15 seconds by default). While unattended, every device stretches its
  process query to 16× the base interval (4 minutes at defaults); core GPU,
  system, trend, and incident telemetry never change cadence.
- The first returning viewer forces a catch-up process sample on the next
  5-second core cycle.

**Non-viewer automation (collectors, cron jobs, inspection agents) must not
send the marker header.** Every diagnostic read an agent needs is available
at the L tier without it. Sending the marker from an always-on poller keeps
the whole fleet on the attended process cadence around the clock and defeats
the unattended power/command savings.

## API conventions

### Naming

Top-level JSON keys are `camelCase`. Two legacy subtrees are `snake_case`
and are documented as-is (there are no duplicate camelCase keys):

- `servers[].system` and `servers[].gpus` in the snapshot, including their
  nested `disks`, `processes`, `workload`, and `health` objects
  (for example `memory_total_mib`, `used_memory_mib`, `first_seen_at`,
  `started_at`).
- `incidentActions[]` items in the inventory use `condition_key`.

### Units

| Quantity | Unit | Examples |
|---|---|---|
| Memory, VRAM, disk capacity | MiB | `memory_total_mib`, `memoryUsedMiB` |
| Network / disk I/O rates | bytes per second | `network_rx_bps`, `diskReadBps` |
| Ratios | percent, 0–100 | `cpu_usage_pct`, `utilization_gpu_pct` |
| Latency / durations | milliseconds | `latencyMs`, `lastPollDurationMs` |
| Intervals / timeouts | seconds | `pollIntervalSeconds`, `retryAfterSeconds` |
| Temperature | degrees Celsius | `temperature_c` |
| Power | watts | `power_draw_w` |
| Timestamps | RFC 3339 UTC, second precision | `generatedAt` |

OpenMetrics (`GET /metrics`) converts to base units (bytes, ratios 0–1,
seconds) as listed in the [OpenMetrics reference](#openmetrics-reference).

### Nullability

Telemetry that is unavailable is `null`, never `0` or a sentinel. Common
cases: `cpu_usage_pct` and the rate fields need two samples and are `null`
on a host's first sample; NVIDIA fields report `null` when the driver
returns `N/A`; `latencyMs`, `lastSuccessAt`, `nextRetryAt`, `message` are
`null` until the corresponding event has happened.

### Strict JSON on writes

Write bodies are parsed strictly: duplicate keys, unknown keys, missing
keys, non-finite numbers (`NaN`/`Infinity`), and boolean values where
numbers are expected are all rejected. Each write route has a hard body
cap:

| Route | Body cap (bytes) |
|---|---|
| `POST /api/settings/poll-interval` | 128 |
| `POST /api/settings/collector` | 512 |
| `POST /api/settings/hosts` | 512 |
| `POST /api/settings/maintenance` | 512 |
| `POST /api/settings/host-group` | 512 |
| `POST /api/settings/incident-action` | 1024 |
| `POST /api/probe` | 512 |
| `POST /api/notifications/test` | 32 |
| `POST /api/service/restart` | 32 |

### Error envelope and stable codes

Every API error response is a JSON object:

```json
{"error": "human-readable message", "code": "STABLE_MACHINE_TAG"}
```

`code` is stable and safe to branch on; `error` is for humans and may be
reworded. One exception: the `POST /api/probe` conflict/rate-limit/unknown
responses carry the probe status body *plus* a `code` field instead of an
`error` key (see the endpoint entry).

| Code | HTTP | Meaning |
|---|---|---|
| `INVALID_REQUEST_TARGET` | 400 | The request URL could not be parsed. |
| `REQUEST_BODY_NOT_ALLOWED` | 400 | A `GET`/`HEAD` request declared a body. |
| `QUERY_NOT_ALLOWED` | 400 | This route accepts no query parameters. |
| `UNKNOWN_QUERY_PARAMETER` | 400 | A query parameter outside the route's allowlist. |
| `INVALID_QUERY` | 400 | A malformed `host`, `gpu`, or `limit` query value. |
| `INVALID_LIMIT` | 400 | `limit` is not an integer within the route's bounds. |
| `INVALID_HOURS` | 400 | `hours` is not an integer within the route's bounds. |
| `INVALID_HOST` | 400 | The `host` query value is not a safe alias. |
| `INVALID_JSON` | 400 | The body is not valid strict JSON (duplicate keys and non-finite numbers included). |
| `INVALID_SCHEMA` | 400 | The JSON body does not match the route's exact schema. |
| `INVALID_SETTINGS` | 400 | Schema-valid values outside documented bounds or invalid for the current configuration. |
| `UNTRUSTED_ORIGIN` | 403 | Missing/untrusted `Host`, marker header, `Origin`, or cross-site Fetch Metadata; also every `OPTIONS`. |
| `NOT_FOUND` | 404 | Unknown API-family path. |
| `UNKNOWN_HOST` | 404 | The named host is not a current monitoring target. |
| `UNKNOWN_GPU` | 404 | The named GPU identity has no telemetry on that host. |
| `METHOD_NOT_ALLOWED` | 405 | Wrong method for a known path; the `Allow` header lists valid methods. |
| `PROBE_IN_PROGRESS` | 409 | A probe for that host is already running. |
| `INVENTORY_CHANGED` | 409 | The configuration changed under the request; re-read and retry. |
| `PAYLOAD_TOO_LARGE` | 413 | Body missing, empty, or beyond the route's byte cap. |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | `Content-Type` is not `application/json`. |
| `RATE_LIMITED` | 429 | Manual probe cooldown (with `Retry-After` header) or notification-test cooldown. |
| `INTERNAL_ERROR` | 500 | The host-group change failed unexpectedly. |
| `SERVICE_UNAVAILABLE` | 503 | The capability is not available (no config controller, restart not supervised, manual probing disabled, SSE slots exhausted, scan/persist failure). |
| `NOTIFICATIONS_DISABLED` | 503 | Notification test requested but no webhook is configured. |

### Retries and idempotency

- All `GET` routes are safe and can be retried freely.
- **`POST /api/service/restart` is not retryable.** After the `202` the
  process exits and systemd starts a replacement. If your request times out,
  do not resend blindly: poll `GET /healthz` until it answers, then compare
  the snapshot `startedAt` with the pre-restart value — if it changed, the
  restart already happened.
- **`POST /api/settings/maintenance` is not idempotent in time:** the server
  computes `until = now + durationSeconds` on every accepted call, so
  resubmitting the same body extends the window from the new "now".
  `durationSeconds: 0` (clear) is idempotent.
- **`POST /api/settings/incident-action`** behaves the same way: repeats
  recompute `until`; `action: "clear"` is idempotent.
- **`POST /api/settings/hosts`** is not idempotent: adding an
  already-configured host or removing a host that is no longer active
  answers `409 INVENTORY_CHANGED`. Always read `GET /api/inventory` first
  and treat `409` as "re-read, then decide again".
- **`POST /api/probe`** answers `409 PROBE_IN_PROGRESS` while a probe runs
  and `429 RATE_LIMITED` with a `Retry-After` header (seconds) during the
  per-host cooldown. Honor `Retry-After`; do not hammer.
- Configuration writes are serialized server-side against the live JSON
  file; `409 INVENTORY_CHANGED` always means "someone else changed the
  configuration — re-read before retrying".

## Quick start

Five copy-paste examples against a default local deployment.

1. Bare read — no headers needed at the L tier:

```bash
curl -s http://127.0.0.1:8787/api/snapshot | jq '.stats.onlineServers'
```

2. R-tier read (inventory). Note: the marker header also flags a live
   viewer for 30 seconds — use it only when something is actually watching:

```bash
curl -s -H 'X-Monitor-Request: dashboard' \
  http://127.0.0.1:8787/api/inventory | jq '.activeHosts'
```

3. W-tier write — start a one-hour maintenance window:

```bash
curl -s -X POST http://127.0.0.1:8787/api/settings/maintenance \
  -H 'Origin: http://127.0.0.1:8787' \
  -H 'X-Monitor-Request: dashboard' \
  -H 'Content-Type: application/json' \
  -d '{"host":"gpu-node-01","durationSeconds":3600,"reason":"kernel upgrade"}'
```

4. Server-Sent Events — one `snapshot` event per state revision plus a named
   `heartbeat` event every 15 quiet seconds (a connected stream counts as a
   viewer):

```bash
curl -N http://127.0.0.1:8787/api/events
```

5. Error handling — branch on the stable `code`, never on the message:

```bash
curl -s 'http://127.0.0.1:8787/api/incidents?limit=0' | jq -r '.code'
# INVALID_LIMIT
```

## Endpoint index

This table matches the server's route manifest exactly.

| Method | Path | Tier | Purpose |
|---|---|---|---|
| GET | `/api/snapshot` | L | Full current state: hosts, GPUs, processes, stats. |
| GET | `/api/events` | L | SSE stream of snapshots with named heartbeats. |
| GET | `/api/history` | L | Per-host resource trend points. |
| GET | `/api/usage` | L | Per-owner GPU occupancy and idle-occupancy rollup. |
| GET | `/api/incidents` | L | Active conditions, transition events, correlations. |
| GET | `/api/meta` | L | API self-description: versions, capabilities, endpoints. |
| GET | `/api/service` | L | **Deprecated** alias of the `/api/meta` capabilities block. |
| GET | `/healthz` | L | Liveness plus cumulative transport retries. |
| GET | `/readyz` | L | Readiness; `503` until the first successful sample. |
| GET | `/metrics` | L | OpenMetrics 1.0 exposition of the current snapshot. |
| GET | `/api/gpu-history` | R | Per-GPU trend points and process events. |
| GET | `/api/diagnostics` | R | Redacted, alias-anonymized support bundle. |
| GET | `/api/inventory` | R | Configured/active/available hosts and collector settings. |
| GET | `/api/topology` | R | Display-only connection tree. |
| POST | `/api/settings/collector` | W | Update any subset of the collector settings. |
| POST | `/api/settings/poll-interval` | W | **Deprecated** single-field cadence alias. |
| POST | `/api/settings/hosts` | W | Add or remove one monitored host. |
| POST | `/api/settings/maintenance` | W | Start or clear one maintenance window. |
| POST | `/api/settings/host-group` | W | Assign or clear one host's group. |
| POST | `/api/settings/incident-action` | W | Acknowledge, silence, or clear one condition. |
| POST | `/api/probe` | W | Queue one immediate probe of one host. |
| POST | `/api/notifications/test` | W | Queue one webhook delivery test. |
| POST | `/api/service/restart` | W | Restart the supervised service. |

## Endpoints

### GET /api/snapshot

Full current state. Tier L. Query: ignored. This is the same object carried
by every SSE `snapshot` event.

Top-level fields:

| Field | Type | Description |
|---|---|---|
| `version` | int | Monotonic state revision; increases on every observable change. |
| `appVersion` | string | Mocop release, e.g. `"0.8.0"`. |
| `incidentVersion` | int | Incident-view revision; also advances on action/maintenance expiry. |
| `generatedAt` | timestamp | When this snapshot projection was assembled (per state revision, not per request). |
| `startedAt` | timestamp | Process start; changes prove a restart happened. |
| `pollIntervalSeconds` | number | Current global collection cadence. |
| `collectionStaleAfterSeconds` | number | `pollIntervalSeconds × collection_stale_cycles`; freshness horizon. |
| `lastPollCompletedAt` | timestamp \| null | When the most recent *scheduler submission batch* fully completed. |
| `lastPollDurationMs` | int \| null | Duration of that batch. |
| `collectorError` | string \| null | Fleet-level collector failure (e.g. discovery failed), else `null`. |
| `persistence` | object | `{enabled, backend, healthy, queuedWrites, droppedWrites, lastError}` (+ `writtenRecords` when SQLite). |
| `notifications` | object | `{enabled, healthy, queuedDeliveries, droppedDeliveries, endpoints[]}`; each endpoint reports `{name, healthy, queuedDeliveries, deliveredEvents, droppedDeliveries, lastError, lastAttemptAt, lastSuccessAt}`. |
| `thresholds` | object | The eleven active incident thresholds (`cpu_warning_pct`, …, `psi_io_some_pct`). |
| `stats` | object | Fleet aggregates; see below. |
| `servers` | array | Per-host state; see below. |

Timestamp disambiguation (frequently confused):

- `generatedAt` — when the served snapshot was built; advances with state
  revisions.
- `lastPollCompletedAt` — when the last complete scheduler batch finished;
  fleet-level collection liveness.
- `servers[].lastSuccessAt` — that host's most recent successful probe;
  per-host data freshness.

`stats` fields (all numbers; aggregates cover **online** hosts only):

| Field | Description |
|---|---|
| `servers`, `onlineServers`, `staleServers`, `pollingServers` | Fleet counts by state. |
| `issueServers` | Hosts that are not online or have any active incident. |
| `incidentServers` | Hosts with at least one active incident. |
| `actionableIssueServers`, `actionableIncidentServers` | Same, excluding maintained/acknowledged/silenced conditions. |
| `maintenanceServers` | Hosts inside an active maintenance window. |
| `activeIncidents`, `criticalIncidents` | Raw active condition counts. |
| `actionableIncidents`, `actionableCriticalIncidents` | Active counts excluding silenced/acknowledged conditions. |
| `gpus`, `busyGpus` | GPUs on online hosts; busy means utilization ≥ `gpu_busy_pct`. |
| `memoryTotalMiB`, `memoryUsedMiB` | Cluster VRAM (MiB). |
| `cpuAveragePct` | Mean host CPU percent, `null` before first deltas. |
| `cpuCores`, `systemMemoryTotalMiB`, `systemMemoryUsedMiB`, `swapTotalMiB`, `swapUsedMiB`, `diskTotalMiB`, `diskUsedMiB` | System capacity aggregates. |
| `networkRxBps`, `networkTxBps`, `diskReadBps`, `diskWriteBps` | Rate aggregates (bytes/second). |

`servers[]` fields:

| Field | Type | Description |
|---|---|---|
| `host` | string | The configured SSH alias (collection identity). |
| `status` | string | `pending`, `online`, `unreachable`, `no_nvidia_smi`, or `error`. |
| `polling` | bool | A probe is currently in flight for this host. |
| `latencyMs` | int \| null | Duration of the most recent probe attempt. |
| `message` | string \| null | Redacted failure classification or GPU-query warning. |
| `lastAttemptAt` | timestamp \| null | Most recent probe attempt. |
| `lastSuccessAt` | timestamp \| null | Most recent successful probe. |
| `nextRetryAt` | timestamp \| null | Backoff deadline while failing. |
| `stale` | bool | **Exactly:** `status` is not `online` **and** `lastSuccessAt` is not null — the shown resources are last-known data. |
| `consecutiveFailures` | int | Failed probes since the last success. |
| `transportRetried` | bool | The most recent probe retried once over a fresh connection after a stale multiplexed SSH transport. |
| `system` | object \| null | Last-known system metrics (snake_case; see below). |
| `gpus` | array | Last-known GPU metrics (snake_case; see below). |
| `maintenance` | object \| null | Active window as `{until, reason}` (+ `recurring: true` for weekly windows), else `null`. |
| `group` | string \| null | Configured host group. |
| `displayName` | string \| null | Optional human-readable label; `host` remains the identity. |
| `incidents` | object | `{active, critical, actionable, actionableCritical}` counts for this host. |

`servers[].system` (snake_case): `hostname`, `uptime_seconds`, `load_1m`,
`load_5m`, `load_15m`, `cpu_cores`, `cpu_usage_pct` (null on first sample),
`memory_total_mib`, `memory_used_mib`, `memory_available_mib`,
`swap_total_mib`, `swap_used_mib`, `disk_total_mib`, `disk_used_mib`,
`network_rx_bps`, `network_tx_bps`, `disk_read_bps`, `disk_write_bps`
(rates null on first sample), `disks[]` with `device`,
`filesystem_type`, `mountpoint`, `total_mib`, `used_mib`, `available_mib`,
`used_pct`, and `pressure` (null on kernels without PSI) with per-resource
`cpu`/`memory`/`io` objects of `some_avg10`, `some_avg60`, `full_avg10`,
`full_avg60` — the percentage of the trailing 10/60 seconds during which at
least one task (`some`) or every task (`full`, nullable) was stalled on
that resource.

`servers[].gpus[]` (snake_case):

| Field | Type | Description |
|---|---|---|
| `index` | int | NVIDIA device index. |
| `uuid` | string | Device UUID (may be empty when the driver hides it). |
| `name`, `driver_version` | string | Model and driver. |
| `pstate` | string \| null | Performance state. |
| `temperature_c`, `utilization_gpu_pct`, `utilization_memory_pct` | number \| null | Current readings. |
| `memory_total_mib`, `memory_used_mib`, `memory_free_mib` | number \| null | VRAM. |
| `power_draw_w`, `power_limit_w` | number \| null | Power. |
| `processes` | array | Compute processes; see below. |
| `processes_available` | bool | `false`: the process query ran and failed/was unsupported; retried every core cycle. |
| `processes_sampled` | bool | `false`: this cycle intentionally **reused the cached last-good process set** (tiered cadence); `processes_observed_at` keeps the cache's original time. |
| `processes_observed_at` | timestamp \| null | When the shown process set was actually sampled. |
| `health` | object \| null | `{ecc_uncorrected_volatile, retired_pages_pending, remapped_rows_pending, thermal_slowdown, power_brake_slowdown, mig_mode}`, each nullable. |

`processes[]`:

| Field | Type | Description |
|---|---|---|
| `pid` | int | Remote PID. |
| `name` | string | Process name from `nvidia-smi`. |
| `used_memory_mib` | number \| null | VRAM used by this process. |
| `workload` | object \| null | Present only with `workloads.mode` `identity`/`auto`: `{kind, workload_id, name, owner, queue, namespace, command, started_at}`; `kind` is `process`, `slurm`, `kubernetes`, `docker`, or `podman`, everything else nullable. `command` and `started_at` (true start time) are populated by both tiers; the container kinds and scheduler identifiers additionally require the `auto` tier's cgroup read. |
| `first_seen_at` | timestamp \| null | **Monitor-relative lower bound**: when this monitor first observed the `(pid, name)` pair on this device. Resets on monitor restart. When a PID is reused and the workload `started_at` changes, the server treats it as a new instance: a stop/start event pair is emitted and `first_seen_at` restarts. |

Errors: none specific (`400 REQUEST_BODY_NOT_ALLOWED` applies as everywhere).

### GET /api/events

Server-Sent Events stream. Tier L (no marker header required, but a
connected stream **is** a viewer — see the warning above). `HEAD` is
rejected with `405`.

- On connect the current snapshot is sent immediately, then one
  `event: snapshot` frame per state revision. `data:` is the exact
  `GET /api/snapshot` object.
- After 15 seconds without a revision the server sends
  `event: heartbeat` / `data: {}` — a **named event**, so `EventSource`
  clients can observe stream liveness and reconnect on stall.
- At most 16 concurrent stream clients; the next connect receives
  `503 SERVICE_UNAVAILABLE`.

### GET /api/history

Per-host resource trend. Tier L.

Query parameters:

| Parameter | Required | Bounds | Default |
|---|---|---|---|
| `host` | yes | safe alias of a current target | — |
| `limit` | no | integer 2–300 | 120 |

Response: `{host, pollIntervalSeconds, maxPoints, points[]}`. Each point:
`observedAt`, `cpuUsagePct` (nullable), `memoryUsagePct`, `swapUsagePct`,
`diskUsagePct`, `networkRxBps`/`networkTxBps`/`diskReadBps`/`diskWriteBps`
(nullable), `gpuUsagePct`/`gpuMemoryUsagePct`/`gpuTemperatureC` (nullable),
`transportRetried` (bool). Points exist only for successful samples.

Errors: `UNKNOWN_QUERY_PARAMETER`, `INVALID_QUERY`, `INVALID_LIMIT`,
`404 UNKNOWN_HOST`.

### GET /api/usage

Per-owner GPU occupancy rollup over a bounded window. Tier L. Aggregates
the in-memory process timeline (started/stopped transitions plus the live
process table), so coverage is limited to what the monitor observed — the
response says so explicitly instead of extrapolating.

Query parameters:

| Parameter | Required | Bounds | Default |
|---|---|---|---|
| `hours` | no | integer 1–720 | 24 |
| `limit` | no | integer 1–500 (owner rows) | 50 |

Response fields:

| Field | Type | Description |
|---|---|---|
| `generatedAt` | timestamp | When this rollup was computed. |
| `sinceAt` | timestamp | Start of the requested window (`now - hours`). |
| `windowHours` | int | Echo of the effective `hours`. |
| `gpuBusyPct` | number | Utilization threshold that classified idle occupancy. |
| `owners` | array | Per-owner rollup, sorted by `gpuSeconds` descending, at most `limit` rows. |
| `totalOwners` | int | Owner count before the `limit` cut. |
| `totalGpuSeconds` | number | Sum of `gpuSeconds` across every owner (not just the returned rows). |
| `earliestDataAt` | timestamp \| null | Oldest timeline record that informed this rollup. If it is later than `sinceAt`, the window is only partially covered. |
| `droppedRecords` | int | Timeline records skipped because no trustworthy start anchor existed. |

`owners[]` fields:

| Field | Type | Description |
|---|---|---|
| `owner` | string \| null | Process owner from workload identity; `null` when `workloads.mode` is `disabled` or the owner was unresolvable. |
| `gpuSeconds` | number | Wall-clock seconds the owner's processes occupied a GPU (two GPUs in parallel count twice). |
| `sampledSeconds` | number | Portion of `gpuSeconds` that overlapped utilization samples and could be classified. |
| `idleSeconds` | number | Classified seconds during which the occupied GPU stayed below `gpuBusyPct` utilization. |
| `idleShare` | number \| null | `idleSeconds / sampledSeconds`, rounded to 4 decimals; `null` without classified samples. |
| `hosts` | array | Host aliases the owner occupied. |
| `gpus` | int | Distinct GPUs the owner occupied. |
| `processes` | int | Occupancy intervals in the window. |
| `kinds` | object | Interval counts by workload kind (`process`, `slurm`, `kubernetes`, `docker`, `podman`). |

Owner attribution requires `workloads.mode` `identity` or `auto`; with
`disabled` everything aggregates under `owner: null`. Idle classification
skips sample gaps longer than four poll cycles (offline stretches are never
counted as measured activity).

Errors: `UNKNOWN_QUERY_PARAMETER`, `INVALID_QUERY`, `INVALID_HOURS`,
`INVALID_LIMIT`.

### GET /api/incidents

Active conditions, bounded transition history, and shared-path
correlations. Tier L.

Query parameters:

| Parameter | Required | Bounds | Default |
|---|---|---|---|
| `limit` | no | integer 1–200 | 50 |

**`limit` applies only to `events`.** The `active` list is always complete.

Response fields:

| Field | Type | Description |
|---|---|---|
| `version` | int | Incident-view revision (same meaning as snapshot `incidentVersion`). |
| `active` | array | Every currently active condition, decorated and sorted (actionable first, then critical, then host/key). |
| `events` | array | The most recent `limit` transitions, newest first. |
| `correlations` | array | Possible shared-path groupings; see below. |

`active[]` fields:

| Field | Type | Description |
|---|---|---|
| `host` | string | Affected host alias. |
| `conditionKey` | string | Stable key, e.g. `cpu`, `disk:/dev/sda1:/`, `gpu_temperature:<uuid>`. |
| `category` | string | `connectivity`, `cpu`, `memory`, `swap`, `disk`, `pressure`, `gpu_availability`, `gpu_count`, `gpu_processes`, `gpu_temperature`, `gpu_memory`, `gpu_idle_memory`, `gpu_ecc`, `gpu_memory_repair`, `gpu_slowdown`. |
| `resource` | string | Human-readable subject, e.g. `GPU 3 VRAM`. |
| `severity` | string | `warning` or `critical`. |
| `value`, `threshold` | number \| null | Measured value and configured threshold, when numeric. |
| `observedAt` | timestamp | Sample that produced the current state. |
| `detail` | string \| null | Bounded extra context. |
| `groupKey` | string \| null | Set for shared network filesystems so one backend outage groups visually. |
| `firstObservedAt`, `lastObservedAt` | timestamp | When the condition opened / was last confirmed. |
| `maintenanceSilenced` | bool | Host is inside an active maintenance window. |
| `silenced` | bool | `maintenanceSilenced` **or** an active `silenced` action on this condition. |
| `acknowledged` | bool | An active `acknowledged` action exists on this condition. |
| `actionable` | bool | **Exactly:** `not (silenced or acknowledged)`. This is what counts toward the `actionable*` stats and what webhooks/correlation consume. |
| `action` | string \| null | `acknowledged`, `silenced`, or `null`. |
| `actionUntil`, `actionReason` | timestamp/string \| null | Expiry and reason of the active action. |
| `maintenanceUntil`, `maintenanceReason` | timestamp/string | Present only while `maintenanceSilenced` is true. |
| `diagnosis` | object | `{title, summary, evidence[], nextSteps[], targetGpuIndex}` — deterministic guidance, at most 8 evidence items and 4 steps. |

`events[]` carry the same condition fields plus `eventId` (monotonic int),
`state` (`opened`, `resolved`, `escalated`, `deescalated`), and
`observedAt`. `correlations[]` items are `{correlationKey, kind:
"configured_shared_path", anchor, hosts[], severity, confidence:
"possible", detail}` — they group **actionable connectivity** conditions
whose hosts share a configured topology path, without changing the raw
list.

Errors: `UNKNOWN_QUERY_PARAMETER`, `INVALID_LIMIT`.

### GET /api/meta

API self-description. Tier L. Query: rejected (`QUERY_NOT_ALLOWED`).

```json
{
  "apiVersion": "1",
  "appVersion": "0.8.0",
  "schemaVersion": 1,
  "capabilities": {
    "restartSupported": true,
    "manualProbeSupported": true,
    "configurationWriteSupported": true
  },
  "endpoints": [
    {"method": "GET", "path": "/api/snapshot", "access": "listener"}
  ]
}
```

`endpoints` lists every route with its access tier (`listener`, `reader`,
`writer`). `restartSupported` is true only under the supervised user
service; `manualProbeSupported` requires the live scheduler;
`configurationWriteSupported` reports whether the active configuration file
is dashboard-writable (file metadata only, no SSH).

### GET /api/service

**Deprecated** (`Deprecation: true` header) alias of the `/api/meta`
capabilities block. Tier L. Query: rejected. Returns
`{"restartSupported": bool}`. Use `GET /api/meta` instead.

### GET /healthz

Liveness. Tier L. Always `200` while the process serves requests.

```json
{"status": "ok", "ready": true, "transportRetries": 4}
```

`transportRetries` counts stale-SSH-transport retries since process start.

### GET /readyz

Readiness. Tier L. `200` when at least one target exists and at least one
host has a successful sample; otherwise `503` with the same body shape:

| Field | Type | Description |
|---|---|---|
| `status` | string | `ready` or `not_ready`. |
| `ready` | bool | Readiness verdict. |
| `reason` | string \| null | `host discovery failed`, `no monitoring targets discovered`, or `waiting for first successful collection`. |
| `targets` | int | Discovered monitoring targets. |
| `targetsWithSuccessfulSample` | int | Targets with at least one success. |
| `transportRetries` | int | Cumulative transport retries. |
| `version` | int | Current state revision. |
| `startedAt` | timestamp | Process start. |

### GET /metrics

OpenMetrics 1.0 exposition of the current in-memory snapshot
(`application/openmetrics-text; version=1.0.0; charset=utf-8`). Tier L.
Query: rejected. Never triggers collection. See the
[OpenMetrics reference](#openmetrics-reference).

### GET /api/gpu-history

Per-GPU trend and process events. Tier R.

Query parameters:

| Parameter | Required | Bounds | Default |
|---|---|---|---|
| `host` | yes | safe alias of a current target | — |
| `gpu` | yes | GPU identity, 1–128 visible chars: the device UUID, or `index:N` for UUID-less devices | — |
| `limit` | no | integer 2–300 | 120 |

Response: `{host, gpuId, pollIntervalSeconds, maxPoints, points[],
processEvents[]}`. Points: `observedAt`, `gpuId`, `index`,
`utilizationGpuPct`, `memoryUsedMiB`, `memoryTotalMiB`, `temperatureC`,
`powerDrawW` (all nullable). Process events: `observedAt`, `gpuId`,
`index`, `event` (`started`/`stopped`), `pid`, `name`, `usedMemoryMiB`
(nullable), `workload` (nullable object as in the snapshot).

Errors: `403 UNTRUSTED_ORIGIN`, `UNKNOWN_QUERY_PARAMETER`, `INVALID_QUERY`,
`INVALID_LIMIT`, `404 UNKNOWN_GPU`.

### GET /api/diagnostics

Redacted support bundle. Tier R.

Query parameters: optional `host` (safe alias) narrows the bundle to one
target; `404 UNKNOWN_HOST` if unknown, `400 INVALID_HOST` if malformed.

The bundle anonymizes hosts as `node-001…` and omits UUIDs, process names,
commands, raw errors, and paths. Fields: `schemaVersion`, `generatedAt`,
`appVersion`, `collection{pollIntervalSeconds, collectionStaleAfterSeconds,
lastPollCompletedAt, lastPollDurationMs}`, `persistence`, `notifications`,
`stats`, `servers[]` (aliased, numeric telemetry only), `activeIncidents[]`
(aliased), and a `redaction` manifest stating what was removed.

### GET /api/inventory

Configuration projection. Tier R. Query: rejected.

| Field | Type | Description |
|---|---|---|
| `configuredHosts` | array | Aliases in the configuration's `hosts` list. |
| `activeHosts` | array | Aliases currently being monitored (after exclusion/discovery). |
| `availableHosts` | array | Eligible scanned OpenSSH aliases not currently active (candidates for add). |
| `localHost` | string \| null | The SSH-less local target, if configured. |
| `autoDiscover` | bool | Whether alias discovery is enabled. |
| `ignoredCodeHostCount` | int | Scanned aliases skipped as Git/GitHub/GitLab hosts. |
| `excludedHostCount` | int | Scanned aliases skipped by `exclude_hosts`. |
| `collectorSettings` | object | `{pollIntervalSeconds, probeTimeoutSeconds, connectTimeoutSeconds, maxWorkers}`. **`connectTimeoutSeconds` is read-only context** (the probe timeout must exceed it); the write route rejects it. |
| `maintenanceWindows` | object | **Every configured window**, keyed by alias: `{until, reason, active}` plus `recurring: true` for weekly windows. `active` says whether the window is silencing the host right now; for recurring windows `until` is the end of the current or next instance. |
| `incidentActions` | array | Currently active actions: `{host, condition_key, action, until, reason}` (snake_case `condition_key`). |
| `hostGroups` | object | Alias → group name. |
| `writable` | bool | Whether dashboard writes can persist to the configuration file. |

Errors: `403 UNTRUSTED_ORIGIN`, `503 SERVICE_UNAVAILABLE` (scan failed or
no configuration controller).

### GET /api/topology

Display-only connection tree from the configuration. Tier R. Query:
rejected. Response: `{root, links[]}` where `root` is an alias or `null`
and each link is `{source, target, transport, label?}` with `transport` one
of `ssh`, `frp-stcp`, `frp-xtcp`, `vpn`. Topology aliases never authorize
probing.

### POST /api/settings/collector

Update any non-empty **subset** of the dashboard collector settings. Tier
W. Body cap 512 bytes.

| Body field | Bounds |
|---|---|
| `pollIntervalSeconds` | number, 2–60 |
| `probeTimeoutSeconds` | number, 2–300, must exceed the configured SSH connect timeout |
| `maxWorkers` | integer, 1–64 |

Omitted fields keep their current values. Response `200`:

```json
{"version": 41, "startedAt": "…", "collectionStaleAfterSeconds": 15,
 "collectorSettings": {"pollIntervalSeconds": 5, "probeTimeoutSeconds": 12,
                       "connectTimeoutSeconds": 5, "maxWorkers": 8}}
```

Errors: `INVALID_SCHEMA` (unknown key, empty object, `connectTimeoutSeconds`
included, out-of-range type), `INVALID_SETTINGS` (bounds/cross-field),
`503 SERVICE_UNAVAILABLE`.

### POST /api/settings/poll-interval

**Deprecated** (`Deprecation: true` header) single-field alias of the
collector route. Tier W. Body cap 128 bytes. Body: exactly
`{"pollIntervalSeconds": 2–60}`. Response `200`: `{version, startedAt,
pollIntervalSeconds, collectionStaleAfterSeconds}`. Use
`POST /api/settings/collector` instead.

### POST /api/settings/hosts

Add or remove one monitored host. Tier W. Body cap 512 bytes. Body: exactly
`{"action": "add"|"remove", "host": "<safe alias>"}`.

- `add` requires the alias to be currently *eligible*: present in a fresh
  server-side OpenSSH scan, not excluded, not a recognizable Git host.
- `remove` requires the alias to be in the active inventory; it also clears
  that host's expected GPU count, overrides, maintenance window, actions,
  group, and topology links, and adds it to `exclude_hosts` when discovery
  is on.

Response `200`: the full inventory snapshot (same shape as
`GET /api/inventory`). Errors: `INVALID_SCHEMA`, `409 INVENTORY_CHANGED`
(not eligible / not active / configuration changed underneath — re-read
inventory first), `503 SERVICE_UNAVAILABLE`.

### POST /api/settings/maintenance

Start or clear one maintenance window. Tier W. Body cap 512 bytes. Body:
exactly `{"host", "durationSeconds", "reason"}`.

- `durationSeconds` ∈ {0, 3600, 14400, 86400, 604800}; `0` clears.
- `reason`: required unless clearing; at most 120 visible characters.
- The server computes `until = now + durationSeconds`. **Resubmitting
  extends the window from the new "now"** (see idempotency notes).
- Maintenance silences actionable alerting; collection continues. Weekly
  recurring windows are configured in the JSON file, not through this route.

Response `200`: the full inventory snapshot. Errors: `INVALID_SCHEMA`,
`INVALID_SETTINGS`, `409 INVENTORY_CHANGED` (host not explicitly
configured), `503 SERVICE_UNAVAILABLE`.

### POST /api/settings/host-group

Assign or clear one explicitly configured host's group. Tier W. Body cap
512 bytes. Body: exactly `{"host", "group"}`; empty/whitespace `group`
clears; at most 48 visible characters. Response `200`: the full inventory
snapshot. Errors: `INVALID_SCHEMA`, `INVALID_SETTINGS`,
`409 INVENTORY_CHANGED`, `500 INTERNAL_ERROR`, `503 SERVICE_UNAVAILABLE`.

### POST /api/settings/incident-action

Acknowledge, silence, or clear one active condition. Tier W. Body cap
1024 bytes. Body: exactly `{"host", "conditionKey", "action",
"durationSeconds", "reason"}`.

- `action` ∈ {`acknowledged`, `silenced`, `clear`}.
- `durationSeconds` ∈ {0, 3600, 14400, 86400, 604800}; `0` if and only if
  `action` is `clear`.
- `reason`: at most 120 visible characters (may be empty).
- Acknowledged conditions stay visible and keep recovery notifications;
  silenced conditions suppress new notifications. Both drop the condition
  from `actionable`.

Response `200`: the full inventory snapshot. Errors: `INVALID_SCHEMA`,
`INVALID_SETTINGS`, `409 INVENTORY_CHANGED`, `503 SERVICE_UNAVAILABLE`.

### POST /api/probe

Queue one immediate bounded probe of one host without changing the global
schedule. Tier W. Body cap 512 bytes. Body: exactly `{"host": "<alias>"}`.

Success `202`:

```json
{"status": "queued", "accepted": true, "host": "gpu-node-01"}
```

Non-success responses carry the same status body **plus `code`** (no
`error` key):

| HTTP | `status` | `code` | Notes |
|---|---|---|---|
| 404 | `unknown_host` | `UNKNOWN_HOST` | Not a current target. |
| 409 | `in_progress` | `PROBE_IN_PROGRESS` | A probe is already running for this host. |
| 429 | `rate_limited` | `RATE_LIMITED` | Cooldown active; body has `retryAfterSeconds` and the response carries a `Retry-After` header (integer seconds). |

A duplicate request while one is already queued answers `202` with
`status: "queued"` again (coalesced). Schema and availability failures use
the normal envelope (`INVALID_SCHEMA`, `503 SERVICE_UNAVAILABLE`).

### POST /api/notifications/test

Queue one webhook delivery test to every configured endpoint. Tier W. Body
cap 32 bytes. Body: exactly `{}`. Success `202`: `{"status": "queued"}`.
Errors: `INVALID_SCHEMA`, `503 NOTIFICATIONS_DISABLED` (no webhook
configured), `429 RATE_LIMITED` (test cooldown).

### POST /api/service/restart

Restart the supervised user service. Tier W. Body cap 32 bytes. Body:
exactly `{}`. Success `202`: `{"status": "restarting"}` — the process exits
right after acknowledging and systemd starts the replacement. Available
only when `GET /api/meta` reports `restartSupported: true`; otherwise
`503 SERVICE_UNAVAILABLE`. **Never blind-retry this call** — verify via
`GET /healthz` and the snapshot `startedAt` change (see idempotency notes).

## Agent playbooks

Recommended request sequences for common automation tasks. All of them
honor the viewer semantics: pure diagnostics stay at the L tier without the
marker header.

### 1. Read-only diagnosis (no viewer side effect)

1. `GET /healthz` — process up? `transportRetries` growing fast suggests a
   flaky SSH path.
2. `GET /readyz` — if `503`, the `reason` field distinguishes discovery
   failure, empty inventory, and "no successful sample yet".
3. `GET /api/snapshot` — check `collectorError`, then per host: `status`,
   `stale`, `consecutiveFailures`, `nextRetryAt`, `transportRetried`,
   `message`.
4. `GET /api/incidents` — work through `active` in order (it is sorted
   actionable-first, critical-first); each item ships a `diagnosis` with
   evidence and next steps.
5. Remember `stale: true` means "not online now, showing last-known data" —
   don't read `system`/`gpus` of a stale host as current.

### 2. Enter and clear maintenance

1. `GET /api/inventory` (R) — confirm the alias is in `configuredHosts` and
   check `maintenanceWindows` for an existing window.
2. `POST /api/settings/maintenance` with one of the fixed durations and a
   reason.
3. Verify: the response (an inventory snapshot) shows the window with
   `active: true`; the host's snapshot entry gains `maintenance`.
4. To clear early: same route with `durationSeconds: 0` (idempotent).
5. Do not "refresh" a window on a timer — every accepted call recomputes
   `until` from now. Set the intended duration once.

### 3. Safely add or remove a monitored host

1. `GET /api/inventory` — the only valid add candidates are
   `availableHosts`; the only valid remove candidates are `activeHosts`.
2. `POST /api/settings/hosts` with `{"action": "add"|"remove", "host": …}`.
3. On `409 INVENTORY_CHANGED`: the configuration moved (another writer, or
   the alias stopped being eligible). Re-read the inventory and decide
   again; do not retry the same body blindly.
4. Removal is destructive cleanup (window, overrides, group, topology links
   are dropped with the host) — confirm intent before automating it.

### 4. Tune collection parameters

1. `GET /api/inventory` — read current `collectorSettings`, including the
   read-only `connectTimeoutSeconds` floor.
2. `POST /api/settings/collector` with only the fields you change, e.g.
   `{"probeTimeoutSeconds": 30}`. Keep `probeTimeoutSeconds` above
   `connectTimeoutSeconds` or the call fails with `INVALID_SETTINGS`.
3. Verify the echoed `collectorSettings` in the response; cadence changes
   rebase retry deadlines immediately.

### 5. Probe with backoff

1. `POST /api/probe` with the target host.
2. `202 queued` → poll `GET /api/snapshot` until the host's
   `lastAttemptAt` advances past your request time, then read the result.
3. `429` → sleep for the `Retry-After` header (or body
   `retryAfterSeconds`), then retry once.
4. `409 PROBE_IN_PROGRESS` → a probe is running anyway; just poll the
   snapshot for its result.
5. Never loop faster than the cooldown; the scheduler already probes every
   host at the configured cadence.

### 6. Restart without duplicate submission

1. Read `GET /api/meta` → require `restartSupported: true`.
2. Record the current snapshot `startedAt`.
3. `POST /api/service/restart`. Treat connection drop after send as
   *possibly restarted*, not failed.
4. Poll `GET /healthz` (expect a connection-refused gap), then read the
   snapshot: `startedAt` changed → restart done. `startedAt` unchanged
   after a generous window → the restart did not happen; only then submit
   again.

## OpenMetrics reference

`GET /metrics` renders the current snapshot as OpenMetrics 1.0. Counter
families expose their sample as `<name>_total`. Empty families are omitted
entirely. **Hosts that are not currently online — including `stale` hosts
whose last-known data is still in the JSON snapshot — emit only the
presence/status families below; their current resource and GPU series are
omitted.** Process names and PIDs are never exported.

Collector-level (no labels unless noted):

| Family | Type | Notes |
|---|---|---|
| `mocop_build_info` | gauge | Label `version`. |
| `mocop_collection_ready` | gauge | 1 when a batch completed and no collector error. |
| `mocop_collection_poll_interval_seconds` | gauge | |
| `mocop_collection_duration_seconds` | gauge | Latest completed batch. |
| `mocop_collection_last_completed_timestamp_seconds` | gauge | |
| `mocop_snapshot_generated_timestamp_seconds` | gauge | |
| `mocop_persistence_enabled`, `mocop_persistence_healthy` | gauge | |
| `mocop_persistence_queued_writes` | gauge | |
| `mocop_persistence_dropped_writes` | counter | |
| `mocop_notifications_enabled`, `mocop_notifications_healthy` | gauge | |
| `mocop_notifications_queued_deliveries` | gauge | |
| `mocop_notifications_dropped_deliveries` | counter | |

Cluster aggregates (no labels): `mocop_cluster_servers`,
`mocop_cluster_servers_online`, `mocop_cluster_servers_stale`,
`mocop_cluster_servers_maintenance`, `mocop_cluster_servers_issue`,
`mocop_cluster_servers_actionable_issue`, `mocop_cluster_servers_incident`,
`mocop_cluster_servers_actionable_incident`, `mocop_cluster_gpus`,
`mocop_cluster_gpus_busy`, `mocop_cluster_incidents_active`,
`mocop_cluster_incidents_critical`, `mocop_cluster_incidents_actionable`,
`mocop_cluster_incidents_actionable_critical`,
`mocop_cluster_gpu_memory_total_bytes`, `mocop_cluster_gpu_memory_used_bytes`
(all gauges).

Per-host presence/status (label `host`; emitted for every host):
`mocop_host_info` (extra label `mocop_group`), `mocop_host_up`,
`mocop_host_stale`, `mocop_host_maintenance`, `mocop_host_polling`,
`mocop_host_consecutive_failures`, `mocop_host_incidents_active`,
`mocop_host_incidents_critical`, `mocop_host_incidents_actionable`,
`mocop_host_incidents_actionable_critical`,
`mocop_host_probe_latency_seconds`, `mocop_host_probe_transport_retried`.

Per-host current resources (label `host`; **online, non-stale hosts
only**): `mocop_host_cpu_utilization_ratio`, `mocop_host_load1`,
`mocop_host_uptime_seconds`, `mocop_host_memory_total_bytes`,
`mocop_host_memory_used_bytes`, `mocop_host_swap_total_bytes`,
`mocop_host_swap_used_bytes`, `mocop_host_disk_total_bytes`,
`mocop_host_disk_used_bytes`,
`mocop_host_network_receive_bytes_per_second`,
`mocop_host_network_transmit_bytes_per_second`,
`mocop_host_disk_read_bytes_per_second`,
`mocop_host_disk_write_bytes_per_second`, and — on kernels exposing PSI,
with an extra `resource` label of `cpu`/`memory`/`io` —
`mocop_host_pressure_some_ratio` and `mocop_host_pressure_full_ratio`
(share of the last 10 seconds with at least one task, respectively every
task, stalled on the resource).

Per-GPU (labels `host`, `index`, `uuid`; online, non-stale hosts only):
`mocop_gpu_info` (extra labels `model`, `driver`, `mig_mode`),
`mocop_gpu_utilization_ratio`, `mocop_gpu_memory_total_bytes`,
`mocop_gpu_memory_used_bytes`, `mocop_gpu_memory_free_bytes`,
`mocop_gpu_temperature_celsius`, `mocop_gpu_power_draw_watts`,
`mocop_gpu_power_limit_watts`, `mocop_gpu_processes`,
`mocop_gpu_process_telemetry_available`,
`mocop_gpu_process_telemetry_sampled`,
`mocop_gpu_process_sample_timestamp_seconds`, `mocop_gpu_ecc_uncorrected`,
`mocop_gpu_retired_pages_pending`, `mocop_gpu_remapped_rows_pending`,
`mocop_gpu_thermal_slowdown`, `mocop_gpu_power_brake_slowdown`.

## Safe alias grammar

Every `host` value in queries and write bodies must match:

```text
^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$
```

One leading letter or digit, then up to 252 further letters, digits, dots,
underscores, or hyphens — the same grammar the configuration enforces for
OpenSSH aliases. Values that fail it are rejected before any lookup
(`INVALID_HOST`, `INVALID_QUERY`, or `INVALID_SCHEMA` depending on the
route).
