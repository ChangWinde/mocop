# Configuration reference

Mocop reads one strict UTF-8 JSON object. Unknown keys, duplicate JSON keys,
non-finite numbers, wrong types, and values outside the boundaries below fail
startup. The file is limited to 1 MiB. Run `mocop config check` after every
manual change; it validates without opening SSH connections.

## Resolution and ownership

The first applicable source wins:

1. `--config PATH`
2. `MOCOP_CONFIG`
3. `${XDG_CONFIG_HOME:-~/.config}/mocop/config.json`, when it exists
4. `./config/mocop.json`, for a source checkout
5. the bundled empty default

`mocop deploy`, `mocop init`, and `mocop service install` manage a real file, not a symlink.
The managed file must be owned by the invoking user and inaccessible to group
and other users; generated and rewritten files use mode `0600`. Its directory
must be owned by that user and not group/other writable. The service also
validates the optional sibling `environment` and `access-token` files as private
regular files. Dashboard writes are atomic same-directory replacements.
Fresh servers should use `mocop deploy`; it creates this file with local collection,
automatic SSH alias admission, and topology discovery before installing the user service.
Cross-machine moves should use `mocop migrate --from-config SOURCE`; it creates a
new private file, rebinds machine identity, and never copies adjacent credentials or
state. See the [operations runbook](OPERATIONS.md#cross-machine-migration).

Host aliases use `[A-Za-z0-9][A-Za-z0-9._-]{0,252}`. Fields described as a
“safe alias” use that grammar. An explicit host referenced by another field
must be in `hosts` and not in `exclude_hosts`. `host_groups` is the exception
when `auto_discover` is enabled: it may predeclare safe discovered aliases but
does not make those aliases active probe targets.

## Top-level fields

The ten fields marked required must be present, even when empty.

| Field | Required | Type/default | Boundary and meaning |
|---|---:|---|---|
| `ssh_config` | yes | non-empty string | OpenSSH config path; `~` expands, relative paths resolve beside the JSON file, and control/surrogate characters are rejected. |
| `auto_discover` | yes | boolean | Add eligible literal aliases discovered in `ssh_config`; `exclude_hosts` still wins. |
| `ssh_discovery` | no | omitted ⇒ `aliases` mode | `{mode, refresh_seconds, resolve_timeout_seconds}`. `mode` is `aliases` (alias-only scan) or `topology` (resolved routes; see below). Files written by `init`, `deploy`, and `migrate` set `topology`. |
| `hosts` | yes | string array | Explicit allowlist; unique after trimming, at most 1,024 safe aliases. |
| `exclude_hosts` | yes | string array | Deny-list; unique after trimming, at most 1,024 safe aliases. |
| `poll_interval_seconds` | yes | number | 1–3,600. Dashboard writes intentionally narrow this to 2–60. |
| `probe_timeout_seconds` | yes | number | 2–300 and strictly greater than `connect_timeout_seconds`. |
| `connect_timeout_seconds` | yes | integer | 1–120; dashboard displays but cannot change it. |
| `max_workers` | yes | integer | 1–64. |
| `listen_host` | yes | hostname/IP | Plain DNS name, IPv4, or IPv6; no scheme, credentials, path, or query. |
| `listen_port` | yes | integer | 1–65,535. |
| `local_host` | no | `null` | Safe alias in the explicit active `hosts` list; runs the fixed probe locally. |
| `trusted_web_hosts` | no | `[]` | At most 32 exact hostnames/IP literals or HTTPS-only origin suffixes such as `*.preview.example`; no scheme or port. Exact entries authorize browser Host/Origin, while suffix entries authorize Origin only. This is not authentication. |
| `gpu_process_poll_interval_seconds` | no | `15` | Number 2–3,600; independent process-query cadence. |
| `retry_jitter_pct` | no | `15` | Number 0–50. |
| `manual_probe_cooldown_seconds` | no | `5` | Number 1–300. |
| `max_output_bytes` | no | `2097152` | Integer 65,536–16,777,216; shared stdout/stderr probe cap. |
| `history_points` | no | `720` | Integer 12–8,640 per-host in-memory points. |
| `incident_history_points` | no | `500` | Integer 20–5,000 transitions. |
| `collection_stale_cycles` | no | `3` | Integer 2–12. |
| `thresholds` | no | defaults below | Exact optional threshold object. |
| `expected_gpu_counts` | no | `{}` | Active explicit alias → integer 0–256. |
| `incidents` | no | defaults below | Exact optional stability-cycle object. |
| `host_overrides` | no | `{}` | Active explicit alias → non-empty override object. |
| `maintenance_windows` | no | `{}` | Active explicit alias → one-shot or recurring UTC window. |
| `host_groups` | no | `{}` | Active alias → 1–48 visible characters; auto-discovery mode permits inert predeclared aliases. |
| `topology` | no | absent | Validated display-only tree; never authorizes a probe. |
| `persistence` | no | disabled | Bounded SQLite history settings. |
| `workloads` | no | disabled | Remote process metadata tier. |
| `webhooks` | no | `[]` | At most 16 HTTPS endpoint definitions using environment-variable names. |
| `incident_actions` | no | `[]` | At most 512 durable acknowledgement/silence records. Usually UI-managed. |
| `incident_overrides` | no | `{}` | Per-active-host/per-configured-group thresholds and disk exclusions. |
| `updates` | no | `{mode: "off"}` | Release-currency policy: `{mode, check_interval_seconds}`. `mode` is `off` (default: no release checks, no outbound requests), `check` (poll and display), or `self-update` (also allow the dashboard's one-click apply); interval 3,600–86,400 seconds, default 21,600. The poll target is the hardcoded official repository ([ADR-0026](adr/0026-dashboard-self-update.md)); no configuration value can change the update source. |

## Thresholds and incident stability

All threshold fields are numbers. Defaults and inclusive ranges are:

| `thresholds` field | Default | Range |
|---|---:|---:|
| `cpu_warning_pct` | 85 | 0–100 |
| `memory_warning_pct` | 90 | 0–100 |
| `swap_warning_pct` | 50 | 0–100 |
| `disk_warning_pct` | 85 | 0–100 |
| `disk_min_free_gib` | 5 | 0–1,048,576 |
| `psi_memory_some_pct` | 20 | 0–100 |
| `psi_io_some_pct` | 30 | 0–100 |
| `gpu_temperature_warning_c` | 80 | 0–150 |
| `gpu_busy_pct` | 10 | 0–100 |
| `gpu_memory_warning_pct` | 90 | 0–100 |
| `gpu_idle_memory_pct` | 20 | 0–100 |

`incidents.resource_open_cycles` (2), `recovery_cycles` (2), and
`gpu_idle_memory_cycles` (12) are integers from 1 through 60.

`incident_overrides` may contain only `hosts` and `groups`, each with at most
256 entries. A scope object is non-empty and may contain `thresholds` and/or
`exclude_disk_mounts`. Scoped numeric values are 0–100, except GPU temperature
which is 0–150. Disk exclusions contain at most 128 unique absolute paths, each
at most 512 characters. Host overrides take precedence over group overrides.

## Host policy and maintenance

Each `host_overrides.<alias>` object is non-empty and accepts only:

| Field | Boundary |
|---|---|
| `poll_interval_seconds` | number 1–3,600 |
| `probe_timeout_seconds` | number 2–300 and greater than the global connection timeout |
| `display_name` | 1–64 visible characters |

A maintenance window contains optional `reason` (at most 120 visible
characters) and exactly one of:

- `until`: strict UTC `YYYY-MM-DDTHH:MM:SSZ` timestamp.
- `recurrence`: exactly `{weekday, start, duration_minutes}`, where weekday is
  integer 0–6 (Monday–Sunday), start is `HH:MM` UTC, and duration is integer
  1–10,079 minutes (strictly less than one week).

An `incident_actions` item has `host`, `condition_key`, `action`, `until`, and
`reason`, plus optional `incident_started_at`; no other shape is accepted. The
host is active, condition key is 1–512 characters, action is `acknowledged` or
`silenced`, `until` uses the strict UTC form above, and reason is at most 120
visible characters. When non-null, `incident_started_at` uses the same timestamp
form and binds the action to one incident
generation so a later recurrence cannot inherit a stale acknowledgement. Legacy
records without it remain readable. Only one item per host/condition pair is
accepted; the dashboard normally owns these records.

Current active state is deliberately re-established from live probes after a
service restart, not trusted from historical events. A pre-existing bound action
may therefore bind once to the first matching condition confirmed after startup;
an initial healthy sample consumes that allowance, and any later recurrence is
actionable. This preserves a continuous outage across a supervised restart while
failing open if recovery happened during downtime.

## Topology

`ssh_discovery.mode: "aliases"` preserves literal-alias discovery. In
`"topology"` mode Mocop periodically runs bounded `ssh -G` resolution without
opening an SSH connection. Aliases referenced by effective `ProxyJump` or a
recognizable SSH-backed `ProxyCommand` become infrastructure and are not admitted
by automatic discovery. Explicit active `hosts` entries still win. The closest
resolved proxy alias becomes the inferred group. Direct targets sharing the same
numbered alias prefix form a fallback group; explicit `host_groups` wins.
Opaque proxy commands are represented by a non-sensitive synthetic node and
their command/address text is discarded.

`ssh_discovery.refresh_seconds` is an integer from 30–3,600 (default 300), and
`resolve_timeout_seconds` is a finite number from 1–30 (default 3) per alias.
Omitting `ssh_discovery` preserves the pre-discovery `aliases` mode for upgrade
compatibility. A configured `topology` remains authoritative over the generated
tree.

`topology` contains exactly `root` and `links`. Root and endpoints are safe
aliases, but topology-only aliases need not be monitoring targets. There are at
most 512 links. Every link has `source`, `target`, and `transport`; optional
`label` is 1–64 visible characters. Transport is one of `ssh`, `frp-stcp`,
`frp-xtcp`, or `vpn`. Self-links, multiple parents, an incoming root link,
cycles, and nodes unreachable from root are rejected.

## Persistence and workloads

| Field | Default | Boundary |
|---|---:|---|
| `persistence.enabled` | `false` | boolean |
| `persistence.retention_hours` | 168 | integer 1–8,760 |
| `persistence.max_bytes` | 134,217,728 | integer 8,388,608–1,073,741,824 |
| `workloads.mode` | `disabled` | `disabled`, `identity`, or `auto` |

The byte limit caps the SQLite database file, not its temporary rollback
journal. Rows older than `retention_hours` are deleted every 60 seconds and
the pages they occupied are returned to the filesystem — up to 8 MiB per cycle
while the service runs, and completely at startup, where a file with free pages
left over (after a long downtime, or when `max_bytes` was lowered) is rebuilt
with `VACUUM` before the cap is checked. That rebuild takes well under a second
per hundred megabytes of live data and needs temporary disk space up to the
file's size; lowering `max_bytes` therefore takes effect at the next start as
long as the live data fits. `identity` reads bounded UID/start/command metadata; `auto` additionally
classifies supported scheduler/container contexts. Neither mode executes a
scheduler client.

## Webhooks and secrets

Each webhook accepts only these fields:

| Field | Required/default | Boundary |
|---|---|---|
| `name` | required | 1–48 characters, safe alias grammar |
| `url_env` | required | environment name `[A-Z_][A-Z0-9_]{0,127}` |
| `secret_env` | `null` | null or environment name |
| `events` | all four | unique non-empty subset of `opened`, `resolved`, `escalated`, `deescalated` |
| `timeout_seconds` | 5 | number 0.5–30 |
| `max_attempts` | 3 | integer 1–8 |
| `retry_base_seconds` | 1 | number 0.1–60 |
| `min_interval_seconds` | 1 | number 0–300 |
| `allow_private_networks` | `false` | boolean; explicit SSRF-sensitive opt-in |

JSON stores environment-variable names, never destinations or signing secrets.
For the managed service, put `NAME=value` lines in the private `environment`
file beside `config.json`, rerun `mocop service install`, and verify status.
Webhook URLs must be credential-free HTTPS URLs; non-global destinations require
the explicit private-network opt-in.

## Related references

- [Operations, upgrade, and rollback](OPERATIONS.md)
- [HTTP API and access tiers](API.md)
- [Security model](SECURITY.md)
- [Complete publication-safe example](../examples/mocop.example.json)
