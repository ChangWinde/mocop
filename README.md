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

- GPU-first dashboard with utilization, VRAM, temperature, power, model, driver, per-device tasks, and hardware health
- Cluster scheduling heatmap and per-host GPU groups that stay collapsed until needed
- CPU, load, memory, swap, filesystem capacity, disk I/O, network throughput, and uptime context
- Drag-to-order servers, five structurally distinct themes, validated browser-local backgrounds, search, filters, bounded trends, incidents, and safe CSV export
- Dashboard SSH-alias inventory scan with constrained add/remove, Git/GitHub/GitLab filtering, atomic persistence, and live scheduler updates
- Expected GPU inventory, authoritative incidents, anti-flap activation/recovery, failure backoff, and stale-data handling
- Explicit host allowlist, loopback binding, strict host-key checking, fixed remote script, and resource limits

## Requirements

- Linux and Python 3.10 or newer on the Mocop host
- OpenSSH client
- Key-based or `ssh-agent` access that works without an interactive prompt
- Linux `/proc` on monitored hosts
- `nvidia-smi` on hosts where NVIDIA GPU metrics are required
- A user-level systemd manager only when using the optional managed service

Verify every host fingerprint manually before unattended monitoring creates its first connection.

## Quick start

### 1. Create OpenSSH aliases

Mocop monitors aliases, not raw connection strings. Define one explicit alias for each compute node in `~/.ssh/config`:

```sshconfig
Host gpu-node-01
    HostName 192.0.2.10
    User cluster-monitor
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

The address belongs to the documentation-only `192.0.2.0/24` range. Replace every example value with your own environment. If a node requires a jump host, keep that connection detail in OpenSSH:

```sshconfig
Host gpu-bastion
    HostName 192.0.2.5
    User cluster-monitor

Host gpu-node-*
    ProxyJump gpu-bastion
```

Connect once interactively to verify the host fingerprint, then confirm that unattended access works:

```bash
ssh gpu-node-01 true
ssh -o BatchMode=yes gpu-node-01 true
```

The jump host and unrelated aliases such as Git remotes are connection infrastructure, not monitored compute nodes. Do not add them to Mocop's `hosts` list.

### 2. Install Mocop

Install an isolated command with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/ChangWinde/mocop.git
```

### 3. Create the cluster configuration

```bash
mocop init --host gpu-node-01 --host gpu-node-02
```

This creates the complete `~/.config/mocop/config.json` with mode `0600`. Only aliases in `hosts` are monitored. The generated configuration keeps `auto_discover` disabled, so jump hosts, Git aliases, and wildcard SSH entries are not added implicitly. The initial collection cadence is 5 seconds.

Review the [complete example configuration](examples/mocop.example.json) before changing timeouts, concurrency, history limits, or thresholds.

### 4. Start the dashboard

```bash
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

### Change the monitored host set

Open **Settings → Monitored nodes** to scan literal aliases from the configured OpenSSH files. Scanning reads configuration only; it does not connect to a candidate. Recognizable Git, GitHub, and GitLab aliases plus entries in `exclude_hosts` are omitted. An add is accepted only for a fresh scanned alias, and a removal requires a second confirmation. Mocop persists the host list atomically with mode `0600` and updates the running scheduler without a restart.

Use the JSON directly when you also need to maintain expected GPU counts, per-host overrides, or explicit infrastructure exclusions. `mocop init` refuses to overwrite an existing configuration. Keep automatic discovery disabled and list only compute-node aliases:

```json
{
  "auto_discover": false,
  "hosts": ["gpu-node-01", "gpu-node-02"],
  "exclude_hosts": ["gpu-bastion", "git-host"]
}
```

This is an inventory excerpt, not a complete replacement configuration. Keep the other fields generated by `mocop init`. `exclude_hosts` is a final deny-list and is especially useful for jump hosts or custom code-host aliases that do not visibly contain a `git`, `github`, or `gitlab` token. Restart the service after a manual file edit; dashboard inventory changes hot-update the current process.

### Change and persist collection policy

Use the dashboard selector to change the collection cadence to any interval from 2 to 60 seconds. The centered **Settings → Collection policy** workspace also exposes the complete-probe timeout and worker concurrency. These controls validate and atomically update the selected local `config.json`, hot-update the actual scheduler, and are restored on the next service start. The initial default remains 5 seconds.

Shorter cadence, longer timeouts, and higher concurrency all increase collection pressure. Mocop therefore keeps strict bounds and does not expose SSH paths, commands, listeners, thresholds, or arbitrary configuration keys to the browser.

### Detect missing GPUs and noisy resource samples

Declare the expected device count for stable compute nodes and tune incident stability in the configuration:

```json
{
  "expected_gpu_counts": {
    "gpu-node-01": 8,
    "gpu-node-02": 8
  },
  "host_overrides": {
    "gpu-node-02": {
      "poll_interval_seconds": 30,
      "probe_timeout_seconds": 20
    }
  },
  "incidents": {
    "resource_open_cycles": 2,
    "recovery_cycles": 2,
    "gpu_idle_memory_cycles": 12
  }
}
```

Expected-count and override keys must reference active aliases in the explicit `hosts` list. Use a host override only after measuring a node whose own resource query exceeds the fleet timeout: its longer timeout restores complete data, while its slower cadence prevents that expensive probe from running every global cycle. Connectivity and GPU-query loss surface immediately. Resource pressure requires consecutive samples, recovery requires consecutive healthy samples, and idle GPUs retaining significant VRAM use the longer window. This prevents one noisy sample from flooding the incident feed.

### Monitor the machine running Mocop

Give the local machine an inventory alias, add it to `hosts`, and set the same alias as `local_host`:

```json
{
  "hosts": ["monitor-host", "gpu-node-01", "gpu-node-02"],
  "local_host": "monitor-host"
}
```

This is an inventory excerpt. Keep the remaining generated fields. `local_host` does not need an OpenSSH entry: Mocop runs the same fixed, bounded probe locally and bypasses SSH. Only one local alias is supported, and it must also be present in the explicit `hosts` list.

### Personalize the dashboard

Use the centered **Settings** workspace to choose one of five purpose-designed themes, comfortable or compact density, the default fleet focus, server and GPU sorting, the heatmap metric, and optional GPU columns. The glass and terminal themes change geometry, surface treatment, depth and typography rather than only changing colors. You may also choose a PNG, JPEG, WebP or AVIF background up to 8 MiB and control its visibility. Mocop validates its decoded dimensions and stores the image only in this browser; it is never uploaded to the service.

Drag any server row to save a custom order. Display preferences stay in the current browser so different viewers do not overwrite one another; collection policy and monitored nodes are clearly marked as durable local-configuration changes. Select a GPU row or heatmap cell to inspect its active CUDA compute tasks and per-process VRAM. Mocop uses refined local system stacks for interface text and tabular metrics and never downloads a third-party font.

### Collect one snapshot

Use one-shot mode for local inspection or a controlled automation pipeline:

```bash
mocop --once > snapshot.json
```

The output contains inventory and telemetry. Store and delete it according to the same policy used for infrastructure logs.

## Dashboard data

| Area | Data |
|---|---|
| GPU | count, utilization, VRAM, temperature, power, model, driver, tasks, ECC, memory-repair and slowdown state, MIG mode |
| Host | status, CPU, load, memory, swap, disk capacity and I/O, network rate, uptime |
| Cluster | capacity totals, scheduling heatmap, attention queue, health filters, search |
| Operations | bounded trends, state transitions, retry timing, staleness, CSV export |

Failed hosts retain their last successful sample and are marked stale. Stale values remain available for diagnosis but are excluded from current cluster totals.

## Configuration

| Field | Purpose | Range or default |
|---|---|---|
| `hosts` / `exclude_hosts` | OpenSSH alias allowlist and exclusions | empty by default |
| `auto_discover` | discover explicit `Host` aliases from OpenSSH config | `false` |
| `local_host` | optional alias in `hosts` to probe without SSH | `null` |
| `expected_gpu_counts` | expected device count by explicit host alias | empty; 0 to 256 per host |
| `host_overrides` | optional per-host collection cadence and complete-probe timeout | empty; same bounds as global values |
| `poll_interval_seconds` | global collection cadence | 1 to 3600; default 5, dashboard 2 to 60 |
| `probe_timeout_seconds` | complete collection timeout for one host | 2 to 300; dashboard-managed |
| `connect_timeout_seconds` | SSH connection timeout | 1 to 120; less than probe timeout |
| `max_output_bytes` | combined SSH stdout and stderr limit | 64 KiB to 16 MiB |
| `max_workers` | concurrent host probes | 1 to 64; dashboard-managed |
| `listen_host` / `listen_port` | dashboard listener | `127.0.0.1:8787` |
| `history_points` | successful samples retained per host | 12 to 8640 |
| `incident_history_points` | state transitions retained in memory | 20 to 5000 |
| `collection_stale_cycles` | delay threshold measured in collection cycles | 2 to 12 |
| `incidents` | consecutive activation, recovery, and idle-VRAM windows | 1 to 60 cycles |
| `thresholds` | CPU, memory, swap, disk, GPU temperature, utilization, and VRAM thresholds | see example |

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
JSON host allowlist ──▶ bounded scheduler ──┬──▶ OpenSSH
                                           └──▶ local shell
                                                  │
                                         fixed read-only probe
                                    /proc · df · nvidia-smi
                                                  │
browser ◀──── SSE / JSON ◀──── bounded in-memory state
```

Each remote host uses one logical SSH round trip per cycle; the optional local target uses one bounded shell process. Base GPU metrics, compute tasks, and optional hardware-health data are isolated sections in the same fixed probe, so a health-query failure cannot hide system or base GPU telemetry. Results are published as they complete, so a slow node does not delay a healthy node. Repeated failures back off to at most 60 seconds. Snapshots, trends, and incidents use bounded memory structures and are never persisted by Mocop.

See the [architecture](docs/ARCHITECTURE.md), [performance methodology](docs/PERFORMANCE.md), and [repository layout decision](docs/adr/0001-repository-layout.md) for implementation details.

## Security boundary

The browser cannot provide an arbitrary host, command, path, or raw configuration. It may add only a literal alias from a fresh, server-side OpenSSH scan; recognizable Git/GitHub/GitLab aliases and configured exclusions are denied. Its only collection-policy fields are bounded cadence, complete-probe timeout, and worker concurrency. Targets still enter the explicit local JSON allowlist, while the remote probe remains fixed and versioned. Mocop enforces strict host-key checking, batch mode, timeouts, output limits, concurrency limits, atomic private config writes, and safe rendering for untrusted remote text.

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

CI runs syntax, format, lint, and unit checks on Python 3.10 through 3.14. A separate source-install and headless Chrome job verifies the populated GPU dashboard, collapsed groups, centered responsive settings, browser preference persistence, durable collector controls, SSH inventory controls, and cadence/SSE race handling.

Read the [contribution guide](.github/CONTRIBUTING.md), [changelog](docs/CHANGELOG.md), and [code of conduct](.github/CODE_OF_CONDUCT.md) before submitting a change.

## License

Mocop is available under the [MIT License](LICENSE).
