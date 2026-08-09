<p align="center">
  <img src="mocop/static/favicon.svg" width="88" height="88" alt="Mocop logo">
</p>

<h1 align="center">Mocop</h1>

<p align="center"><strong>AI-native GPU cluster monitor</strong></p>

<p align="center">
  通过 OpenSSH 实时查看整个 GPU 集群。无需远端 Agent，无数据库，无运行时 Python 依赖。
</p>

<p align="center">
  <a href="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml"><img src="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6d8cff" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/runtime_dependencies-0-55d6a5" alt="Zero runtime dependencies">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#配置服务器">配置</a> ·
  <a href="#安全边界">安全</a> ·
  <a href="CONTRIBUTING.md">参与贡献</a>
</p>

![Mocop dashboard with fictional cluster data](docs/assets/dashboard.png)

Mocop 面向 AI 训练与推理集群，把 GPU 容量、显存和调度状态放在页面的第一阅读层级，同时提供 CPU、内存、Swap、磁盘和网络上下文。它复用操作者现有的 OpenSSH 配置，在一次只读 SSH 往返中完成单台服务器采集，并通过 SSE 将结果持续推送到浏览器。

这里的 **AI-native** 指产品围绕 GPU 集群的容量判断、故障定位和调度工作流设计；核心采集不依赖外部 AI API，也不会把遥测发送给第三方。

## 为什么选择 Mocop

- **GPU-first**：数量、利用率、显存、温度、功耗、型号和驱动一屏可见
- **集群视角**：调度热力图、按服务器折叠的 GPU 清单、搜索、筛选、排序和 CSV 导出
- **完整上下文**：CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率和运行时间
- **实时且稳健**：2–60 秒运行时采集频率、SSE 推送、失败退避、重试倒计时和陈旧数据标记
- **低侵入**：远端无需安装 Agent；本地单进程运行，不需要数据库或 CDN
- **安全默认值**：显式主机白名单、回环监听、严格主机密钥校验、固定命令和全面资源上限

## 快速开始

### 1. 准备环境

- Linux 与 Python 3.10+
- OpenSSH 客户端
- 目标主机可通过密钥或 `ssh-agent` 非交互登录
- 目标主机提供 Linux `/proc`；GPU 指标需要 `nvidia-smi`

首次连接目标前，请先在终端人工核对主机指纹并建立 `known_hosts`。

### 2. 从 GitHub 安装

推荐使用 [`uv`](https://docs.astral.sh/uv/) 从公开仓库创建隔离的命令环境，不在当前目录留下构建文件：

```bash
uv tool install git+https://github.com/ChangWinde/mocop.git
```

### 3. 初始化并启动

```bash
mocop init --host gpu-node-01 --host gpu-node-02
mocop service install
```

打开 <http://127.0.0.1:8787>。

`service install` 会校验配置，安装并立即启动当前用户的 systemd 服务，同时设置为该用户登录后自动启动。安装源码本身不会修改 systemd。无需后台服务时可直接运行 `mocop`；一次性采集可运行 `mocop --once`。

```bash
mocop service status
mocop service uninstall
```

卸载服务不会删除配置。确需用户未登录时也随系统开机运行，可由管理员在确认该账号能长期安全使用 SSH 凭据后执行 `loginctl enable-linger <user>`；Mocop 不会自动改变 linger 策略。

## 配置服务器

`mocop init` 默认以 `0600` 权限创建 `~/.config/mocop/config.json`，并拒绝覆盖已有配置。也可以复制完整的 [`config/mocop.example.json`](config/mocop.example.json)。

推荐关闭自动发现，让 `hosts` 成为唯一监控白名单：

```json
{
  "auto_discover": false,
  "hosts": ["gpu-node-01", "gpu-node-02"],
  "exclude_hosts": []
}
```

示例仅展示关键字段；配置文件必须包含完整 schema。SSH 别名只允许字母、数字、点、下划线和连字符。修改配置后重启服务：

```bash
systemctl --user restart mocop.service
```

配置查找顺序：

1. `--config`
2. `MOCOP_CONFIG`
3. `$XDG_CONFIG_HOME/mocop/config.json`，默认 `~/.config/mocop/config.json`
4. 当前目录的 `config/mocop.json`
5. 包内安全默认配置：空白名单，仅监听回环地址

| 字段 | 作用 | 范围 / 默认值 |
|---|---|---|
| `hosts` / `exclude_hosts` | SSH 别名白名单 / 排除列表 | 默认空 |
| `auto_discover` | 发现 OpenSSH 中明确的 `Host` 别名 | 默认 `false` |
| `poll_interval_seconds` | 启动时采集周期 | 1–3600 秒，默认 5 |
| `probe_timeout_seconds` | 单台完整采集超时 | 2–300 秒 |
| `connect_timeout_seconds` | SSH 建连超时 | 1–120 秒，且小于完整超时 |
| `max_output_bytes` | 单次 SSH 输出硬上限 | 64 KiB–16 MiB |
| `max_workers` | 并发探测上限 | 1–64 |
| `listen_host` / `listen_port` | Web 监听地址和端口 | `127.0.0.1:8787` |
| `history_points` | 每台服务器的内存趋势点数 | 12–8640 |
| `incident_history_points` | 状态变化事件环大小 | 20–5000 |
| `collection_stale_cycles` | 采集延迟判定窗口 | 2–12 个周期 |
| `thresholds` | CPU、内存、Swap、磁盘、GPU 温度和繁忙阈值 | 见示例配置 |

网页上的采集频率会立即改变当前进程的真实 SSH 调度，但不会写回配置；服务重启后恢复 `poll_interval_seconds`。这是有意设计的临时运行控制。

## 工作原理

```text
JSON 主机白名单 ──▶ 有界并发调度 ──▶ OpenSSH
                                          │
                            固定只读脚本：/proc、df、nvidia-smi
                                          │
浏览器 ◀── SSE / JSON ◀── 线程安全内存状态 ◀┘
```

每台主机的结果完成后立即发布，慢节点不会拖延快节点。连续失败的目标指数退避到最多 60 秒，健康节点仍按正常周期采集。失败后保留最后成功样本并标记为陈旧，但不把旧数据计入实时集群汇总。

更完整的模块边界与性能依据见 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [docs/PERFORMANCE.md](docs/PERFORMANCE.md)。

## 健康检查与数据接口

| 接口 | 用途 |
|---|---|
| `GET /healthz` | HTTP 进程存活 |
| `GET /readyz` | 已发现目标且至少获得一份成功样本 |
| `GET /api/snapshot` | 当前集群快照 |
| `GET /api/events` | SSE 实时快照流 |
| `GET /api/history` | 单节点有界短期趋势 |
| `GET /api/incidents` | 有界状态变化时间线 |

唯一写接口只允许同源页面把当前进程的采集周期调整到 2–60 秒，不能更改主机、SSH 参数或远端命令。

## 安全边界

- 浏览器不能添加目标或提供命令；目标只能来自本地配置中的安全 SSH 别名
- OpenSSH 使用结构化参数、`BatchMode`、严格主机密钥校验和操作者现有凭据
- 远端执行仓库内固定、版本化的只读脚本；不读取、复制或保存 SSH 私钥
- 建连时间、总时间、并发数、失败频率以及 stdout/stderr 内存都有硬上限
- 原始 SSH stderr 不进入状态存储，也不会发送到浏览器
- 默认没有账号系统且只监听回环地址；远程开放必须置于带 TLS 和认证授权的反向代理或 VPN 后

完整威胁模型见 [docs/SECURITY.md](docs/SECURITY.md)。漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 开发

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

CI 在 Python 3.10–3.14 上运行语法、格式、静态检查和测试，并在无头 Chrome 中验证完整的 GPU 数据形态、默认折叠、响应式布局和采集频率竞态。

贡献规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。Mocop 使用 [MIT License](LICENSE)。
