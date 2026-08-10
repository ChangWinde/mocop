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
- GPU capacity matching, scheduling heatmap, host groups, search, filters, and CSV export
- CPU, load, memory, swap, disk capacity and I/O, network rate, and uptime
- Incidents with anti-flap thresholds, stale-data handling, failure backoff, and timed maintenance windows
- Config-backed host inventory, expected GPU counts, local-host collection, and host groups
- Five browser-local themes, compact mode, saved ordering, and validated local backgrounds
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

Keep `ProxyJump`, ports, users, and identities in OpenSSH. Do not add jump hosts or Git remotes to Mocop's monitored `hosts` list.

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
- `host_overrides` changes cadence or timeout for a measured slow node.

See the [complete example](examples/mocop.example.json) for all fields and safe bounds. Restart the service after editing JSON manually. Changes made in the dashboard are validated, written atomically, and applied without a restart.

### What is persisted

| Setting | Storage | Survives service restart | Shared across browsers |
|---|---|---:|---:|
| Collection interval, probe timeout, worker count | `config.json` | Yes | Yes |
| Monitored hosts, groups, maintenance windows | `config.json` | Yes | Yes |
| Theme, density, sorting, filters, visible GPU columns | browser `localStorage` | Yes | No |
| Custom background | browser `IndexedDB` | Yes | No |

Browser settings are lost only when that browser's site data is cleared or its display preferences are reset. Removing a custom background is a separate action.

The dashboard allows a 2–60 second interval, a 2–300 second probe timeout, and 1–64 workers. Short intervals and high concurrency increase SSH and remote-host load.

## Daily use

- Select a GPU row or heatmap cell to inspect its processes and per-process VRAM.
- Use **Match capacity** to find same-host, same-model GPUs with enough free VRAM. The result is not a reservation.
- Set a maintenance window to silence actionable alerts while collection continues.
- Scan SSH aliases in **Settings → Monitored nodes** to add or remove eligible compute nodes.
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

The endpoint includes collection health, host availability, incidents, system resources, and current GPU metrics. Stale resource values, process names, and PIDs are not exported.

## Failure behavior

Mocop publishes healthy hosts without waiting for slow hosts. Failed hosts keep their last successful sample but are marked stale and excluded from current cluster totals. Repeated failures back off for up to 60 seconds.

The collection interval is the target cadence, not a guarantee that every full-cluster cycle finishes within that time. A connection timeout can make the cycle duration longer while completed hosts continue to update through SSE.

Test the same SSH path outside Mocop before changing timeouts:

```bash
ssh -o BatchMode=yes gpu-node-01 true
ssh -G gpu-node-01 | grep -E '^(hostname|port|user|proxyjump|controlmaster) '
```

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
