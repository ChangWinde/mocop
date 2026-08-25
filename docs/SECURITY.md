# Security model

## Threat contract

**Assets and sensitive data:** the dashboard Bearer capability, SSH private keys and
agent capabilities, SSH usernames/addresses in local configuration, the
operator-authored logical connection topology, remote inventory, system/GPU telemetry
including GPU process and optional job/user metadata, optional SQLite history, webhook
URLs and signing secrets, and monitor host availability.

**Actors and privileges:** the local operator can edit the JSON, logical connection
topology, OpenSSH configuration, and private per-install Bearer capability. A client
holding that capability has one operator role: it can read telemetry and, after the
browser-origin checks, persist the narrow collector/inventory/action schemas and
request a restart only when explicitly supervised. Remote SSH servers return
telemetry. There are no application tenants, separate viewer identities, or
role-based permissions. TCP loopback is shared by local Unix users and is not by
itself an authorization boundary.

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
`NoNewPrivileges=true`, restricted address families, `UMask=0077`, and a private
`StateDirectory=mocop`. It intentionally makes no mount-namespace filesystem-isolation
claim in a user manager; private ownership/modes protect configuration and secrets
without breaking required SSH agent or multiplex-socket paths. Installation rejects
symlink, non-regular, foreign-owned, or group/other-accessible capability and optional
environment files before systemd may read them.
Cross-machine migration reads one private, valid source config and creates one
exclusive `0600` target. It refuses overwrite and a pre-existing target capability,
does not read or copy adjacent tokens, environment secrets, SSH material, units, or
history, and preserves automatic host admission unless the operator explicitly
changes it. Old-machine local policy is removed before strict target validation.
Fresh-host `mocop deploy` similarly refuses an existing config, sibling capability, or
service environment. It creates new private configuration before delegating capability,
unit, rollback, and health verification to the existing service lifecycle. It never
modifies SSH material or executes a downloaded bootstrap script.
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

**Required properties:** never put credentials into source artifacts, configuration
JSON, API payloads, persistent browser storage, or logs; keep resolved destinations and raw
connection errors out of browser/support responses; authenticate every telemetry,
metrics, SSE, and write request with exactly one Bearer capability; never accept
command construction or an outbound destination from HTTP; preserve host-key and TLS
verification; reject webhook SSRF by default; bound probe time, output, concurrency,
storage retention/size, background queues, retries, and remote metadata; keep
persistence and notifications out of the collection critical path; listen locally by
default.

**Credible abuse cases:** the existing browser, configuration, SSH-option, remote
output, CSV, and exposed-listener attacks; a remote process supplies excessive or
control-character metadata; SQLite fills the operator's disk or blocks collection; a
webhook URL targets loopback/cloud metadata, changes DNS after validation, stalls a
sender, returns failures to amplify retries, or exposes a secret through configuration
or status output; a capability is copied from its file, shell input, tab session storage,
or an injected same-origin script; a capability holder repeatedly requests service
restarts, probes, or notification tests; a support bundle leaks aliases, GPU UUIDs,
command lines, or raw connection errors.

**Enforcement:** static routes use an exact allowlist. `/api/meta`, `/healthz`, and
`/readyz` are public; every other API-family route and `/metrics` first requires
exactly one valid `Authorization: Bearer` header. Reader routes additionally require a
trusted `Host`, `X-Monitor-Request: dashboard`, and non-cross-site Fetch Metadata when
present. Host and GPU history validate identity grammar, current telemetry membership,
and a 300-point cap; the incident route accepts only one integer capped at 200. The
diagnostic serializer uses an explicit field allowlist, aliases nodes, and omits raw
errors, UUIDs, paths, configuration, processes, and workload identity. The inventory
scan and topology projection use the same read guard. The topology endpoint accepts no
query. Configured topology reads only validated local JSON; opt-in resolved discovery
may populate the shared cache with bounded `ssh -G` processes but never initiates an
SSH connection. It retains only safe aliases, route kinds, and sanitized warnings;
usernames, addresses, and raw proxy commands are discarded. Only the resulting
`HostDiscoverySnapshot.hosts` authorizes a probe. A `host_groups` key alone is
display metadata and never adds an alias to that set. All write routes require
Bearer authentication, an exact queryless path, valid trusted HTTP(S) `Origin`, the
dashboard marker, non-cross-site Fetch Metadata when present, exact JSON media type,
unique keys, exact schema, and a route-specific body cap. The detailed route bounds
and stable errors are the tested contract in [API.md](API.md).

The write guard intentionally does not compare external Origin with a proxy-rewritten
backend Host. Exact `trusted_web_hosts` entries authorize Host and Origin; a leading
`*.` entry authorizes only HTTPS Origins strictly below that DNS suffix and never
relaxes the exact backend Host check. JSON plus the custom header make every POST non-simple, every CORS
preflight is rejected with no `Access-Control-Allow-Origin`, forms cannot add the
marker or required media type, and browser-supplied Fetch Metadata must not be
cross-site. This preserves browser CSRF protection behind a same-origin Host-rewriting
proxy without trusting client-supplied forwarding headers. Non-browser clients can
forge these browser-defense headers but must still possess the Bearer capability.
POST connections close so rejected unread
bytes cannot be reused as another request. Configuration mutation serializes
concurrent changes, reloads the current file, limits the file to 1 MiB, refuses the
bundled template and non-regular or differently owned files, writes a `0600`
same-directory temporary file, validates the complete candidate through the normal
strict loader, fsyncs it, atomically replaces the target, fsyncs the directory, and
then wakes the scheduler with the new immutable configuration. A no-op settings update
does not rewrite the file. A partial temporary file never replaces the active
configuration.

`/metrics` is read-only and requires the same Bearer capability as JSON/SSE telemetry.
It does not accept a target, query, or collection control and never starts remote work.
Operators who expose Mocop beyond loopback must apply authenticated TLS or private-VPN
transport to this route as well.

All remote values—including shared-storage devices, mountpoints and heatmap labels—enter the DOM only through `textContent` or property assignment. Shared-resource grouping and focus filters transform only the in-memory snapshot; they do not construct URLs, commands or HTML. CSV cells are always quoted, embedded quotes are doubled, and values whose trimmed form starts with `=`, `+`, `-` or `@` receive a leading apostrophe before the browser creates a short-lived object URL. OpenMetrics labels escape backslashes, quotes, and newlines; current GPU and system resource series omit stale hosts, and process names/PIDs are not exported. Browser-selected backgrounds accept only PNG, JPEG, WebP or AVIF sources up to 32 MiB, must match the declared container signature, and must decode within 8,192 pixels per side and 32 megapixels. Sources above 8 MiB are locally resized to at most 4,096 pixels per side and 12 megapixels, encoded as a static WebP no larger than 8 MiB, and revalidated before one IndexedDB `Blob` is replaced. SVG and animated formats are rejected before decode. Rendering uses a browser-generated, revocable `blob:` URL; CSP grants `blob:` only to `img-src`, and no upload endpoint exists.

Aliases pass a strict grammar; remote aliases follow `--`, while the local target uses the constant `sh -s` argv. Both transports receive the same repository-owned fixed script through stdin. A selector drains stdout and stderr incrementally into buffers sharing the configured 64 KiB–16 MiB hard limit; crossing it kills the isolated process group and returns a finite error. The parser accepts only the current `MONITOR_V8` protocol version and rejects everything else, along with incomplete metric sections, conflicting sampled/skipped process states, missing fields, invalid GPU or health values, duplicate health UUIDs, oversized text and more than 256 GPU or health, 1,024 disk, or 4,096 GPU-process records per host. `MONITOR_V8` can explicitly skip the fixed process query between its bounded deadlines; no browser value controls that decision. Base GPU and health fields share one fixed query, but parsing still treats health as additive: malformed or unsupported health fields cannot suppress valid base telemetry, and an unsupported combined query falls back to the fixed base query. Strict host-key checking and batch mode are mandatory for remote targets; configured timeouts, worker bounds and jittered failure backoff isolate slow targets and disperse shared-path retries; security headers include a same-origin CSP. The default loopback bind reduces network reachability, while the Bearer capability isolates private HTTP surfaces from unrelated local users. Remote exposure requires authenticated TLS proxy/VPN controls.

Optional workload records are capped at the GPU-process limit, accept only the
`process`, `slurm`, `kubernetes`, `docker`, and `podman` kinds, and bound every
field. Container IDs are accepted only from anchored Docker/Podman cgroup segments
with 12–64 lowercase hexadecimal characters and are truncated to the conventional
12-character display form. The fixed script
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
account and state directory. Webhook delivery is not a persistent outbox. Separate
roles, per-person identity, revocation lists, and multi-tenant authorization remain out
of scope: one bearer capability grants the complete operator surface.

Opt-in self-update ([ADR-0026](adr/0026-dashboard-self-update.md)) is the only
non-SSH, non-webhook outbound surface, and only when `updates.mode` is not
`off`. It polls one hardcoded official repository over HTTPS with bounded
response sizes; the browser can trigger only a fixed empty writer-tier POST
and can never name a version, repository, or installer option. Apply installs
the exact `mocop-<version>-py3-none-any.whl` release asset only after its
SHA-256 matches the release manifest, uses wheels exclusively so no downloaded
code executes during installation, verifies the installed version through the
target interpreter, and triggers the ADR-0012 supervised restart only on a
verified match — a failed attempt reports its state and the running process
keeps serving. GitHub and its release infrastructure become trusted parties
exactly when an operator enables the mode.

## Secret handling

The process inherits the operator's SSH environment so OpenSSH can use ssh-agent. It
never opens private-key files itself. Raw SSH stderr is classified locally and is not
stored or emitted. The managed dashboard capability is an owner-only regular file
beside the selected configuration. The browser removes it from the URL fragment and
keeps it in tab-scoped `sessionStorage` so an intentional reload remains authenticated;
it is never stored in a cookie, persistent `localStorage`, or IndexedDB. Closing the
tab ends that browser session. Webhook URLs and signing secrets are read from environment variables;
for the generated service, place them in the optional private `environment` file next
to `config.json`. They never enter the config API, snapshot, status, or logs.

If the dashboard starts without a fragment or stored capability, it accepts the
same token through a non-dismissible form and retains it only after successful
authentication. Invalid tokens are cleared and are not retried. A reverse proxy
that terminates TLS can observe subsequent Bearer headers and is therefore part of
the trusted computing base; do not expose Mocop through an untrusted forwarding
service.

`mocop doctor` is read-only: it resolves aliases with `ssh -G`, optionally runs the
bounded non-interactive probe command, and never writes SSH configuration or keys.
Connection tests follow the operator's existing OpenSSH policy, so a configured
`ControlMaster auto` may create its usual control socket exactly as any probe would.
Its report names aliases and local socket directories only — never remote
usernames, addresses, or raw stderr. When OpenSSH connection multiplexing is enabled,
the `ControlPath` directory must be owned by the operator with mode `0700`; a
group- or world-accessible socket directory lets another local account hijack the
multiplexed session, and doctor flags it.

## Deployment requirement

Changing `listen_host` away from loopback is a security-sensitive deployment
decision. Bearer authentication is still required but does not encrypt plain HTTP or
authenticate the server. Put the service behind TLS plus authenticated authorization
(or a private VPN), restrict source networks, and do not forward `/api/events`
anonymously. Capability rotation, viewer access review, proxy configuration, upgrade,
and rollback are deployment responsibilities; see [OPERATIONS.md](OPERATIONS.md) and
[ADR-0017](adr/0017-per-install-dashboard-capability.md).
