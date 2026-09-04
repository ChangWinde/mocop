<p align="center">
  <img src="../../../src/mocop/static/favicon.svg" width="88" height="88" alt="Mocop 标志">
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
- GPU 算力匹配与可选的容量守望：出现满足条件的空闲组合时页面横幅提示，并可选浏览器通知；另有调度热力图、连接拓扑、全局/单服务器程序搜索、按进程筛选/排序、归属筛选、一键复制 `ssh <别名>` 和 CSV 导出
- CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率、运行时间和内核压力失速（PSI）遥测
- 带诊断、确认/静默、分级阈值、防抖处理和定时维护的告警
- 节点级独立调度、可能的共享链路聚合和可选 HTTPS Webhook
- 基于配置的节点资产、预期 GPU 数、本机采集和节点分组
- 单卡趋势和进程时间线，以及可选的有界 SQLite 留存与只读 Slurm/Kubernetes/Docker/Podman 上下文
- 按使用者聚合的 GPU 占用与闲置占比账单（`GET /api/usage` 与使用者面板，窗口可选）
- 六种视觉风格、六种独立主题色、紧凑模式、排序记忆和经过校验的本地背景
- 可选的版本检查与经过校验的控制台一键自更新（默认关闭）
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

`ProxyJump`、`ProxyCommand`、端口、用户和身份文件仍由 OpenSSH 管理。新配置会通过
有界且不建立连接的 `ssh -G` 解析识别代理别名，将自动发现的跳板机排除在采集清单
之外，生成展示拓扑，并按离目标最近的跳板 alias 自动分组；没有跳板的目标若共享编号
alias 前缀（如 `gpu-1`、`gpu-2`），则使用该前缀兜底分组。显式主机、排除项、分组和
手工拓扑始终优先；Git 远端仍单独过滤。

### 2. 安装并快速部署

```bash
uv tool install "git+https://github.com/ChangWinde/mocop.git@v0.11.0"
"$(uv tool dir --bin)/mocop" deploy --display-name monitor-0
```

全新服务器无需手写资产 JSON：`mocop deploy` 会创建 `0600` 配置、默认采集本机、从
`~/.ssh/config` 自动发现安全 alias，并安装和验证用户服务。显式 bin 路径不依赖当前
shell 的 PATH；后续命令假定已开启新 shell（或执行过 `uv tool update-shell`）。
已存在 `config.json`、`access-token` 或 `environment` 文件时命令会拒绝，已有部署请使用
`mocop service install`；没有 systemd 用户管理器的环境（容器、部分 WSL）请改用
`mocop init` 加前台 `mocop`。

### 3. 校验部署与 SSH 路径

```bash
mocop config check
mocop doctor
```

`mocop config check` 只校验配置，不启动网页服务，也不打开任何 SSH 连接；它报告解析到的路径、主机数量以及持久化/工作负载/拓扑/webhook 状态（绝不输出密钥值），有效时退出 `0`，无效时退出 `2`。

随后 `mocop doctor` 验证每个受监控别名的非交互式 SSH 可达性与连接复用：全部可用退出 `0`，至少一个失败退出 `1`，配置或用法错误退出 `2`。加 `--json` 可输出机器可读报告；[运维手册](../../OPERATIONS.md#command-reference-and-exit-codes)列出了全部命令、参数和退出码。

### 4. 打开控制台

打开 `mocop deploy` 打印的完整 `Dashboard:` 能力 URL，例如
`http://127.0.0.1:8787/#access_token=...`。页面把能力保存在当前标签页的
`sessionStorage` 中，刷新后仍保持认证；新开标签页需要再次使用打印的 URL，
或在弹出的输入框中粘贴配置旁 `access-token` 文件（默认为
`~/.config/mocop/access-token`）的内容。能力的完整规则由
[API 参考](../../API.md#scope-and-compatibility)负责说明。

直接运行 `mocop` 可使用前台模式。后台服务可通过以下命令管理：

```bash
mocop service status
mocop service uninstall
```

卸载只删除生成的服务 unit。跨机器迁移、升级、回滚、token 轮换或清理前，请阅读
[运维手册](../../OPERATIONS.md)；其中包含非破坏性的 `mocop migrate` 流程、全部
保留文件和安全备份步骤。

## 配置

`mocop deploy` 写入全新服务器配置；`mocop init` 是只创建配置的底层命令。
控制台可安全修改常用的资产、周期、
维护和分组设置，无需重启。所有字段、默认值和限制以
[配置参考](../../CONFIGURATION.md)为准；手工编写 JSON 时参考
[完整安全示例](../../../examples/mocop.example.json)。可选 workload 身份和
限额 SQLite 历史默认关闭。手工修改后先执行 `mocop config check`；需要重启
托管服务时按[运维手册](../../OPERATIONS.md)操作。

## 日常使用

- 在“全部服务器”中搜索整个集群；先选择服务器后，可按程序、命令、PID、
  用户、workload、队列、主机、型号或 UUID 限定范围。
- 主 GPU 表直接展示进程数、最大分配、显存覆盖和新鲜度；打开单卡后，任务行以
  真实入口（例如 `train.dragon_video2motion` 而非 `python`）开头，附带环境与
  资源占用标签，命令行可点击展开，并支持有界筛选、排序、复制和归属下钻。
- 打开告警查看证据并确认/静默，或设置维护窗口；采集不会因此停止。
- “立即探测”“匹配算力”和“设置 → 监控节点”覆盖常用操作。容量匹配只是
  当前观测，不代表资源预留。
- 使用 `mocop --once` 导出快照；自动化可加 `--strict`，让任一配置主机无
  在线样本时返回失败。

## HTTP API

网页展示的一切也都可以通过一套小型 JSON API 获取：稳定的机器可读错误
code 与 P/A/R/W 访问分级。公开的 `GET /api/meta` 清单列出每个路由的分级、
可接受的查询参数及取值范围、请求体上限、响应类型，以及当前版本对应的文档
链接；`403` 响应会说明能力令牌存放在哪里——AI 代理无需任何额外知识即可
驾驭一个部署。只有 API 发现与健康检查公开；带认证的 curl 示例，以及
非观众型自动化不应发送 `X-Monitor-Request: dashboard` 标记头的原因见
[API 参考](../../API.md)。

## 指标与故障排查

已认证的 `GET /metrics` 以 OpenMetrics 1.0 输出当前快照，不会触发采集。
Prometheus 配置和指标契约由 [API 参考](../../API.md)维护。首次排查使用：

```bash
journalctl --user -u mocop -f              # 实时跟踪服务日志
curl -s http://127.0.0.1:8787/healthz      # 存活状态 + 累计 SSH 传输重试次数
curl -s http://127.0.0.1:8787/readyz       # 就绪状态；首次成功采集前返回 503 并带原因
mocop doctor --probe                       # 对每个别名执行一次有界生产采集
```

各节点独立调度；失败样本会明确标记为陈旧，并按上限退避重试。服务恢复和
退出码见[运维手册](../../OPERATIONS.md)；调整周期或并发前阅读
[性能说明](../../PERFORMANCE.md)。

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
python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

完整的质量门禁清单（lint、覆盖率、浏览器 leaf 契约测试、真实浏览器 smoke
测试）以及提交与文档规则以[贡献指南](../../../.github/CONTRIBUTING.md)为准；
修改代码、测试、文档或公开契约前请先阅读。

## 许可证

[MIT](../../../LICENSE)
