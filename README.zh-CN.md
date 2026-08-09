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

- GPU 优先：利用率、显存、温度、功耗、型号、驱动和每卡计算任务集中展示
- 集群视角：调度热力图与按主机组织的 GPU 分组，默认折叠，需要时再展开
- 完整上下文：CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率和运行时间
- 舒适交互：服务器拖动排序、浏览器本地偏好、搜索、筛选、有界趋势、异常历史和安全 CSV 导出
- 稳定采集：逐主机发布、有界并发、失败退避、重试倒计时和陈旧数据处理
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

`mocop init` 不会覆盖已有配置。集群变化时直接编辑配置文件，保持自动发现关闭，并且只列出计算节点别名：

```json
{
  "auto_discover": false,
  "hosts": ["gpu-node-01", "gpu-node-02"],
  "exclude_hosts": ["gpu-bastion", "git-host"]
}
```

上面只是资产字段片段，不能替换完整配置；请保留 `mocop init` 生成的其他字段。`exclude_hosts` 是最终拒绝列表，主动启用 `auto_discover` 时尤其适合排除跳板机等非计算节点。编辑后重启服务。

### 临时调整实时采集频率

网页选择器可以把当前进程调整为 2 至 60 秒的任意周期，并立即改变真实 SSH 调度。这个操作不会改写配置文件，因此服务重启后会恢复 `poll_interval_seconds`，其默认值为 5 秒。

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

在右上角 **设置** 中可以选择服务器和 GPU 排序、热力图指标及可见的 GPU 数据列。拖动任意服务器可以保存自定义顺序。这些显示偏好只保存在当前浏览器中，不会修改集群配置。点击 GPU 表格行或热力图单元格，可以查看该卡正在运行的 CUDA 计算任务及每个进程的显存。

### 获取一次快照

一次性采集适合本地检查或受控的自动化流程：

```bash
mocop --once > snapshot.json
```

输出包含资产与遥测，应按照基础设施日志的安全要求保存和删除。

## 控制台数据

| 区域 | 数据 |
|---|---|
| GPU | 数量、利用率、显存、温度、功耗、型号、驱动、每卡计算任务 |
| 主机 | 状态、CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率、运行时间 |
| 集群 | 容量汇总、调度热力图、关注队列、健康筛选、搜索 |
| 运维 | 有界趋势、状态变化、重试时间、陈旧标记、CSV 导出 |

主机失败后保留最后一次成功样本并标记为陈旧。旧值仍可用于诊断，但不会进入当前集群汇总。

## 配置

| 字段 | 作用 | 范围或默认值 |
|---|---|---|
| `hosts` / `exclude_hosts` | OpenSSH 别名白名单与排除项 | 默认空 |
| `auto_discover` | 从 OpenSSH 配置发现明确的 `Host` 别名 | `false` |
| `local_host` | `hosts` 中可绕过 SSH 采集的本机别名 | `null` |
| `poll_interval_seconds` | 进程启动时的采集周期 | 1 至 3600，默认 5 |
| `probe_timeout_seconds` | 单台主机完整采集超时 | 2 至 300 |
| `connect_timeout_seconds` | SSH 建连超时 | 1 至 120，且小于完整超时 |
| `max_output_bytes` | SSH stdout 与 stderr 合计上限 | 64 KiB 至 16 MiB |
| `max_workers` | 主机并发探测数 | 1 至 64 |
| `listen_host` / `listen_port` | 网页监听地址 | `127.0.0.1:8787` |
| `history_points` | 每台主机保留的成功样本数 | 12 至 8640 |
| `incident_history_points` | 内存中保留的状态变化数 | 20 至 5000 |
| `collection_stale_cycles` | 以采集周期计算的延迟阈值 | 2 至 12 |
| `thresholds` | CPU、内存、Swap、磁盘、GPU 温度和繁忙阈值 | 见示例配置 |

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

每台远端主机每个周期只使用一次逻辑 SSH 往返；可选的本机目标使用一个有界 shell 进程。GPU 指标与计算任务在同一次采集中完成。结果完成后立即发布，因此慢节点不会延迟健康节点。连续失败会退避到最多 60 秒。当前快照、趋势和异常都使用有界内存结构，Mocop 不会持久化这些数据。

实现细节见[系统架构](docs/ARCHITECTURE.md)、[性能验证方法](docs/PERFORMANCE.md)和[仓库结构决策](docs/adr/0001-repository-layout.md)。

## 安全边界

浏览器不能添加主机或提供命令。目标只来自本地配置，远端探针固定且版本化。Mocop 强制使用严格主机密钥校验、批处理模式、超时、输出上限、并发上限，并安全渲染不可信的远端文本。

Mocop 没有内置账号系统，默认只监听回环地址。任何远程部署都必须通过反向代理或 VPN 增加 TLS 和身份认证授权。

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

CI 在 Python 3.10 至 3.14 上执行语法、格式、静态检查和单元测试。独立的源码安装与无头 Chrome 任务会验证完整 GPU 页面、默认折叠、响应式布局和采集频率竞态。

提交改动前请阅读[贡献指南](.github/CONTRIBUTING.md)、[变更记录](docs/CHANGELOG.md)和[行为准则](.github/CODE_OF_CONDUCT.md)。

## 许可证

Mocop 使用 [MIT License](LICENSE)。
