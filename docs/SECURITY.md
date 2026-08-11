# Security model

## Threat contract

**Assets and sensitive data:** SSH private keys and agent capabilities, SSH
usernames/addresses in local configuration, the operator-authored logical connection
topology, remote inventory, system/GPU telemetry including GPU process and optional
job/user metadata, optional SQLite history, webhook URLs and signing secrets, and
monitor host availability.

**Actors and privileges:** the local operator can edit the JSON, logical connection topology, and OpenSSH configuration; a dashboard user can read telemetry and the approved logical topology, persist three bounded collector fields, add or remove only locally discovered SSH aliases, assign a bounded group to an explicit host, set finite maintenance windows, and request a restart only when the process explicitly reports that it is supervised; remote SSH servers return telemetry. There are no application tenants or built-in viewer identities. Because the dashboard has no built-in accounts, access to its loopback listener is the authorization boundary for dashboard-managed configuration changes.

**Entry points and trust boundaries:** operator-owned JSON, OpenSSH files, and optional
service environment variables enter the process; versioned tab-separated system,
GPU, process, health, and workload records enter the collector; HTTP enters a fixed
route table; JSON, SSE, OpenMetrics, and explicitly configured HTTPS webhook bodies
leave the service. Optional SQLite stores successful trends and incident transitions
in the user state directory. One optional `local_host` selects local execution only
after allowlist validation. Workload mode reads `/proc` only for PIDs already returned
by `nvidia-smi`; it does not call a Slurm or Kubernetes API. Topology aliases remain
display/correlation input and never authorize a probe or assert a root cause. Dashboard
writes remain limited to the existing collector, inventory, group, and maintenance
schemas; they cannot set persistence, workload mode, webhook destinations, paths, or
secrets.

**Local lifecycle boundary:** `mocop init` creates a `0600` configuration without
overwrite. Service management writes only the fixed user unit path and invokes
`systemctl --user` with fixed arguments, never a shell. The unit applies
`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, restricted address families,
`UMask=0077`, and a private `StateDirectory=mocop`. `ReadWritePaths` grants only the
configuration directory. If an optional `environment` file exists beside the selected
configuration, installation rejects symlinks, non-regular files, foreign ownership,
or group/other permissions before systemd may read it.
The generated unit marks its process as supervised. In that mode an exact empty JSON
restart request sets a fixed in-process event and exits with status 75 after the HTTP
acknowledgement; systemd's existing failure policy starts the replacement. The web
process never constructs or invokes a lifecycle command. Foreground launches do not
expose this capability. Shutdown cancels child process groups owned by the probe.

**External dependencies and execution environments:** the system `ssh` executable,
local ssh-agent/keys, configured SSH proxies, remote POSIX shell, Linux `/proc`, `df`,
optional `nvidia-smi`, SQLite from the Python standard library, configured HTTPS
webhook services, DNS/TLS infrastructure, and the browser. Exact hardening of SSH
servers, webhook receivers, and reverse proxies is **UNABLE TO DETERMINE** here.

**Required properties:** never put secrets or deployment inventory into source
artifacts, JSON, or browser errors; never accept command construction or an outbound
destination from HTTP; preserve host-key and TLS verification; reject webhook SSRF by
default; bound probe time, output, concurrency, storage retention/size, background
queues, retries, and remote metadata; keep persistence and notifications out of the
collection critical path; listen locally by default.

**Credible abuse cases:** the existing browser, configuration, SSH-option, remote
output, CSV, and exposed-listener attacks; a remote process supplies excessive or
control-character metadata; SQLite fills the operator's disk or blocks collection; a
webhook URL targets loopback/cloud metadata, changes DNS after validation, stalls a
sender, returns failures to amplify retries, or exposes a secret through configuration
or status output; a dashboard reader repeatedly requests service restarts, probes, or
notification tests; a support bundle leaks aliases, GPU UUIDs, command lines, or raw
connection errors.

**Enforcement:** static routes use an exact allowlist; host and GPU history routes validate alias/identity grammar, current telemetry membership, and a 300-point cap; the incident route accepts only one integer capped at 200. GPU history and redacted diagnostics require `X-Monitor-Request: dashboard` and reject cross-site Fetch Metadata. The diagnostic serializer uses an explicit field allowlist, aliases nodes in its output, and omits raw errors, UUIDs, paths, configuration, processes, and workload identity. The inventory scan and topology projection use the same read guard. The topology endpoint accepts no query, reads only the already validated local JSON, starts no process, and returns no resolved OpenSSH field. Topology aliases use the same option-safe grammar as inventory aliases, but only `HostSource.hosts()` can authorize a probe. All write routes require an exact queryless path, a syntactically valid HTTP(S) `Origin`, the same dashboard marker, non-cross-site Fetch Metadata when present, exact JSON media type, unique keys and a route-specific body cap. The cadence schema is one finite non-boolean number from 2 to 60 in at most 128 bytes. The collector schema contains exactly a 2–60 second cadence, a finite 2–300 second complete-probe timeout greater than the configured SSH connection timeout, and integer concurrency from 1 to 64 in at most 512 bytes. Inventory, group, maintenance, and condition-action mutations accept exact bounded schemas and only explicitly monitored aliases. Condition actions use fixed durations and a bounded condition key/reason; expired entries have no effect. A manual-probe request contains one current host alias, enters the existing fixed scheduler, cannot overlap that host, coalesces duplicates, and is rate-limited. A notification test accepts only an empty object and targets only startup-validated configured workers, which also enforce their own test cooldown. The restart schema is exactly an empty object in at most 32 bytes and is unavailable without the supervised-process callback. An add is authorized again against a fresh server-side scan, and recognizable Git/GitHub/GitLab aliases plus `exclude_hosts` entries are ineligible. A removal must match the active inventory; automatic discovery adds a removed alias to the final deny-list. Removal also clears matching expected counts, host overrides, maintenance windows, condition actions, groups, and topology links, and clears `local_host` instead of accidentally converting it into a remote target. Removing the topology root clears the topology rather than selecting another root implicitly.

The write guard intentionally does not compare external Origin with a proxy-rewritten
backend Host. JSON plus the custom header make every POST non-simple, every CORS
preflight is rejected with no `Access-Control-Allow-Origin`, forms cannot add the
marker or required media type, and browser-supplied Fetch Metadata must not be
cross-site. This preserves browser CSRF protection behind a same-origin Host-rewriting
proxy. Non-browser local clients could forge these headers and remain inside the
existing loopback/operator trust boundary. POST connections close so rejected unread
bytes cannot be reused as another request. Configuration mutation serializes
concurrent changes, reloads the current file, limits the file to 1 MiB, refuses the
bundled template and non-regular or differently owned files, writes a `0600`
same-directory temporary file, validates the complete candidate through the normal
strict loader, fsyncs it, atomically replaces the target, fsyncs the directory, and
then wakes the scheduler with the new immutable configuration. A no-op settings update
does not rewrite the file. A partial temporary file never replaces the active
configuration.

`/metrics` is read-only and inherits the same listener-level confidentiality boundary as JSON/SSE telemetry. It does not accept a target, query, or collection control and never starts remote work. Operators who expose Mocop beyond loopback must apply the same authenticated TLS or VPN policy to this route as to the dashboard.

All remote values—including shared-storage devices, mountpoints and heatmap labels—enter the DOM only through `textContent` or property assignment. Shared-resource grouping and focus filters transform only the in-memory snapshot; they do not construct URLs, commands or HTML. CSV cells are always quoted, embedded quotes are doubled, and values whose trimmed form starts with `=`, `+`, `-` or `@` receive a leading apostrophe before the browser creates a short-lived object URL. OpenMetrics labels escape backslashes, quotes, and newlines; current GPU and system resource series omit stale hosts, and process names/PIDs are not exported. Browser-selected backgrounds accept only PNG, JPEG, WebP or AVIF sources up to 32 MiB, must match the declared container signature, and must decode within 8,192 pixels per side and 32 megapixels. Sources above 8 MiB are locally resized to at most 4,096 pixels per side and 12 megapixels, encoded as a static WebP no larger than 8 MiB, and revalidated before one IndexedDB `Blob` is replaced. SVG and animated formats are rejected before decode. Rendering uses a browser-generated, revocable `blob:` URL; CSP grants `blob:` only to `img-src`, and no upload endpoint exists.

Aliases pass a strict grammar; remote aliases follow `--`, while the local target uses the constant `sh -s` argv. Both transports receive the same repository-owned fixed script through stdin. A selector drains stdout and stderr incrementally into buffers sharing the configured 64 KiB–16 MiB hard limit; crossing it kills the isolated process group and returns a finite error. The parser rejects unknown protocol versions, incomplete metric sections, conflicting sampled/skipped process states, missing fields, invalid GPU or health values, duplicate health UUIDs, oversized text and more than 256 GPU or health, 1,024 disk, or 4,096 GPU-process records per host. `MONITOR_V6` can explicitly skip the fixed process query between its bounded deadlines; no browser value controls that decision. Base GPU and health fields share one fixed query, but parsing still treats health as additive: malformed or unsupported health fields cannot suppress valid base telemetry, and an unsupported combined query falls back to the fixed base query. Strict host-key checking and batch mode are mandatory for remote targets; configured timeouts, worker bounds and jittered failure backoff isolate slow targets and disperse shared-path retries; security headers include a same-origin CSP. Network abuse is prevented by the default loopback bind and requires authenticated TLS proxy/VPN controls if the operator changes that default.

Optional workload records are capped at the GPU-process limit, accept only the
`process`, `slurm`, and `kubernetes` kinds, and bound every field. The fixed script
reads at most 16 KiB of cgroup data and 64 KiB of environment data per active GPU PID.
It selects only workload identity fields and never executes a scheduler client.

SQLite is disabled by default. When enabled, its directory and database are `0700` and
`0600`, symlink database paths are rejected, SQL values are parameterized, retention is
enforced at startup and periodically, and `max_page_count` enforces the configured
database-file cap. Collection only uses non-blocking writes to a 4,096-item queue; a
full queue or database error is reported in status rather than delaying a probe. The
SQLite rollback journal can temporarily consume additional bounded transaction space,
so the configured value is a database-file cap rather than a filesystem quota.

Webhook configuration stores environment variable names only. Startup requires a
credential-free HTTPS URL, validates DNS, rejects every non-global address unless
`allow_private_networks` is explicit, and repeats validation immediately before each
request. The TLS connection is pinned to the validated address while retaining SNI and
hostname verification, which closes the normal DNS-rebinding gap. Each endpoint owns a
1,024-item queue, finite timeout, event-ID dedupe window, throttle, and bounded jittered
retry count. Optional request signing is HMAC-SHA256 over canonical JSON. Status exposes
only endpoint names and finite counters, never URLs, secrets, or response bodies.

**Out of scope / assumptions:** configured aliases, explicitly allowed private webhook
networks, and local operator configuration are trusted administrative inputs. A fully
compromised SSH endpoint can consume one probe timeout and output allowance per active
worker. Process and opted-in workload metadata are intentionally visible to dashboard
readers and omitted from OpenMetrics. SQLite is not encrypted at rest; protect the user
account and state directory. Webhook delivery is not a persistent outbox. Multi-user
authorization remains out of scope because the service is local by default.

## Secret handling

The process inherits the operator's SSH environment so OpenSSH can use ssh-agent. It
never opens private-key files itself. Raw SSH stderr is classified locally and is not
stored or emitted. Webhook URLs and signing secrets are read from environment variables;
for the generated service, place them in the optional private `environment` file next
to `config.json`. They never enter the config API, snapshot, status, or logs.

## Deployment requirement

Changing `listen_host` away from loopback is a security-sensitive deployment decision. Put the service behind TLS plus authenticated authorization (or a private VPN), restrict source networks, and do not forward `/api/events` anonymously. Credential rotation, viewer access review and proxy configuration are deployment responsibilities.
