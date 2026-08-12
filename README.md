<p align="center">
  <img src="mocop/static/favicon.svg" width="88" height="88" alt="Mocop logo">
</p>

<h1 align="center">Mocop</h1>

<p align="center">AI-native GPU cluster monitoring over OpenSSH</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml"><img src="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6d8cff" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/runtime_dependencies-0-55d6a5" alt="Zero runtime dependencies">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#failure-behavior">Troubleshooting</a> ·
  <a href="#security">Security</a>
</p>

![Mocop dashboard with fictional cluster data](docs/assets/dashboard.png)

Mocop is a local web dashboard for NVIDIA GPU clusters. It uses existing OpenSSH aliases to collect GPU, CPU, memory, swap, disk, and network data, then streams each host result to the browser as soon as it completes.

Remote hosts need no agent, database, Python installation, or monitoring port. They need Linux `/proc` and `nvidia-smi` for NVIDIA GPU data. Mocop itself uses the Python standard library and the system OpenSSH client.

Here, **AI-native** means that the interface is built around GPU capacity, task placement, and failure diagnosis. Mocop does not call an AI service or upload telemetry.

## Features

- GPU utilization, VRAM, temperature, power, model, driver, hardware health, and per-GPU processes
- GPU capacity matching, scheduling heatmap, connection map, search, filters, and CSV export
- CPU, load, memory, swap, disk capacity and I/O, network rate, and uptime
- Incidents with diagnosis, acknowledgement/silence, scoped thresholds, anti-flap handling, and timed maintenance
- Independent per-host scheduling, possible shared-path grouping, and optional HTTPS webhooks
- Config-backed host inventory, expected GPU counts, local-host collection, and host groups
- Per-GPU trends and process timelines, with optional bounded SQLite retention and read-only Slurm/Kubernetes context
- Six visual styles, six independent accents, compact mode, saved ordering, and validated local backgrounds
- OpenMetrics 1.0 endpoint for Prometheus and Grafana

## Quick start

Mocop requires Linux, Python 3.10 or newer, OpenSSH, and non-interactive SSH access to each remote node. Verify host fingerprints manually before enabling unattended collection.

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

### 3. Start the dashboard

```bash
mocop service install
```

Open <http://127.0.0.1:8787>. The command installs, enables, and starts a user-level systemd service. It does not change the system linger policy.

Run `mocop` for a foreground process, or manage the service with:

```bash
mocop service status
mocop service uninstall
```

## Configuration

The generated file is complete. Edit it directly only when you need fields not exposed in the dashboard. This excerpt shows the main inventory fields:

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

- `hosts` is the explicit allowlist. `exclude_hosts` always wins.
- `local_host` names one entry in `hosts` that should be probed without SSH.
- `expected_gpu_counts` reports missing devices.
- `host_groups` provides shared navigation groups.
- `host_overrides` changes cadence or timeout for a measured slow node, and its optional `display_name` gives an alias a human-readable fleet label without changing collection identity.
- `maintenance_windows` entries define either a one-shot `until` or a weekly `recurrence` (`{"weekday": 0-6, "start": "HH:MM", "duration_minutes": N}`, all in UTC, Monday is 0); recurring windows silence actionable alerts during every instance while collection continues.
- `gpu_process_poll_interval_seconds` controls timestamped GPU task refresh independently; the 15-second default reduces NVIDIA command overhead while core GPU data keeps the normal cadence.
- `incident_overrides` applies bounded host/group thresholds and exact disk-mount exclusions; host settings take precedence.
- `incident_actions` stores dashboard acknowledgements and silences with UTC expiry. It is maintained by the UI in normal use.
- `manual_probe_cooldown_seconds` bounds repeated on-demand probes of one node; the default is 5 seconds.
- `retry_jitter_pct` disperses retries after a shared SSH path fails; the default is 15%.
- `topology` describes the connection tree. Its safe aliases are display-only unless they also appear in the active `hosts` inventory.
- `persistence.enabled` retains bounded trends and incident context in SQLite; it is off by default.
- `workloads.mode: "auto"` adds best-effort Slurm/Kubernetes identity from bounded `/proc` reads; it is off by default.

Webhook destinations and signing secrets stay out of JSON. A minimal endpoint uses
environment-variable names:

```json
{
  "webhooks": [{
    "name": "operations",
    "url_env": "MOCOP_OPS_WEBHOOK_URL",
    "secret_env": "MOCOP_OPS_WEBHOOK_SECRET"
  }]
}
```

For `mocop service install`, create a private `environment` file beside `config.json`:

```bash
install -m 600 /dev/null ~/.config/mocop/environment
${EDITOR:-vi} ~/.config/mocop/environment
mocop service install
```

Add `MOCOP_OPS_WEBHOOK_URL=...` and `MOCOP_OPS_WEBHOOK_SECRET=...` as separate
lines. Use a real secret and protect this file. HTTPS is required; private-network targets
must be explicitly allowed in the endpoint configuration. Delivery covers open,
recovery, severity changes, deduplication, throttling, and bounded retries.

See the [complete example](examples/mocop.example.json) for all fields and safe bounds. Restart the service after editing JSON manually. Changes made in the dashboard are validated, written atomically, and applied without a restart.

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

- Select a GPU row or heatmap cell to inspect current processes, recent utilization/VRAM/temperature/power, and process start/stop events.
- Open an incident for evidence-based guidance, then acknowledge it or silence only that condition for a fixed period.
- Use **Probe now** on the selected node to advance one bounded collection without changing the global interval.
- Use **Match capacity** to find same-host, same-model GPUs with enough free VRAM. The result is not a reservation.
- Set a maintenance window to silence actionable alerts while collection continues.
- Scan SSH aliases in **Settings → Monitored nodes** to add or remove eligible compute nodes.
- After an upgrade, use **Settings → Service status → Restart service**. This action is
  available only for the installed user service and reloads the page after recovery.
- Upload a PNG, JPEG, WebP, or AVIF background up to 32 MiB. Sources above 8 MiB are compressed locally; no image is uploaded.
- Export one current snapshot with `mocop --once > snapshot.json`.

## Prometheus

`GET /metrics` exports the current in-memory snapshot in OpenMetrics 1.0 format and does not start another probe:

```yaml
scrape_configs:
  - job_name: mocop
    static_configs:
      - targets: ["127.0.0.1:8787"]
```

The endpoint includes collection and background-subsystem health, host availability, incidents, system resources, and current GPU metrics. Stale resource values, process names, and PIDs are not exported.

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
```

It verifies non-interactive reachability for every monitored alias, measures cold
versus multiplexed connection latency, flags missing connection reuse, a missing
or group-accessible control-socket directory, and an ineffective `ControlPersist`,
and warns when the installed package is newer than the running service. Add
`--profile` to decompose a slow host into transport, fixed-script, and NVIDIA-query
stages. The same checks are possible manually:

```bash
ssh -o BatchMode=yes gpu-node-01 true
ssh -G gpu-node-01 | grep -E '^(controlmaster|controlpath|controlpersist) '
```

Enabling OpenSSH connection reuse removes most per-probe connection cost on remote
routes (measured 76.6% behind a jump host; see
[docs/PERFORMANCE.md](docs/PERFORMANCE.md)):

```sshconfig
Host gpu-node-01
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h:%p
    ControlPersist 600
```

Create `~/.ssh/sockets` yourself with mode `0700`; Mocop never edits SSH configuration.

If several nodes that share one `ProxyJump`, VPN, or FRP route fail together, inspect that shared route first. Restarting Mocop does not repair an unavailable tunnel or remote SSH service.

## Security

Mocop accepts only explicit SSH aliases and runs one fixed, read-only probe. It enforces host-key checking, batch mode, timeouts, output limits, bounded concurrency, private atomic configuration writes, and safe rendering of remote text.

The service has no built-in user accounts and listens on `127.0.0.1` by default. If you expose the dashboard or `/metrics` remotely, place it behind authenticated TLS or a private VPN.

Read the [threat model](docs/SECURITY.md) and [security policy](.github/SECURITY.md) before changing a trust boundary.

## Development

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
uvx --from ruff==0.12.11 ruff check .
uvx --from ruff==0.12.11 ruff format --check .
node --experimental-websocket tests/browser_smoke.mjs
```

See [CONTRIBUTING.md](.github/CONTRIBUTING.md), [architecture](docs/ARCHITECTURE.md), [performance](docs/PERFORMANCE.md), and the [changelog](docs/CHANGELOG.md).

## License

[MIT](LICENSE)
