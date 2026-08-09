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

- GPU 优先：利用率、显存、温度、功耗、型号、驱动和进程状态集中展示
- 集群视角：调度热力图与按主机组织的 GPU 分组，默认折叠，需要时再展开
- 完整上下文：CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率和运行时间
- 清晰诊断：搜索、健康筛选、排序、有界趋势、异常历史和安全 CSV 导出
- 稳定采集：逐主机发布、有界并发、失败退避、重试倒计时和陈旧数据处理
- 安全默认值：显式主机白名单、回环监听、严格主机密钥校验、固定远端脚本和资源上限

## 环境要求

- Mocop 所在机器使用 Linux 和 Python 3.10 或更高版本
- OpenSSH 客户端
- 可以通过密钥或 `ssh-agent` 非交互访问目标主机
- 目标主机提供 Linux `/proc`
- 需要 NVIDIA GPU 指标的主机提供 `nvidia-smi`

开始无人值守监控前，请人工核对每台主机的指纹。

## 快速开始

使用 [`uv`](https://docs.astral.sh/uv/) 创建隔离的命令环境：

```bash
uv tool install git+https://github.com/ChangWinde/mocop.git
```

创建显式资产清单并安装用户级 systemd 服务：

```bash
mocop init --host gpu-node-01 --host gpu-node-02
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

### 只监控明确指定的主机

`mocop init` 以 `0600` 权限创建 `~/.config/mocop/config.json`，并拒绝覆盖已有文件。建议关闭自动发现，只列出属于目标集群的 OpenSSH 别名：

```json
{
  "auto_discover": false,
  "hosts": ["gpu-node-01", "gpu-node-02"],
  "exclude_hosts": []
}
```

上面的片段只展示资产字段。完整配置请从不含真实资产的[示例配置](examples/mocop.example.json)开始。

### 临时调整实时采集频率

网页选择器可以把当前进程调整为 2 至 60 秒的任意周期，并立即改变真实 SSH 调度。这个操作不会改写配置文件，因此服务重启后会恢复 `poll_interval_seconds`，其默认值为 5 秒。

### 获取一次快照

一次性采集适合本地检查或受控的自动化流程：

```bash
mocop --once > snapshot.json
```

输出包含资产与遥测，应按照基础设施日志的安全要求保存和删除。

## 控制台数据

| 区域 | 数据 |
|---|---|
| GPU | 数量、利用率、显存、温度、功耗、型号、驱动、进程 |
| 主机 | 状态、CPU、Load、内存、Swap、磁盘容量与 I/O、网络速率、运行时间 |
| 集群 | 容量汇总、调度热力图、关注队列、健康筛选、搜索 |
| 运维 | 有界趋势、状态变化、重试时间、陈旧标记、CSV 导出 |

主机失败后保留最后一次成功样本并标记为陈旧。旧值仍可用于诊断，但不会进入当前集群汇总。

## 配置

| 字段 | 作用 | 范围或默认值 |
|---|---|---|
| `hosts` / `exclude_hosts` | OpenSSH 别名白名单与排除项 | 默认空 |
| `auto_discover` | 从 OpenSSH 配置发现明确的 `Host` 别名 | `false` |
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
JSON 主机白名单 ──▶ 有界调度器 ──▶ OpenSSH
                                          │
                                  固定只读探针
                            /proc · df · nvidia-smi
                                          │
浏览器 ◀── SSE / JSON ◀── 有界内存状态
```

每台主机每个周期只使用一次逻辑 SSH 往返。结果完成后立即发布，因此慢节点不会延迟健康节点。连续失败会退避到最多 60 秒。当前快照、趋势和异常都使用有界内存结构，Mocop 不会持久化这些数据。

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
