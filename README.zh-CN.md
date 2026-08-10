<p align="center">
  <img src="mocop/static/favicon.svg" width="88" height="88" alt="Mocop 标志">
</p>

<h1 align="center">Mocop</h1>

<p align="center">基于 OpenSSH 的 AI-native GPU 集群监控</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml"><img src="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6d8cff" alt="MIT 许可证"></a>
  <img src="https://img.shields.io/badge/runtime_dependencies-0-55d6a5" alt="零运行时依赖">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#配置">配置</a> ·
  <a href="#故障行为">故障排查</a> ·
  <a href="#安全">安全</a>
</p>

![使用虚构集群数据的 Mocop 控制台](docs/assets/dashboard.png)

Mocop 是面向 NVIDIA GPU 集群的本地网页监控工具。它复用已有 OpenSSH 别名，采集 GPU、CPU、内存、Swap、磁盘和网络数据，并在每台主机完成采集后立即将结果推送到浏览器。

远端主机不需要安装 Agent、数据库、Python，也不需要开放监控端口。远端只需提供 Linux `/proc`；需要 NVIDIA GPU 数据时还需提供 `nvidia-smi`。Mocop 本身只使用 Python 标准库和系统 OpenSSH 客户端。

这里的 **AI-native** 是指界面围绕 GPU 容量、任务放置和故障定位设计。Mocop 不调用 AI 服务，也不上传遥测数据。

## 功能

- GPU 利用率、显存、温度、功耗、型号、驱动、硬件健康和每卡进程
- GPU 算力匹配、调度热力图、节点分组、搜索、筛选和 CSV 导出
- CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率和运行时间
- 带防抖阈值、陈旧数据处理、失败退避和定时维护窗口的告警
- 基于配置的节点资产、预期 GPU 数、本机采集和节点分组
- 五套浏览器本地皮肤、紧凑模式、排序记忆和经过校验的本地背景
- 可供 Prometheus 和 Grafana 使用的 OpenMetrics 1.0 端点

## 快速开始

Mocop 需要 Linux、Python 3.10 或更高版本、OpenSSH，以及对每台远端节点的非交互式 SSH 访问。启用无人值守采集前，请人工核对主机指纹。

### 1. 配置 SSH

在 `~/.ssh/config` 中为每个计算节点添加一个明确别名：

```sshconfig
Host gpu-node-01
    HostName 192.0.2.10
    User cluster-monitor
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

上面的地址只用于文档示例。替换为实际主机后验证连接：

```bash
ssh gpu-node-01 true
ssh -o BatchMode=yes gpu-node-01 true
```

`ProxyJump`、端口、用户和身份文件应继续由 OpenSSH 管理。不要把跳板机或 Git 远端加入 Mocop 的被监控 `hosts` 列表。

### 2. 安装并初始化

```bash
uv tool install git+https://github.com/ChangWinde/mocop.git
mocop init --host gpu-node-01 --host gpu-node-02
```

`mocop init` 会创建权限为 `0600` 的 `~/.config/mocop/config.json`。它只监控通过 `--host` 指定的别名，关闭自动发现，并将采集周期设为 5 秒。

### 3. 启动控制台

```bash
mocop service install
```

打开 <http://127.0.0.1:8787>。该命令会安装、启用并立即启动用户级 systemd 服务，但不会修改系统的 linger 策略。

直接运行 `mocop` 可使用前台模式。后台服务可通过以下命令管理：

```bash
mocop service status
mocop service uninstall
```

## 配置

初始化生成的文件已经包含全部字段。只有在网页未提供对应设置时才需要直接编辑。下面只展示主要资产字段：

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

- `hosts` 是明确的监控白名单，`exclude_hosts` 的排除优先级更高。
- `local_host` 指定 `hosts` 中唯一一台绕过 SSH、直接在本机采集的节点。
- `expected_gpu_counts` 用于发现 GPU 缺失。
- `host_groups` 用于共享节点分组。
- `host_overrides` 只用于调整经过测量的慢节点的采集周期或超时。

全部字段和安全范围见[完整配置示例](examples/mocop.example.json)。直接修改 JSON 后需要重启服务；网页中的修改会先校验，再原子写入，并立即生效。

### 哪些设置会保留

| 设置 | 保存位置 | 服务重启后保留 | 不同浏览器共享 |
|---|---|---:|---:|
| 采集周期、探测超时、并发数 | `config.json` | 是 | 是 |
| 监控节点、分组、维护窗口 | `config.json` | 是 | 是 |
| 皮肤、密度、排序、筛选、GPU 列 | 浏览器 `localStorage` | 是 | 否 |
| 自定义背景 | 浏览器 `IndexedDB` | 是 | 否 |

只有清理当前浏览器的站点数据或恢复显示偏好时，浏览器设置才会丢失。移除自定义背景是单独操作。

网页允许设置 2–60 秒采集周期、2–300 秒探测超时和 1–64 个并发 worker。周期越短、并发越高，SSH 和远端主机负载越大。

## 日常使用

- 点击 GPU 行或热力图单元格，查看进程及每个进程的显存占用。
- 使用“匹配算力”查找同一主机、同一型号且剩余显存足够的 GPU。结果不代表资源预留。
- 设置维护窗口后，采集继续进行，但对应问题不会进入待处理告警。
- 在“设置 → 监控节点”中扫描 SSH 别名，添加或删除符合条件的计算节点。
- 可上传最大 32 MiB 的 PNG、JPEG、WebP 或 AVIF 背景；超过 8 MiB 时只在浏览器内压缩，不会上传。
- 使用 `mocop --once > snapshot.json` 导出一次当前快照。

## Prometheus

`GET /metrics` 使用 OpenMetrics 1.0 输出当前内存快照，不会触发新的采集：

```yaml
scrape_configs:
  - job_name: mocop
    static_configs:
      - targets: ["127.0.0.1:8787"]
```

该端点包含采集健康、节点可用性、告警、系统资源和当前 GPU 指标。陈旧资源值、进程名称和 PID 不会被导出。

## 故障行为

Mocop 不会等待慢节点才发布正常节点的数据。失败节点保留最后一次成功样本，但会标记为陈旧，并从当前集群总量中排除。连续失败的重试间隔最长为 60 秒。

采集周期是目标频率，不保证每轮完整集群采集都能在该时间内结束。连接超时可能让整轮耗时更长，但已经完成的节点仍会通过 SSE 立即更新。

调整超时前，先在 Mocop 外测试同一条 SSH 路径：

```bash
ssh -o BatchMode=yes gpu-node-01 true
ssh -G gpu-node-01 | grep -E '^(hostname|port|user|proxyjump|controlmaster) '
```

如果多台节点共同依赖一个 `ProxyJump`、VPN 或 FRP 路径并同时离线，应先检查这条共享路径。重启 Mocop 无法修复不可用的隧道或远端 SSH 服务。

## 安全

Mocop 只接受明确列出的 SSH 别名，并执行固定的只读探针。它强制启用主机密钥校验、BatchMode、超时、输出上限、并发上限、私有原子配置写入和远端文本安全渲染。

服务没有内置用户系统，默认只监听 `127.0.0.1`。如需远程开放控制台或 `/metrics`，必须放在带身份认证的 TLS 反向代理或私有 VPN 后面。

修改信任边界前，请阅读[威胁模型](docs/SECURITY.md)和[安全策略](.github/SECURITY.md)。

## 开发

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
uvx --from ruff==0.12.11 ruff check .
uvx --from ruff==0.12.11 ruff format --check .
node --experimental-websocket tests/browser_smoke.mjs
```

更多信息见[贡献指南](.github/CONTRIBUTING.md)、[架构](docs/ARCHITECTURE.md)、[性能说明](docs/PERFORMANCE.md)和[更新日志](docs/CHANGELOG.md)。

## 许可证

[MIT](LICENSE)
