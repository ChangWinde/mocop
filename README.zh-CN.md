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
  <a href="#架构">架构</a> ·
  <a href="#安全边界">安全</a>
</p>

![使用虚构集群数据的 Mocop 控制台](docs/assets/dashboard.png)

Mocop 为 NVIDIA 计算集群提供实时、GPU 优先的资源视图。它复用现有 OpenSSH 配置，采集 GPU、CPU、内存、Swap、磁盘和网络指标，并在每台主机完成采集后立即把结果推送到本地网页。

远端机器无需安装 Agent、数据库、Python 环境，也不需要开放监控端口。本地运行时只依赖 Python 标准库和系统 OpenSSH 客户端。

Mocop 的 AI-native 指产品围绕 AI 训练与推理集群的容量判断、故障定位和调度决策设计。采集路径不调用外部 AI API，也不会把遥测发送给第三方。

## 为什么选择 Mocop

- GPU 优先：利用率、显存、温度、功耗、型号、驱动、每卡任务和硬件健康集中展示
- 集群视角：实时算力匹配、调度热力图与按主机组织的 GPU 分组，默认折叠，需要时再展开
- 完整上下文：CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率和运行时间
- 舒适交互：服务器拖动排序、五套结构化皮肤、经校验的浏览器本地背景、搜索、筛选、有界趋势、异常历史和安全 CSV 导出
- 节点管理：扫描 SSH 别名、过滤 Git/GitHub/GitLab、受控增删、原子持久化并热更新调度器
- 生态集成：零依赖 OpenMetrics 1.0 端点，可直接接入 Prometheus / Grafana
- 稳定采集：预期 GPU 资产、权威告警、防抖触发与恢复、失败退避和陈旧数据处理
- 计划维护：持续采集，同时将有期限的静默问题与当前待处理问题明确分离
- 安全默认值：显式主机白名单、回环监听、严格主机密钥校验、固定远端脚本和资源上限

## 环境要求

- Mocop 所在机器使用 Linux 和 Python 3.10 或更高版本
- OpenSSH 客户端
- 可以通过密钥或 `ssh-agent` 非交互访问目标主机
- 目标主机提供 Linux `/proc`
- 需要 NVIDIA GPU 指标的主机提供 `nvidia-smi`
- 只有使用可选的后台服务时才需要用户级 systemd 管理器

开始无人值守监控前，请人工核对每台主机的指纹。

## 快速开始

### 1. 创建 OpenSSH 别名

Mocop 监控的是 SSH 别名，而不是直接写入连接字符串。先在 `~/.ssh/config` 中为每个计算节点定义明确的别名：

```sshconfig
Host gpu-node-01
    HostName 192.0.2.10
    User cluster-monitor
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

这里使用的是文档专用地址段 `192.0.2.0/24`，实际使用时必须替换为自己的环境。如果节点需要跳板机，继续由 OpenSSH 维护连接关系：

```sshconfig
Host gpu-bastion
    HostName 192.0.2.5
    User cluster-monitor

Host gpu-node-*
    ProxyJump gpu-bastion
```

首次连接时人工核对主机指纹，然后确认无人值守连接可用：

```bash
ssh gpu-node-01 true
ssh -o BatchMode=yes gpu-node-01 true
```

跳板机和 Git 远端等无关别名只是连接基础设施，不是被监控的计算节点，不要把它们加入 Mocop 的 `hosts`。

### 2. 安装 Mocop

使用 [`uv`](https://docs.astral.sh/uv/) 创建隔离的命令环境：

```bash
uv tool install git+https://github.com/ChangWinde/mocop.git
```

### 3. 创建集群配置

```bash
mocop init --host gpu-node-01 --host gpu-node-02
```

这条命令会以 `0600` 权限创建完整的 `~/.config/mocop/config.json`。只有 `hosts` 中的别名会被监控。生成的配置默认关闭 `auto_discover`，因此不会自动加入跳板机、Git 别名或 SSH 通配项。初始采集周期为 5 秒。

修改超时、并发、历史长度或告警阈值前，请先查看[完整示例配置](examples/mocop.example.json)。

### 4. 启动控制台

```bash
mocop service install
```

打开 <http://127.0.0.1:8787>。

`mocop service install` 会校验配置、写入加固后的用户服务、设置启用并立即启动。安装 Python 包本身不会修改 systemd。不需要后台服务时，直接运行 `mocop` 即可。

```bash
mocop service status
mocop service uninstall
```

服务随用户的 systemd 管理器启动。如果需要在该用户登录前运行，应由管理员在检查该账号的 SSH 凭据管理方式后启用 linger：

```bash
loginctl enable-linger <user>
```

Mocop 不会自动修改 linger 策略。

## 常用工作流

### 修改被监控的主机

打开 **设置 → 监控节点**，即可扫描 OpenSSH 配置中的明确别名。扫描只读取配置，不会连接候选机器；名称可识别的 Git、GitHub、GitLab 别名和 `exclude_hosts` 中的项目会被过滤。只有服务器端刚扫描到的候选别名才允许添加，移除则需要二次确认。Mocop 会以 `0600` 权限原子写入主机清单并热更新采集器，不需要重启。对显式节点点击 **设置分组**，可以让所有浏览器共享一个简短的用途或归属分组；服务器排序选择 **节点分组** 后会在左侧形成稳定分区。

如果还要维护预期 GPU 数量、单机覆盖项或基础设施排除规则，再直接编辑 JSON。`mocop init` 不会覆盖已有配置；保持自动发现关闭，并且只列出计算节点别名：

```json
{
  "auto_discover": false,
  "hosts": ["gpu-node-01", "gpu-node-02"],
  "exclude_hosts": ["gpu-bastion", "git-host"],
  "host_groups": {
    "gpu-node-01": "训练集群",
    "gpu-node-02": "推理集群"
  }
}
```

上面只是资产字段片段，不能替换完整配置；请保留 `mocop init` 生成的其他字段。`host_groups` 的键必须引用显式启用的 `hosts`，每台节点最多属于一个共享分组。`exclude_hosts` 是最终拒绝列表，适合跳板机以及名称中不直接包含 `git`、`github` 或 `gitlab` 标记的自定义代码托管别名。手工编辑文件后需要重启服务；网页中的节点与分组变更会直接热更新当前进程。

### 调整并持久化采集策略

网页顶部选择器可以把采集周期调整为 2 至 60 秒。居中的 **设置 → 采集策略** 还可以修改单轮完整探测超时与并发探测数。这些操作会经过严格校验，以原子方式写入选中的本地 `config.json`，立即热更新真实调度器，并在下次服务启动时恢复；初始默认周期仍为 5 秒。

更短的周期、更长的超时和更高的并发都会增加采集压力，因此 Mocop 保持严格数值边界，也不会向网页暴露 SSH 路径、命令、监听地址、告警阈值或任意配置键。

### 在不中断采集的情况下静默计划维护

打开 **设置 → 监控节点**，点击 **设为维护**，填写简短原因并选择 1 小时、4 小时、24 小时或 7 天。维护窗口会写入 `config.json`，无需重启即可生效，到期自动结束，也可以随时手动结束。

维护不会暂停 SSH 采集、删除活动问题或伪造恢复事件。Mocop 始终展示节点的真实状态和变化历史，同时把全部活动问题与当前需要处理的问题分开统计。这样可以让计划内操作退出关注队列，又不会形成监控盲区。

### 检测 GPU 缺失并抑制资源噪声

可以为稳定的计算节点声明预期设备数，并在配置中调整告警稳定窗口：

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

预期数量和覆盖项的键必须引用显式 `hosts` 列表中实际启用的别名。只有在实测某台节点自身的资源查询超过集群默认超时后，才应使用主机覆盖项：更长的超时可以恢复完整数据，更慢的独立周期则避免每个全局周期都运行这次昂贵探测。连接失败和 GPU 查询失效会立即显示；资源压力需要连续样本，恢复也需要连续健康样本，而低负载但持续占用显存使用更长的窗口。这样可以避免单个噪声样本刷屏。

### 监控运行 Mocop 的本机

为本机定义一个资产别名，把它加入 `hosts`，再把相同别名设置为 `local_host`：

```json
{
  "hosts": ["monitor-host", "gpu-node-01", "gpu-node-02"],
  "local_host": "monitor-host"
}
```

上面只是资产字段片段，请保留配置中的其他字段。`local_host` 不需要 OpenSSH 条目：Mocop 会在本机执行同一套固定、有界的探针并绕过 SSH。只支持一个本机别名，而且它必须同时出现在显式 `hosts` 列表中。

### 个性化控制台

在居中的 **设置** 工作区中，可以选择五套专门设计的界面皮肤、舒适或紧凑的信息密度、默认服务器关注视图、服务器和 GPU 排序、热力图指标及可见的 GPU 数据列。雾光玻璃与终端矩阵会改变几何、表面质感、层次和字体气质，而不只是换颜色。还可以选择不超过 32 MiB 的 PNG、JPEG、WebP 或 AVIF 背景并调整可见度；超过 8 MiB 的原图会在浏览器内缩放并压缩为有界 WebP。Mocop 会校验容器与解码尺寸，只在当前浏览器保存最多 8 MiB，绝不会把图片上传到服务端。

拖动任意服务器可以保存自定义顺序。这些显示偏好只保存在当前浏览器中，不会让不同查看者互相覆盖；采集策略与监控节点会明确标记为本地配置的持久化变化。点击 GPU 表格行或热力图单元格，可以查看该卡正在运行的 CUDA 计算任务及每个进程的显存。Mocop 针对界面文字与表格数字使用经过优化的本机字体栈，不会下载第三方字体。

点击 **匹配算力**，可以填写 GPU 数量、每卡最少可用显存和可选型号。Mocop 只根据当前快照在同一节点、同一型号内排序候选，排除维护中或存在硬件问题的设备，并明确区分完全满足与接近结果。计算完全发生在浏览器中，不会再发起 SSH 查询。它用于辅助放置任务，不会预留资源，启动作业前仍需确认调度系统中的实际状态。

### 获取一次快照

一次性采集适合本地检查或受控的自动化流程：

```bash
mocop --once > snapshot.json
```

输出包含资产与遥测，应按照基础设施日志的安全要求保存和删除。

### 使用 Prometheus 抓取当前指标

Mocop 在 `GET /metrics` 暴露当前内存快照，格式为 OpenMetrics 1.0。同一台机器上的 Prometheus 可以使用：

```yaml
scrape_configs:
  - job_name: mocop
    metrics_path: /metrics
    static_configs:
      - targets: ["127.0.0.1:8787"]
```

端点包含采集健康、集群容量、集群与逐节点的原始/可操作告警计数、逐节点可用性与系统资源，以及当前 GPU 利用率、显存、温度、功耗、任务数量和硬件健康指标。它只序列化已有快照，绝不会额外发起探测。陈旧节点资源不会伪装成当前时序，`mocop_host_up` 与 `mocop_host_stale` 仍会保留真实可用性；进程名和 PID 刻意不进入标签，以避免敏感信息与高基数。抓取兼容性见 [Prometheus 官方格式说明](https://prometheus.io/docs/instrumenting/exposition_formats/)。

## 控制台数据

| 区域 | 数据 |
|---|---|
| GPU | 数量、利用率、显存、温度、功耗、型号、驱动、任务、ECC、显存修复与降频状态、MIG 模式 |
| 主机 | 状态、CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率、运行时间 |
| 集群 | 算力匹配与容量汇总、调度热力图、关注队列、健康筛选、搜索 |
| 运维 | 有界趋势、状态变化、重试时间、陈旧标记、CSV 与 OpenMetrics 导出 |

主机失败后保留最后一次成功样本并标记为陈旧。旧值仍可用于诊断，但不会进入当前集群汇总。

## 配置

| 字段 | 作用 | 范围或默认值 |
|---|---|---|
| `hosts` / `exclude_hosts` | OpenSSH 别名白名单与排除项 | 默认空 |
| `auto_discover` | 从 OpenSSH 配置发现明确的 `Host` 别名 | `false` |
| `local_host` | `hosts` 中可绕过 SSH 采集的本机别名 | `null` |
| `expected_gpu_counts` | 按显式主机别名声明预期设备数 | 默认空，每台 0 至 256 |
| `host_overrides` | 可选的逐主机采集周期与完整探测超时 | 默认空，与全局字段范围相同 |
| `maintenance_windows` | 按显式主机别名保存 UTC 到期时间与原因 | 默认空，网页提供 1 小时至 7 天 |
| `host_groups` | 按显式主机别名保存共享导航分组 | 默认空，最长 48 个可见字符 |
| `poll_interval_seconds` | 全局采集周期 | 1 至 3600，默认 5，网页为 2 至 60 |
| `probe_timeout_seconds` | 单台主机完整采集超时 | 2 至 300，可由网页管理 |
| `connect_timeout_seconds` | SSH 建连超时 | 1 至 120，且小于完整超时 |
| `max_output_bytes` | SSH stdout 与 stderr 合计上限 | 64 KiB 至 16 MiB |
| `max_workers` | 主机并发探测数 | 1 至 64，可由网页管理 |
| `listen_host` / `listen_port` | 网页监听地址 | `127.0.0.1:8787` |
| `history_points` | 每台主机保留的成功样本数 | 12 至 8640 |
| `incident_history_points` | 内存中保留的状态变化数 | 20 至 5000 |
| `collection_stale_cycles` | 以采集周期计算的延迟阈值 | 2 至 12 |
| `incidents` | 连续触发、恢复及空闲显存窗口 | 1 至 60 个周期 |
| `thresholds` | CPU、内存、Swap、磁盘、GPU 温度、利用率和显存阈值 | 见示例配置 |

配置按以下顺序查找：

1. `--config`
2. `MOCOP_CONFIG`
3. `$XDG_CONFIG_HOME/mocop/config.json`，或 `~/.config/mocop/config.json`
4. 当前目录的 `config/mocop.json`
5. 包内安全默认配置，其中主机列表为空且只监听回环地址

主机别名只允许字母、数字、点、下划线和连字符。修改文件后重启服务：

```bash
systemctl --user restart mocop.service
```

## 架构

```text
JSON 主机白名单 ──▶ 有界调度器 ──┬──▶ OpenSSH
                                 └──▶ 本机 shell
                                         │
                                 固定只读探针
                           /proc · df · nvidia-smi
                                         │
浏览器 ◀──── SSE / JSON ◀──── 有界内存状态
```

每台远端主机每个周期只使用一次逻辑 SSH 往返；可选的本机目标使用一个有界 shell 进程。基础 GPU 指标、计算任务和可选硬件健康数据位于同一固定探针的独立数据段，因此健康查询失败不会遮蔽系统或基础 GPU 遥测。结果完成后立即发布，所以慢节点不会延迟健康节点。连续失败会退避到最多 60 秒。当前快照、趋势、异常和 OpenMetrics 输出都使用有界内存结构，Mocop 不会持久化这些数据。

实现细节见[系统架构](docs/ARCHITECTURE.md)、[性能验证方法](docs/PERFORMANCE.md)和[仓库结构决策](docs/adr/0001-repository-layout.md)。

## 安全边界

浏览器不能提供任意主机、命令、路径或原始配置，只能添加服务器端刚从 OpenSSH 配置扫描到的明确别名；可识别的 Git/GitHub/GitLab 别名和配置排除项会被拒绝。网页只能为已经显式启用的节点设置有界的可见分组名；可修改的采集策略仅限有界的采集周期、完整探测超时和并发探测数。目标仍然进入本地 JSON 白名单，远端探针固定且版本化。Mocop 强制使用严格主机密钥校验、批处理模式、超时、输出上限、并发上限、私有原子配置写入，并安全渲染不可信的远端文本。

Mocop 没有内置账号系统，默认只监听回环地址。`/metrics` 包含与网页相同类别的运维资产信息；任何远程部署都必须通过反向代理或 VPN 增加 TLS 和身份认证授权。

改变信任边界前请阅读[威胁模型](docs/SECURITY.md)。漏洞报告方式见[安全策略](.github/SECURITY.md)。

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

CI 在 Python 3.10 至 3.14 上执行语法、格式、静态检查和单元测试。独立的源码安装与无头 Chrome 任务会验证完整 GPU 页面、GPU 默认折叠、共享节点分组、居中响应式设置、浏览器偏好持久化、采集策略持久化、SSH 节点管理和采集频率竞态。

提交改动前请阅读[贡献指南](.github/CONTRIBUTING.md)、[变更记录](docs/CHANGELOG.md)和[行为准则](.github/CODE_OF_CONDUCT.md)。

## 许可证

Mocop 使用 [MIT License](LICENSE)。
