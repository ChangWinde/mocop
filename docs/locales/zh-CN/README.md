<p align="center">
  <img src="../../../mocop/static/favicon.svg" width="88" height="88" alt="Mocop 标志">
</p>

<h1 align="center">Mocop</h1>

<p align="center">基于 OpenSSH 的 AI-native GPU 集群监控</p>

<p align="center">
  <a href="../../../README.md">English</a> · <a href="README.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml"><img src="https://github.com/ChangWinde/mocop/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <a href="../../../LICENSE"><img src="https://img.shields.io/badge/license-MIT-6d8cff" alt="MIT 许可证"></a>
  <img src="https://img.shields.io/badge/runtime_dependencies-0-55d6a5" alt="零运行时依赖">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#日常使用">日常使用</a> ·
  <a href="#文档">文档</a> ·
  <a href="#安全">安全</a>
</p>

![使用虚构集群数据的 Mocop 控制台](../../assets/dashboard.png)

Mocop 是面向 NVIDIA GPU 集群的本地网页监控工具。它复用已有 OpenSSH 别名，采集 GPU、CPU、内存、Swap、磁盘和网络数据，并在每台主机完成采集后立即将结果推送到浏览器。

远端主机不需要安装 Agent、数据库、Python，也不需要开放监控端口。远端只需提供 Linux `/proc`；需要 NVIDIA GPU 数据时还需提供 `nvidia-smi`。Mocop 本身只使用 Python 标准库和系统 OpenSSH 客户端。

这里的 **AI-native** 是指界面围绕 GPU 容量、任务放置和故障定位设计。Mocop 不调用 AI 服务，也不上传遥测数据。

当前发布的控制台界面固定为简体中文，尚未提供语言切换；中英文 README
以及 API、运维和工程文档均持续维护。

## 一览

| 属性 | 当前行为 |
|---|---|
| 部署 | 单个 Python 进程，或自动生成的用户级 systemd 服务 |
| 远端占用 | 不安装 Agent、不开放监控端口；通过已有 OpenSSH 别名执行固定只读采集 |
| 运行时依赖 | Python 标准库和系统 `ssh` 客户端 |
| 访问控制 | 每次安装独立的私有 Bearer capability；默认只监听 loopback |
| 更新模型 | 各节点独立采集，通过已认证 SSE 持续推送到浏览器 |
| 历史保留 | 默认仅内存；可选私有、限额的 SQLite 历史 |
| 核心路径 | 在一个控制台查服务器、GPU、程序、用户、workload、告警和可用容量 |

## 主要能力

- GPU 利用率、显存、温度、功耗、型号、驱动、硬件健康和便于扫描的每卡进程摘要
- GPU 算力匹配、调度热力图、连接拓扑、全局/单服务器程序搜索、按进程筛选/排序、归属筛选和 CSV 导出
- CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率、运行时间和内核压力失速（PSI）遥测
- 带诊断、确认/静默、分级阈值、防抖处理和定时维护的告警
- 节点级独立调度、可能的共享链路聚合和可选 HTTPS Webhook
- 基于配置的节点资产、预期 GPU 数、本机采集和节点分组
- 单卡趋势和进程时间线，以及可选的有界 SQLite 留存与只读 Slurm/Kubernetes/Docker/Podman 上下文
- 按使用者聚合的 GPU 占用与闲置占比账单（`GET /api/usage` 与使用者面板，窗口可选）
- 六种视觉风格、六种独立主题色、紧凑模式、排序记忆和经过校验的本地背景
- 可供 Prometheus 和 Grafana 使用的 OpenMetrics 1.0 端点

## 快速开始

Mocop 需要 Linux、Python 3.10 或更高版本、OpenSSH，以及对每台远端节点的非交互式 SSH 访问。启用无人值守采集前，请人工核对主机指纹。

下面的步骤使用 [uv](https://docs.astral.sh/uv/) 安装 Mocop。如果尚未安装 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Mocop 以用户级 systemd 服务运行。未启用 linger 时，用户服务会随登出立即停止；执行一次以下命令，让监控在登出后继续运行：

```bash
loginctl enable-linger "$USER"
```

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

`ProxyJump`、端口、用户和身份文件应继续由 OpenSSH 管理。不要把跳板机或 Git 远端加入 Mocop 的被监控 `hosts` 列表。可选的展示拓扑可以呈现跳板机和 FRP 节点，但不会采集它们。

### 2. 安装并初始化

```bash
uv tool install git+https://github.com/ChangWinde/mocop.git
mocop init --host gpu-node-01 --host gpu-node-02
```

`mocop init` 会创建权限为 `0600` 的 `~/.config/mocop/config.json`。它只监控通过 `--host` 指定的别名，关闭自动发现，并将采集周期设为 5 秒。

### 3. 校验配置与 SSH 路径

```bash
mocop config check
mocop doctor
```

`mocop config check` 只解析并校验配置，不启动网页服务，也不打开任何 SSH 连接。它报告解析到的配置路径、主机数量、持久化/工作负载/拓扑状态，以及每个 webhook 引用的环境变量名及其 set/unset 状态——绝不输出变量值。配置有效时退出码为 `0`，无效时为 `2`。

随后 `mocop doctor` 验证每个受监控别名的非交互式 SSH 可达性与连接复用（全部别名可用时退出 `0`，否则为 `1`）。

### 4. 启动控制台

```bash
mocop service install
```

打开命令打印的完整 `Dashboard:` 能力 URL，例如
`http://127.0.0.1:8787/#access_token=...`。URL 片段不会随 HTTP 请求发送；
页面会立即清除它，并把能力保存在当前标签页的 `sessionStorage` 中，刷新和
服务重启流程仍可继续认证；关闭标签页或新开独立标签页后，需要再次使用
打印的完整 URL。该命令会安装、启用、启动并验证用户级
systemd 服务，但不会修改系统的 linger 策略。

直接运行 `mocop` 可使用前台模式。后台服务可通过以下命令管理：

```bash
mocop service status
mocop service uninstall
```

卸载只会停止/禁用服务并删除生成的 unit；配置、Bearer token、可选环境
文件、SQLite 状态、浏览器数据、journal、SSH 文件/控制套接字、已安装包和
linger 策略都会保留。升级、回滚、token 轮换或手动清理前，请阅读
[运维手册](../../OPERATIONS.md)。

## 配置

初始化生成的文件已经包含全部字段。只有在网页未提供对应设置时才需要
直接编辑。[配置字段与边界参考](../../CONFIGURATION.md) 是所有字段、默认值、
关联约束和硬限制的权威说明。下面只展示主要资产字段：

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

资产清单是明确的：`hosts` 允许采集，`exclude_hosts` 始终优先，
`local_host` 最多指定一台白名单节点使用同一固定探针而不经过 SSH。预期
GPU 数、显示分组、单节点周期、分级告警阈值、维护窗口和只用于展示的连接树
都属于配置，不会变成远端发现副作用。

可选的 `workloads.mode` 通过有界读取补充进程属主、命令、启动时间及支持的
调度器/容器身份。可选 persistence 使用私有且限额的 SQLite 保存趋势和告警
上下文，默认关闭。Webhook JSON 只保存环境变量名，不保存地址或签名密钥。
[配置参考](../../CONFIGURATION.md)负责全部默认值与边界；
[完整安全示例](../../../examples/mocop.example.json)展示完整 schema；
[运维手册](../../OPERATIONS.md)负责密钥文件、重启和回滚流程。

网页修改会经过同一严格校验，以私有原子写入保存并立即生效。手工修改 JSON
后，先运行 `mocop config check`，再按运维手册重新安装/重启托管服务。

### 哪些设置会保留

| 设置 | 保存位置 | 服务重启后保留 | 不同浏览器共享 |
|---|---|---:|---:|
| 采集周期、探测超时、并发数 | `config.json` | 是 | 是 |
| 监控节点、分组、维护和告警操作 | `config.json` | 是 | 是 |
| 可选节点/GPU 趋势及进程/告警转移 | 私有 SQLite 状态 | 是 | 是 |
| 视觉风格、主题色、密度、排序、筛选、GPU 列 | 浏览器 `localStorage` | 是 | 否 |
| 自定义背景 | 浏览器 `IndexedDB` | 是 | 否 |

只有清理当前浏览器的站点数据或恢复显示偏好时，浏览器设置才会丢失。移除自定义背景是单独操作。

网页允许设置 2–60 秒采集周期、2–300 秒探测超时（须大于连接超时，默认 5 秒）和 1–64 个并发 worker。周期越短、并发越高，SSH 和远端主机负载越大。

## 日常使用

- 可按程序名、命令、PID、用户、workload、队列、服务器、GPU 型号或 UUID
  搜索。在“全部服务器”中搜索得到全局结果；先选择一台服务器则只搜索该节点。
  点击程序结果会直接打开所在 GPU，并把关键词带入单卡程序筛选。
- 主 GPU 表可直接扫描进程数、最大已知进程、进程已分配显存和采样新鲜度，
  无需逐卡打开详情。
- 点击 GPU 行或热力图单元格进入以进程为先的工作区：查看归属与已知显存覆盖，
  在 100 行显示上限前筛选“有归属/无归属”，再按显存、运行时长或程序名排序。
  用户/workload 标签可继续筛选当前卡；快捷操作可以复制 PID/命令或转到全局搜索。
- 打开告警查看基于证据的处理建议，再按固定时长确认或仅静默该条件。
- 在选中节点上使用“立即探测”，提前执行一次有界采集，不改变全局刷新周期。
- 使用“匹配算力”查找同一主机、同一型号且剩余显存足够的 GPU。结果不代表资源预留。
- 设置维护窗口后，采集继续进行，但对应问题不会进入待处理告警。
- 在“设置 → 监控节点”中扫描 SSH 别名，添加或删除符合条件的计算节点。
- 按照[升级与回滚手册](../../OPERATIONS.md)操作。验证软件包升级后，可使用
  “设置 → 监控服务状态 → 重启服务”；该按钮只在用户级服务模式下可用，
  恢复后页面会自动刷新。
- 可上传最大 32 MiB 的 PNG、JPEG、WebP 或 AVIF 背景；超过 8 MiB 时只在浏览器内压缩，不会上传。
- 使用 `mocop --once > snapshot.json` 导出一次当前快照。脚本与定时任务可加 `--strict`：只要有任意配置主机未产生在线采样即退出码 `1`，并在 stderr 列出失败主机。

## HTTP API

网页展示的一切也都可以通过一套小型 JSON API 获取：稳定的机器可读错误
code、公开且自描述的 `GET /api/meta` 端点，以及 P/A/R/W 访问分级。遥测、
SSE 和 OpenMetrics 均要求安装级 Bearer 能力；只有 API 发现、存活和就绪
检查公开。带认证的 curl 示例、全部端点与字段表，以及非观众型自动化不应
发送 `X-Monitor-Request: dashboard` 标记头的原因见 [API 参考](../../API.md)。

## Prometheus

`GET /metrics` 使用 OpenMetrics 1.0 输出当前内存快照，不会触发新的采集：

```yaml
scrape_configs:
  - job_name: mocop
    authorization:
      type: Bearer
      credentials_file: /home/alice/.config/mocop/access-token
    static_configs:
      - targets: ["127.0.0.1:8787"]
```

请使用绝对路径；Prometheus 必须以有权读取该私密文件的身份运行，或使用
单独受保护的凭据副本。该端点包含采集与后台子系统健康、节点可用性、告警、
系统资源和当前 GPU 指标。陈旧资源值、进程名称和 PID 不会被导出。

## 故障行为

Mocop 按节点独立调度，同一节点不会重叠采集。只要仍有 worker 容量，慢节点就
不会推迟健康节点。失败节点保留最后一次成功样本，但会标记为陈旧并从当前汇总中
排除；重试最长退避 60 秒，并按节点分散。

采集周期是目标频率。worker 饱和或单次探测超过周期时，只会推迟对应节点，不会
重新引入全局等待屏障。

调整超时前，先用内置的只读诊断检查 SSH 路径：

```bash
mocop doctor
mocop doctor --profile
mocop doctor --probe
```

默认命令检查非交互可达性和连接复用；`--profile` 将耗时拆分为传输、固定
脚本和 NVIDIA 查询；`--probe` 执行一次真实、有界的生产采集。Mocop 不会
修改 SSH 配置。OpenSSH 复用的实测数据见[性能说明](../../PERFORMANCE.md)，
服务诊断流程见[运维手册](../../OPERATIONS.md)。如果多台节点共同依赖一个
跳板机、VPN 或 FRP 路径并同时离线，应先检查共享路径，再考虑重启 Mocop。

### 故障排查

大多数"为什么没有数据"的排查用四条命令即可覆盖：

```bash
journalctl --user -u mocop -f              # 实时跟踪服务日志
curl -s http://127.0.0.1:8787/healthz      # 存活状态 + 累计 SSH 传输重试次数
curl -s http://127.0.0.1:8787/readyz       # 就绪状态；首次成功采集前返回 503 并带原因
mocop doctor --probe                       # 对每个别名执行一次真实生产采集
```

`mocop doctor --probe` 按别名报告探测状态、延迟、GPU/进程数和 workload
覆盖率。它依赖真实连接测试，因此不能与 `--no-connect` 同时使用。

### CLI 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功。 |
| `1` | 诊断或采集失败：`mocop doctor` 发现至少一个不可用别名，或运行中监控的采集器/监听器失败。 |
| `2` | 配置或用法错误：配置无效、别名过滤器未知，或 `--probe` 与 `--no-connect` 之类的冲突参数。 |

## 安全

Mocop 只接受明确列出的 SSH 别名，并执行固定的只读探针。它强制启用主机密钥校验、BatchMode、超时、输出上限、并发上限、私有原子配置写入和远端文本安全渲染。

服务没有内置用户系统，默认只监听 `127.0.0.1`。私密的安装级 Bearer 能力
保护遥测、指标、SSE 和写操作不被无关本地用户访问，但它代表一个完整的
操作员角色。如需远程开放 Mocop，请使用带身份认证的 TLS 反向代理或私有
VPN；明文 HTTP 上的 Bearer 头不提供网络机密性或服务端身份认证。

修改信任边界前，请阅读[威胁模型](../../SECURITY.md)和[安全策略](../../../.github/SECURITY.md)。

## 文档

[文档导航](../../README.md)提供完整的读者地图、权威文档归属、更新触发条件、
语言策略和 ADR 生命周期。

| 任务 | 文档 |
|---|---|
| 配置集群 | [配置参考](../../CONFIGURATION.md) |
| 运维、升级、备份、回滚或卸载 | [运维手册](../../OPERATIONS.md) |
| 编写 API 客户端或 Prometheus 集成 | [HTTP API](../../API.md) |
| 审查信任与部署边界 | [安全模型](../../SECURITY.md) |
| 理解组件和设计决策 | [架构](../../ARCHITECTURE.md)与 [ADR 索引](../../adr/README.md) |
| 复现性能结论 | [性能说明](../../PERFORMANCE.md) |
| 审阅当前质量与资源证据 | [质量评估](../../QUALITY.md) |
| 查看用户可见变更 | [更新日志](../../CHANGELOG.md) |

## 开发

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
uvx --from ruff==0.12.11 ruff check .
uvx --from ruff==0.12.11 ruff format --check .
node --experimental-websocket tests/browser_smoke.mjs
```

修改代码、测试、文档或公开契约前，请阅读[贡献指南](../../../.github/CONTRIBUTING.md)。

## 许可证

[MIT](../../../LICENSE)
