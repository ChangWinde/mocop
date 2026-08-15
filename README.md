<p align="center">
  <img src="mocop/static/favicon.svg" width="88" height="88" alt="Mocop logo">
</p>

<h1 align="center">Mocop</h1>

<p align="center">AI-native GPU cluster monitoring over OpenSSH</p>

<p align="center">
  <a href="README.md">English</a> · <a href="docs/locales/zh-CN/README.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml"><img src="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6d8cff" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/runtime_dependencies-0-55d6a5" alt="Zero runtime dependencies">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#daily-use">Daily use</a> ·
  <a href="#documentation">Documentation</a> ·
  <a href="#security">Security</a>
</p>

![Mocop dashboard with fictional cluster data](docs/assets/dashboard.png)

Mocop is a local web dashboard for NVIDIA GPU clusters. It uses existing OpenSSH aliases to collect GPU, CPU, memory, swap, disk, and network data, then streams each host result to the browser as soon as it completes.

Remote hosts need no agent, database, Python installation, or monitoring port. They need Linux `/proc` and `nvidia-smi` for NVIDIA GPU data. Mocop itself uses the Python standard library and the system OpenSSH client.

Here, **AI-native** means that the interface is built around GPU capacity, task placement, and failure diagnosis. Mocop does not call an AI service or upload telemetry.

The shipped dashboard UI is currently Simplified Chinese and has no locale
switch yet. The English README, API, operations, and engineering references are
fully maintained.

## At a glance

| Property | Current behavior |
|---|---|
| Deployment | One Python process or a generated user-level systemd service |
| Remote footprint | No agent or open monitoring port; fixed read-only collection over existing OpenSSH aliases |
| Runtime dependencies | Python standard library plus the system `ssh` client |
| Access | Private per-install Bearer capability; loopback by default |
| Update model | Independent per-host collection streamed to the browser over authenticated SSE |
| Retention | In-memory by default; optional bounded private SQLite history |
| Primary workflow | Find a host, GPU, program, owner, workload, incident, or capacity match from one dashboard |

## What you get

- GPU utilization, VRAM, temperature, power, model, driver, hardware health, and
  scan-friendly per-GPU process summaries
- GPU capacity matching, scheduling heatmap, connection map, global/selected-host
  program search, active-process filtering/sorting, attribution filters, and CSV export
- CPU, load, memory, swap, disk capacity and I/O, network rate, uptime, and kernel pressure stall (PSI) telemetry
- Incidents with diagnosis, acknowledgement/silence, scoped thresholds, anti-flap handling, and timed maintenance
- Independent per-host scheduling, possible shared-path grouping, and optional HTTPS webhooks
- Config-backed host inventory, expected GPU counts, local-host collection, and host groups
- Per-GPU trends and process timelines, with optional bounded SQLite retention and read-only Slurm/Kubernetes/Docker/Podman context
- Per-owner GPU occupancy and idle-share rollups over a selectable window (`GET /api/usage` and the owners dialog)
- Six visual styles, six independent accents, compact mode, saved ordering, and validated local backgrounds
- OpenMetrics 1.0 endpoint for Prometheus and Grafana

## Quick start

Mocop requires Linux, Python 3.10 or newer, OpenSSH, and non-interactive SSH access to each remote node. Verify host fingerprints manually before enabling unattended collection.

The steps below install Mocop with [uv](https://docs.astral.sh/uv/). If uv is not installed yet:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Mocop runs as a user-level systemd service. Without lingering, user services stop as soon as you log out; enable lingering once so the monitor survives logouts:

```bash
loginctl enable-linger "$USER"
```

### 1. Configure SSH

Add one explicit alias per compute node to `~/.ssh/config`:

```sshconfig
Host gpu-node-01
    HostName 192.0.2.10
    User cluster-monitor
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

The address above is reserved for documentation. Replace it with your own host, then verify the connection:

```bash
ssh gpu-node-01 true
ssh -o BatchMode=yes gpu-node-01 true
```

Keep `ProxyJump`, ports, users, and identities in OpenSSH. Do not add jump hosts or Git remotes to Mocop's monitored `hosts` list. The optional display-only topology can show jump and FRP nodes without probing them.

### 2. Install and initialize

```bash
uv tool install git+https://github.com/ChangWinde/mocop.git
mocop init --host gpu-node-01 --host gpu-node-02
```

`mocop init` creates `~/.config/mocop/config.json` with mode `0600`. It monitors only the aliases passed with `--host`, disables automatic discovery, and uses a 5-second collection interval.

### 3. Validate the configuration and the SSH path

```bash
mocop config check
mocop doctor
```

`mocop config check` parses and validates the configuration without starting the web server or opening any SSH connection. It reports the resolved config path, host count, persistence/workload/topology state, and each webhook's environment-variable names with their set/unset status — never their values. It exits `0` when the configuration is valid and `2` when it is not.

`mocop doctor` then verifies non-interactive SSH reachability and connection reuse for every monitored alias (exit `0` when every alias is usable, `1` otherwise).

### 4. Start the dashboard

```bash
mocop service install
```

Open the exact `Dashboard:` capability URL printed by the command, for example
`http://127.0.0.1:8787/#access_token=...`. The fragment is not sent over HTTP;
the page removes it immediately and keeps the capability in tab-scoped
`sessionStorage`, so reloads and the managed restart flow remain authenticated.
Closing the tab or opening an independent tab requires the printed URL again. The command installs,
enables, starts, and verifies a user-level systemd service. It does not change
the system linger policy.

Run `mocop` for a foreground process, or manage the service with:

```bash
mocop service status
mocop service uninstall
```

Uninstall stops/disables the service and removes only its generated unit. It
retains the configuration, Bearer token, optional environment file, SQLite
state, browser data, journal, SSH files/control sockets, installed package, and
linger policy. See the [operations runbook](docs/OPERATIONS.md) before upgrade,
rollback, token rotation, or manual cleanup.

## Configuration

The generated file is complete. Edit it directly only when you need fields not
exposed in the dashboard. The [configuration reference](docs/CONFIGURATION.md)
is authoritative for every field, default, relationship, and hard boundary.
This excerpt shows the main inventory fields:

```json
{
  "auto_discover": false,
  "hosts": ["monitor-host", "gpu-node-01", "gpu-node-02"],
  "exclude_hosts": ["gpu-bastion", "git-host"],
  "local_host": "monitor-host",
  "expected_gpu_counts": {
    "gpu-node-01": 8,
    "gpu-node-02": 8
  },
  "host_groups": {
    "gpu-node-01": "training",
    "gpu-node-02": "inference"
  }
}
```

The inventory is explicit: `hosts` allows collection, `exclude_hosts` always
wins, and `local_host` selects at most one allowlisted target for the same fixed
probe without SSH. Expected GPU counts, display groups, per-host cadence, scoped
incident thresholds, maintenance windows, and the display-only connection tree
all remain configuration data rather than remote discovery side effects.

Optional `workloads.mode` values add bounded process ownership, command, start
time, and supported scheduler/container identity. Optional persistence retains
bounded trends and incident context in a private SQLite database; it is disabled
by default. Webhook JSON contains environment-variable names, never destinations
or signing secrets. The [configuration reference](docs/CONFIGURATION.md) owns
all defaults and bounds; the [complete safe example](examples/mocop.example.json)
shows the entire schema, and the [operations runbook](docs/OPERATIONS.md) owns
secret-file setup and restart procedures.

Dashboard changes pass the same strict validator, use private atomic writes, and
apply without a restart. After a manual JSON edit, run `mocop config check` and
reinstall/restart the managed service as described in the operations runbook.

### What is persisted

| Setting | Storage | Survives service restart | Shared across browsers |
|---|---|---:|---:|
| Collection interval, probe timeout, worker count | `config.json` | Yes | Yes |
| Monitored hosts, groups, maintenance and incident actions | `config.json` | Yes | Yes |
| Optional host/GPU trends and process/incident transitions | private SQLite state | Yes | Yes |
| Visual style, accent, density, sorting, filters, visible GPU columns | browser `localStorage` | Yes | No |
| Custom background | browser `IndexedDB` | Yes | No |

Browser settings are lost only when that browser's site data is cleared or its display preferences are reset. Removing a custom background is a separate action.

The dashboard allows a 2–60 second interval, a 2–300 second probe timeout that must exceed the connection timeout (default 5 seconds), and 1–64 workers. Short intervals and high concurrency increase SSH and remote-host load.

## Daily use

- Search by process name, command, PID, owner, workload, queue, host, GPU model,
  or UUID. Search from **All servers** for a fleet-wide result, or select one
  server first to scope results; selecting a process opens its exact GPU and
  carries the query into the per-GPU process filter.
- Scan the main GPU table for process count, the largest known process, allocated
  process VRAM, and sample freshness before opening a device.
- Select a GPU row or heatmap cell to open the process-first workspace. It
  summarizes attribution and known-memory coverage, filters owned or unattributed
  work before the 100-row display limit, and sorts by VRAM, runtime, or program
  name. Owner/workload chips can narrow the device view; quick actions copy a PID
  or command or continue the term in fleet-wide search.
- Open an incident for evidence-based guidance, then acknowledge it or silence only that condition for a fixed period.
- Use **Probe now** on the selected node to advance one bounded collection without changing the global interval.
- Use **Match capacity** to find same-host, same-model GPUs with enough free VRAM. The result is not a reservation.
- Set a maintenance window to silence actionable alerts while collection continues.
- Scan SSH aliases in **Settings → Monitored nodes** to add or remove eligible compute nodes.
- Follow the [upgrade and rollback runbook](docs/OPERATIONS.md). After a verified
  package upgrade, **Settings → Service status → Restart service** is available only
  for the installed user service and reloads the page after recovery.
- Upload a PNG, JPEG, WebP, or AVIF background up to 32 MiB. Sources above 8 MiB are compressed locally; no image is uploaded.
- Export one current snapshot with `mocop --once > snapshot.json`. Add `--strict` in scripts and cron jobs: it exits `1` unless every configured host produced an online sample, and lists the failing hosts on stderr.

## HTTP API

Everything the dashboard shows is also a small JSON API with stable
machine-readable error codes, a public self-describing `GET /api/meta` endpoint,
and P/A/R/W access tiers. Telemetry, SSE, and OpenMetrics require the
per-install Bearer capability; only API discovery and health/readiness are
public. See the [API reference](docs/API.md) for authenticated curl examples,
every endpoint and field, and why non-viewer automation must not send the
`X-Monitor-Request: dashboard` header.

## Prometheus

`GET /metrics` exports the current in-memory snapshot in OpenMetrics 1.0 format and does not start another probe:

```yaml
scrape_configs:
  - job_name: mocop
    authorization:
      type: Bearer
      credentials_file: /home/alice/.config/mocop/access-token
    static_configs:
      - targets: ["127.0.0.1:8787"]
```

Use an absolute credential path; Prometheus must run as an identity permitted to
read that private file, or receive a separately protected copy. The endpoint
includes collection and background-subsystem health, host availability,
incidents, system resources, and current GPU metrics. Stale resource values,
process names, and PIDs are not exported.

## Failure behavior

Mocop schedules each host independently and never overlaps a host with itself. A slow
node does not delay a healthy peer when worker capacity is available. Failed hosts keep
their last successful sample but are marked stale and excluded from current totals;
retries back off for up to 60 seconds with per-host jitter.

The interval is a target cadence. Worker saturation or a probe longer than its interval
can defer that host, but no fleet-wide barrier is introduced.

Diagnose the SSH path with the bundled read-only check before changing timeouts:

```bash
mocop doctor
mocop doctor --profile
mocop doctor --probe
```

The default command verifies non-interactive reachability and connection reuse;
`--profile` separates transport, fixed-script, and NVIDIA-query time; `--probe`
runs one real bounded production collection. Mocop never edits SSH configuration.
See the [performance reference](docs/PERFORMANCE.md) for measured OpenSSH reuse
and the [operations runbook](docs/OPERATIONS.md) for service diagnosis. If several
nodes sharing a jump host, VPN, or FRP route fail together, inspect that route
before restarting Mocop.

### Troubleshooting

Four commands cover most "why is nothing showing up" investigations:

```bash
journalctl --user -u mocop -f              # follow the service logs live
curl -s http://127.0.0.1:8787/healthz      # liveness + cumulative SSH transport retries
curl -s http://127.0.0.1:8787/readyz       # readiness; 503 with a reason until the first successful sample
mocop doctor --probe                       # one real production collection per alias
```

`mocop doctor --probe` reports probe status, latency, GPU/process counts, and
workload coverage per alias. It needs live connection tests and cannot be
combined with `--no-connect`.

### CLI exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Diagnosis or collection failure: `mocop doctor` found at least one unusable alias, or the running monitor's collector or listener failed. |
| `2` | Configuration or usage error: invalid configuration, unknown alias filter, or conflicting flags such as `--probe` with `--no-connect`. |

## Security

Mocop accepts only explicit SSH aliases and runs one fixed, read-only probe. It enforces host-key checking, batch mode, timeouts, output limits, bounded concurrency, private atomic configuration writes, and safe rendering of remote text.

The service has no built-in user accounts and listens on `127.0.0.1` by default.
A private per-install Bearer capability protects telemetry, metrics, SSE, and
writes from unrelated local users, but grants one complete operator role. If you
expose Mocop remotely, use authenticated TLS or a private VPN: a Bearer header
over plain HTTP has no network confidentiality or server authentication.

Read the [threat model](docs/SECURITY.md) and [security policy](.github/SECURITY.md) before changing a trust boundary.

## Documentation

Use the [documentation portal](docs/README.md) for the complete audience map,
canonical ownership, update triggers, language policy, and ADR lifecycle.

| Task | Document |
|---|---|
| Configure a fleet | [Configuration reference](docs/CONFIGURATION.md) |
| Operate, upgrade, back up, roll back, or uninstall | [Operations runbook](docs/OPERATIONS.md) |
| Build an API client or Prometheus integration | [HTTP API](docs/API.md) |
| Review trust and deployment boundaries | [Security model](docs/SECURITY.md) |
| Understand components and decisions | [Architecture](docs/ARCHITECTURE.md) and [ADR index](docs/adr/README.md) |
| Reproduce performance claims | [Performance](docs/PERFORMANCE.md) |
| Review current quality and resource evidence | [Quality assessment](docs/QUALITY.md) |
| Review user-visible changes | [Changelog](docs/CHANGELOG.md) |

## Development

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
uvx --from ruff==0.12.11 ruff check .
uvx --from ruff==0.12.11 ruff format --check .
node --experimental-websocket tests/browser_smoke.mjs
```

See [CONTRIBUTING.md](.github/CONTRIBUTING.md) before changing code, tests,
documentation, or public contracts.

## License

[MIT](LICENSE)
