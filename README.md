<p align="center">
  <img src="src/mocop/static/favicon.svg" width="88" height="88" alt="Mocop logo">
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

The dashboard UI is currently Simplified Chinese and has no locale switch. The
English README, API, operations, and engineering references remain maintained.

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
- GPU capacity matching with an optional capacity watch that raises an in-page
  banner and an opt-in browser notification when a satisfying idle combination
  appears, plus a scheduling heatmap, connection map, global/selected-host
  program search, active-process filtering/sorting, attribution filters,
  one-click `ssh <alias>` copy, and CSV export
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

Keep `ProxyJump`, `ProxyCommand`, ports, users, and identities in OpenSSH. New configurations use bounded, connection-free `ssh -G` resolution to identify proxy aliases, keep automatically discovered jump hosts out of the probe inventory, build the display topology, and group targets by their closest jump alias. Direct targets sharing a numbered alias prefix (such as `gpu-1` and `gpu-2`) form a fallback group. Explicit hosts, exclusions, groups, and configured topology override inference. Git remotes remain filtered separately.

### 2. Install and deploy

```bash
uv tool install "git+https://github.com/ChangWinde/mocop.git@v0.9.0"
"$(uv tool dir --bin)/mocop" deploy --display-name monitor-0
```

`mocop deploy` needs no inventory JSON on a fresh server: it creates a `0600` config, monitors the current machine locally, discovers safe aliases from `~/.ssh/config`, excludes resolved jump hosts, enables topology grouping, and installs and verifies the user service. The explicit bin path works before a new shell picks up uv's tool directory. The command refuses existing config or capability state; use `mocop service install` for an existing setup.

### 3. Validate the deployment and SSH path

```bash
mocop config check
mocop doctor
```

`mocop config check` parses and validates the configuration without starting the web server or opening any SSH connection. It reports the resolved config path, host count, persistence/workload/topology state, and each webhook's environment-variable names with their set/unset status — never their values. It exits `0` when the configuration is valid and `2` when it is not.

`mocop doctor` then verifies non-interactive SSH reachability and connection reuse for every monitored alias (exit `0` when every alias is usable, `1` otherwise).

### 4. Open the dashboard

Open the exact `Dashboard:` capability URL printed by `mocop deploy`, for example `http://127.0.0.1:8787/#access_token=...`. The fragment is not sent over HTTP;
the page removes it immediately and keeps the capability in tab-scoped
`sessionStorage`, so reloads and the managed restart flow remain authenticated.
Closing the tab or opening an independent tab requires the printed URL again. Deployment installs,
enables, starts, and verifies a user-level systemd service. It does not change
the system linger policy.

A bare dashboard or trusted forwarded URL now opens a token prompt instead of an
empty dashboard. Paste the contents of the managed `access-token` file; Mocop
validates it before storing it in the tab session. Invalid credentials are neither
retained nor retried automatically.

Run `mocop` for a foreground process, or manage the service with:

```bash
mocop service status
mocop service uninstall
```

Uninstall removes only the generated service unit. Read the
[operations runbook](docs/OPERATIONS.md) before cross-machine migration, upgrade,
rollback, token rotation, or cleanup; it covers `mocop migrate`, retained files, and safe backup.

## Configuration

`mocop deploy` writes the fresh-host configuration; `mocop init` is the lower-level configuration-only command. The dashboard safely edits
the common inventory, cadence, maintenance, and grouping settings without a
restart. Use the [configuration reference](docs/CONFIGURATION.md) for every field,
default, and limit; use the [complete safe example](examples/mocop.example.json)
when authoring JSON. Optional workload identity and bounded SQLite history are
disabled by default. After a manual edit, run `mocop config check` and follow the
[operations runbook](docs/OPERATIONS.md) when a service restart is required.

## Daily use

- Search from **All servers** for programs across the fleet, or select a server to
  scope by process, command, PID, owner, workload, queue, host, model, or UUID.
- Scan process count, largest allocation, memory coverage, and freshness in the
  main GPU table; open a GPU for bounded process filters, sorting, and copy actions.
- Open incidents for evidence and acknowledgement/silence, or schedule maintenance
  while collection continues.
- Use **Probe now**, **Match capacity**, and **Settings → Monitored nodes** for the
  common operator workflows. Capacity matches are observations, not reservations.
- Export a snapshot with `mocop --once`; add `--strict` for automation that must
  fail when any configured host has no online sample.

## HTTP API

Everything the dashboard shows is also a small JSON API with stable
machine-readable error codes, a public self-describing `GET /api/meta` endpoint,
and P/A/R/W access tiers. Telemetry, SSE, and OpenMetrics require the
per-install Bearer capability; only API discovery and health/readiness are
public. See the [API reference](docs/API.md) for authenticated curl examples,
every endpoint and field, and why non-viewer automation must not send the
`X-Monitor-Request: dashboard` header.

## Metrics and troubleshooting

Authenticated `GET /metrics` exports the current snapshot as OpenMetrics 1.0
without starting a probe. The [API reference](docs/API.md) owns the Prometheus
configuration and metric contract. These commands cover the first diagnosis:

```bash
journalctl --user -u mocop -f              # follow the service logs live
curl -s http://127.0.0.1:8787/healthz      # liveness + cumulative SSH transport retries
curl -s http://127.0.0.1:8787/readyz       # readiness; 503 with a reason until the first successful sample
mocop doctor --probe                       # one bounded production probe per alias
```

Hosts are scheduled independently; failed samples stay visibly stale and retry
with bounded backoff. See [Operations](docs/OPERATIONS.md) for service recovery and
exit codes, and [Performance](docs/PERFORMANCE.md) before changing cadence or
concurrency.

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
node tests/process_search_test.mjs
node --experimental-websocket tests/browser_smoke.mjs
```

See [CONTRIBUTING.md](.github/CONTRIBUTING.md) before changing code, tests,
documentation, or public contracts.

## License

[MIT](LICENSE)
