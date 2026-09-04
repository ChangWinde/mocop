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

Remote hosts need no agent, database, Python installation, or monitoring port. They need Linux `/proc`; `nvidia-smi` is required only for NVIDIA GPU data, and a host without it reports its system metrics with GPU status `no_nvidia_smi`. Mocop itself uses the Python standard library and the system OpenSSH client.

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
- GPU capacity matching plus a capacity watch: a banner and opt-in
  notification fire when idle GPUs satisfy it, plus scheduling heatmap,
  connection map, program search, process filters, attribution filters,
  `ssh` copy, and CSV export
- CPU, load, memory, swap, disk capacity and I/O, network rate, uptime, and kernel pressure stall (PSI) telemetry
- Incidents with diagnosis, acknowledgement/silence, scoped thresholds, anti-flap handling, and timed maintenance
- Independent per-host scheduling, possible shared-path grouping, and optional HTTPS webhooks
- Config-backed host inventory, expected GPU counts, local-host collection, and host groups
- Per-GPU trends and process timelines, with optional bounded SQLite retention and read-only Slurm/Kubernetes/Docker/Podman context
- Per-owner GPU occupancy and idle-share rollups over a selectable window
- Six visual styles, six independent accents, compact mode, saved ordering, and validated local backgrounds
- Opt-in release checks and one-click verified self-update
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

Keep `ProxyJump`, `ProxyCommand`, ports, users, and identities in OpenSSH. New configurations resolve aliases with bounded, connection-free `ssh -G` and infer from the result:

- automatically discovered jump hosts stay out of the probe inventory;
- targets are grouped by their closest jump alias, or by a shared numbered prefix (`gpu-1`, `gpu-2`) when direct;
- explicit hosts, exclusions, groups, and a configured topology always override inference; Git remotes are filtered separately.

### 2. Install and deploy

```bash
uv tool install "git+https://github.com/ChangWinde/mocop.git@v0.11.0"
"$(uv tool dir --bin)/mocop" deploy --display-name monitor-0
```

`mocop deploy` needs no inventory JSON on a fresh server: it creates a `0600` config, monitors the current machine locally, discovers safe aliases from `~/.ssh/config`, and installs and verifies the user service. The explicit bin path works before a new shell picks up uv's tool directory; later commands assume a new shell (or `uv tool update-shell`). Deploy refuses an existing `config.json`, `access-token`, or `environment` file — use `mocop service install` for an existing setup — and without a systemd user manager (containers, some WSL setups) run `mocop init` plus a foreground `mocop` instead.

### 3. Validate the deployment and SSH path

```bash
mocop config check
mocop doctor
```

`mocop config check` validates the configuration without starting the web server or opening any SSH connection, reports the resolved path, host count, and persistence/workload/topology/webhook state (never secret values), and exits `0` when valid or `2` when not.

`mocop doctor` then verifies non-interactive SSH reachability and connection reuse for every monitored alias: exit `0` when every alias is usable, `1` when at least one failed, `2` for a configuration or usage error. Add `--json` for a machine-readable report; the [operations runbook](docs/OPERATIONS.md#command-reference-and-exit-codes) lists every command, flag, and exit code.

### 4. Open the dashboard

Open the exact `Dashboard:` capability URL printed by `mocop deploy`, for example `http://127.0.0.1:8787/#access_token=...`. The page keeps the capability in tab-scoped
`sessionStorage`, so reloads stay authenticated; a new tab either needs the printed
URL again or prompts for the contents of the `access-token` file beside the
configuration (`~/.config/mocop/access-token` by default). The
[API reference](docs/API.md#scope-and-compatibility) owns the capability rules.

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
  main GPU table; open a GPU for task rows led by the real entry point (for example
  `train.dragon_video2motion` rather than `python`), environment and footprint
  chips, click-to-expand command lines, bounded filters, sorting, and copy actions.
- Open incidents for evidence and acknowledgement/silence, or schedule maintenance
  while collection continues.
- Use **Probe now**, **Match capacity**, and **Settings → Monitored nodes** for the
  common operator workflows. Capacity matches are observations, not reservations.
- Export a snapshot with `mocop --once`; add `--strict` for automation that must
  fail when any configured host has no online sample.

## HTTP API

Everything the dashboard shows is also a small JSON API with stable error codes
and P/A/R/W access tiers. The public `GET /api/meta` manifest names every
route's tier, query parameters and bounds, body cap, response type, and the
documentation URL for the running release, and a `403` says where the
capability lives, so an agent needs no out-of-band knowledge. Only discovery
and health are public; see the [API reference](docs/API.md) for curl examples
and why non-viewer automation must not send `X-Monitor-Request: dashboard`.

## Metrics and troubleshooting

Authenticated `GET /metrics` exports the current snapshot as OpenMetrics 1.0
without starting a probe; the [API reference](docs/API.md) owns the metric
contract. First diagnosis:

```bash
journalctl --user -u mocop -f              # follow the service logs live
curl -s http://127.0.0.1:8787/healthz      # liveness + cumulative SSH transport retries
curl -s http://127.0.0.1:8787/readyz       # readiness; 503 with a reason until the first successful sample
mocop doctor --probe                       # one bounded production probe per alias
```

Hosts are scheduled independently; failed samples stay visibly stale and retry
with bounded backoff. See [Operations](docs/OPERATIONS.md) for recovery and exit
codes, and [Performance](docs/PERFORMANCE.md) before changing cadence.

## Security

Mocop accepts only explicit SSH aliases and runs one fixed, read-only probe. It enforces host-key checking, batch mode, timeouts, output limits, bounded concurrency, private atomic configuration writes, and safe rendering of remote text.

The service has no user accounts and listens on `127.0.0.1` by default. A
private per-install Bearer capability protects every private route from other
local users but grants one complete operator role. Expose Mocop remotely only
behind authenticated TLS or a private VPN: Bearer over plain HTTP has no
network confidentiality or server authentication.

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
python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

[CONTRIBUTING.md](.github/CONTRIBUTING.md) owns the complete quality-gate list
(lint, coverage, browser leaf contracts, and the real-browser smoke test) and
the commit and documentation rules; read it before changing code, tests,
documentation, or public contracts.

## License

[MIT](LICENSE)
