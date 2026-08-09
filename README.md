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
  <a href="#architecture">Architecture</a> ·
  <a href="#security-boundary">Security</a>
</p>

![Mocop dashboard with fictional cluster data](docs/assets/dashboard.png)

Mocop provides a live, GPU-first view of an NVIDIA compute cluster. It collects GPU, CPU, memory, swap, disk, and network metrics through the OpenSSH configuration you already use, then streams each completed host result to a local web dashboard.

Remote machines need no agent, database, Python environment, or open monitoring port. The local runtime uses only the Python standard library and the system OpenSSH client.

In Mocop, AI-native describes the product focus: capacity checks, failure diagnosis, and scheduling decisions for AI training and inference clusters. The collection path does not call an AI API or send telemetry to a third party.

## Why Mocop

- GPU-first dashboard with utilization, VRAM, temperature, power, model, driver, and process state
- Cluster scheduling heatmap and per-host GPU groups that stay collapsed until needed
- CPU, load, memory, swap, filesystem capacity, disk I/O, network throughput, and uptime context
- Search, health filters, sorting, bounded trends, incident history, and safe CSV export
- Per-host publication, bounded concurrency, failure backoff, retry countdowns, and stale-data handling
- Explicit host allowlist, loopback binding, strict host-key checking, fixed remote script, and resource limits

## Requirements

- Linux and Python 3.10 or newer on the Mocop host
- OpenSSH client
- Key-based or `ssh-agent` access that works without an interactive prompt
- Linux `/proc` on monitored hosts
- `nvidia-smi` on hosts where NVIDIA GPU metrics are required

Verify every host fingerprint manually before unattended monitoring creates its first connection.

## Quick start

Install an isolated command with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/ChangWinde/mocop.git
```

Create an explicit inventory and install the user-level systemd service:

```bash
mocop init --host gpu-node-01 --host gpu-node-02
mocop service install
```

Open <http://127.0.0.1:8787>.

`mocop service install` validates the configuration, writes a hardened user unit, enables it, and starts it immediately. Package installation itself does not modify systemd. Use `mocop` for a foreground process when a service is unnecessary.

```bash
mocop service status
mocop service uninstall
```

The service starts with the user's systemd manager. If it must run before that user logs in, an administrator can enable lingering after reviewing how the account stores SSH credentials:

```bash
loginctl enable-linger <user>
```

Mocop never changes the linger policy automatically.

## Common workflows

### Monitor an explicit set of hosts

`mocop init` creates `~/.config/mocop/config.json` with mode `0600` and refuses to overwrite an existing file. Keep automatic discovery disabled and list only the OpenSSH aliases that belong to the monitored cluster:

```json
{
  "auto_discover": false,
  "hosts": ["gpu-node-01", "gpu-node-02"],
  "exclude_hosts": []
}
```

The snippet shows only the inventory fields. Start from the complete, publication-safe [example configuration](examples/mocop.example.json).

### Change the live collection cadence

Use the dashboard selector to change the running process to any interval from 2 to 60 seconds. The control changes the actual SSH scheduler immediately. It does not rewrite the configuration, so a service restart restores `poll_interval_seconds`, whose default is 5 seconds.

### Collect one snapshot

Use one-shot mode for local inspection or a controlled automation pipeline:

```bash
mocop --once > snapshot.json
```

The output contains inventory and telemetry. Store and delete it according to the same policy used for infrastructure logs.

## Dashboard data

| Area | Data |
|---|---|
| GPU | count, utilization, VRAM, temperature, power, model, driver, processes |
| Host | status, CPU, load, memory, swap, disk capacity and I/O, network rate, uptime |
| Cluster | capacity totals, scheduling heatmap, attention queue, health filters, search |
| Operations | bounded trends, state transitions, retry timing, staleness, CSV export |

Failed hosts retain their last successful sample and are marked stale. Stale values remain available for diagnosis but are excluded from current cluster totals.

## Configuration

| Field | Purpose | Range or default |
|---|---|---|
| `hosts` / `exclude_hosts` | OpenSSH alias allowlist and exclusions | empty by default |
| `auto_discover` | discover explicit `Host` aliases from OpenSSH config | `false` |
| `poll_interval_seconds` | collection cadence at process start | 1 to 3600; default 5 |
| `probe_timeout_seconds` | complete collection timeout for one host | 2 to 300 |
| `connect_timeout_seconds` | SSH connection timeout | 1 to 120; less than probe timeout |
| `max_output_bytes` | combined SSH stdout and stderr limit | 64 KiB to 16 MiB |
| `max_workers` | concurrent host probes | 1 to 64 |
| `listen_host` / `listen_port` | dashboard listener | `127.0.0.1:8787` |
| `history_points` | successful samples retained per host | 12 to 8640 |
| `incident_history_points` | state transitions retained in memory | 20 to 5000 |
| `collection_stale_cycles` | delay threshold measured in collection cycles | 2 to 12 |
| `thresholds` | CPU, memory, swap, disk, GPU temperature, and busy thresholds | see example |

Configuration is resolved in this order:

1. `--config`
2. `MOCOP_CONFIG`
3. `$XDG_CONFIG_HOME/mocop/config.json`, or `~/.config/mocop/config.json`
4. `config/mocop.json` in the current directory
5. the bundled safe default with an empty host list and loopback listener

Host aliases may contain letters, numbers, dots, underscores, and hyphens. Restart the service after changing the file:

```bash
systemctl --user restart mocop.service
```

## Architecture

```text
JSON host allowlist ──▶ bounded scheduler ──▶ OpenSSH
                                                │
                              fixed read-only probe
                         /proc · df · nvidia-smi
                                                │
browser ◀── SSE / JSON ◀── bounded in-memory state
```

Each host uses one logical SSH round trip per cycle. Results are published as they complete, so a slow node does not delay a healthy node. Repeated failures back off to at most 60 seconds. Snapshots, trends, and incidents use bounded memory structures and are never persisted by Mocop.

See the [architecture](docs/ARCHITECTURE.md), [performance methodology](docs/PERFORMANCE.md), and [repository layout decision](docs/adr/0001-repository-layout.md) for implementation details.

## Security boundary

The browser cannot add a host or provide a command. Targets come from local configuration, while the remote probe is fixed and versioned. Mocop enforces strict host-key checking, batch mode, timeouts, output limits, concurrency limits, and safe rendering for untrusted remote text.

Mocop has no built-in user accounts and listens on loopback by default. Any remote deployment must add TLS and authenticated authorization through a reverse proxy or VPN.

Read the [threat model](docs/SECURITY.md) before changing a trust boundary. Report vulnerabilities through the process in the [security policy](.github/SECURITY.md).

## Development

```bash
git clone https://github.com/ChangWinde/mocop.git
cd mocop
python3 -m unittest discover -s tests -v
python3 -m compileall -q mocop tests
uvx --from ruff==0.12.11 ruff check .
uvx --from ruff==0.12.11 ruff format --check .
node --check mocop/static/app.js
node --experimental-websocket tests/browser_smoke.mjs
```

CI runs syntax, format, lint, and unit checks on Python 3.10 through 3.14. A separate source-install and headless Chrome job verifies the populated GPU dashboard, collapsed groups, responsive layout, and runtime-cadence race handling.

Read the [contribution guide](.github/CONTRIBUTING.md), [changelog](docs/CHANGELOG.md), and [code of conduct](.github/CODE_OF_CONDUCT.md) before submitting a change.

## License

Mocop is available under the [MIT License](LICENSE).
