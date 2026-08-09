# mocop architecture

## 目标与约束

- 使用已经存在的 OpenSSH 别名和凭据，不要求在远端部署 Agent。
- 10–200 台服务器可以并发采集，单台故障不阻塞其他服务器的结果。
- 页面在一次加载后持续收到最新状态；浏览器只能调整有界的运行时采集周期，不能控制 SSH 目标、脚本或命令。
- 安装包无本地资产信息；空白安装可以用安全默认配置启动，不依赖外部 CDN。
- AI-native 表示围绕训练/推理集群的 GPU 容量、显存和调度工作流设计；核心采集路径不依赖外部 AI API，也不把遥测发送到第三方。

## 组件

```text
local mocop JSON ── explicit hosts allowlist
          │ optional auto-discovery from ~/.ssh/config
          │
          ▼
  HostSource Protocol ── openssh-config
          │ literal aliases only
          ▼
   MonitorService ───────────────┐
          │ bounded thread pool  │
          ▼                      │
  ResourceProbe Protocol         │
    openssh-linux-v2             │
          │ fixed argv + script  │
          ▼                      │
 OpenSSH → /proc + /sys/block + df + nvidia-smi
          │ typed ProbeResult    │
          ▼                      │
      StateStore ◄───────────────┘
       │       │        │
 current    bounded    readiness
 snapshot   history
       │       └── IncidentPolicy → bounded transition ring
       ├── JSON / SSE ── HTTP dashboard
       └── bounded runtime cadence ◄── strict same-origin POST
```

依赖方向是 `web → StateStore ← service → protocols/models/config`。网页层不知道 SSH 实现；调度层通过 `HostSource` 和 `ResourceProbe` Protocol 使用注册表工厂创建环境相关实现，因此后续可以换成 Agent、Prometheus 或其他操作系统/硬件采集器。

## 边界与数据格式

- 运维配置边界：JSON；启动时严格校验全部键、类型和范围。优先读取显式参数、环境变量和标准用户配置目录，源码目录只作为开发兼容路径，最后回退到包内空白名单。真实资产配置不进入发布产物。
- 采集边界：不可变 `ProbeResult` / `SystemMetrics` / `DiskMetrics` / `GpuMetrics` 数据类。
- 浏览器边界：UTF-8 JSON 快照通过 SSE 推送；`/api/snapshot` 提供冷启动或诊断读取；`/api/history` 只允许查询已发现的安全别名，返回数量上限为 300；`/api/incidents` 只接受 1–200 的数量参数。唯一写入口 `POST /api/settings/poll-interval` 只接受有效 Origin、自定义请求头、非跨站 Fetch Metadata、128 字节内且 schema 精确的 JSON 数字，范围 2–60 秒；CORS 预检始终拒绝，因此浏览器跨站脚本不能发送该非简单 POST，正常的同源反向代理可以改写内部 Host。
- 远端边界：OpenSSH 参数数组和通过 stdin 提交的固定 `MONITOR_V2` 只读脚本。系统指标使用版本化 Tab 分隔协议，GPU 区段使用标准 CSV；版本、列数、数值范围和 GPU index 会校验。stdout 与 stderr 由选择器增量读取并共享一个配置上限，超时或超限会终止独立 SSH 进程组。

状态存储保留当前快照、每台服务器有上限的成功样本历史和有界异常转换环，不做磁盘持久化。趋势和事件正文不进入 SSE 广播，只在浏览器需要时按需读取；SSE 仅携带很小的事件版本和活动计数。每台服务器完成时立即发布一个新版本，因此慢主机不会延迟快主机在页面上的更新。

`IncidentPolicy` 把成功样本转换成稳定的 CPU、内存、Swap、文件系统与 GPU 温度条件键，把失败样本转换成连接条件。`IncidentTracker` 只记录条件打开、恢复或严重度变化；失败期间保留旧资源条件，避免缺少新数据被误判为恢复。采集循环结束时，权威完成时间与耗时原子写入并立即发布 SSE，避免页面把上一轮元数据保留到下一轮开始。

浏览器把 GPU 数量、繁忙卡数和集群显存放在核心摘要，随后依次呈现调度热力图、通用系统资源和按服务器组织的原生可折叠 GPU 清单。全局分组初始收起；搜索或状态筛选临时展开命中组，用户手动展开状态由独立集合维护。热力图按主机与指标签名键控复用；切换计算/显存/温度不访问服务端。SSE 事件通过 `requestAnimationFrame` 合并同帧更新；GPU 组、服务器条目、问题面板和事件面板按实际依赖生成签名并复用，无变化事件不会重建对应 DOM。事件版本变化时才按需读取时间线。CSV 由当前可见行在浏览器内生成，不新增服务端数据面。

## 安装与进程模型

包安装与系统服务管理严格分离。`mocop init` 以排他创建和 `0600` 权限生成用户配置；`mocop service install` 校验配置后生成当前 Python 环境对应的用户级 systemd unit，显式启用并重启服务。不使用系统级服务，是因为 SSH key、agent、`known_hosts` 和 OpenSSH 配置都属于当前用户身份。包安装阶段不运行生命周期脚本，卸载服务也不会删除操作者配置。

服务 unit 使用固定 `systemctl --user` 参数且不经过 shell；路径拒绝控制字符并按 systemd 规则转义。加固项包括 `NoNewPrivileges`、私有临时目录、只读系统路径、受限地址族和 `UMask=0077`，同时保留读取用户 SSH 资产所需的权限。

## 运行与故障模型

- 每轮重新计算配置主机集合；白名单或已明确启用的 SSH 自动发现变化在下一周期生效。
- `StateStore` 保存当前采集周期和由其换算的新鲜度窗口。合法运行时修改会发布 SSE 并唤醒等待中的调度器；不写配置文件，重启后恢复配置值。
- 每台探测有连接超时和总超时；线程池并发数受配置限制。
- 连续失败目标按指数退避到最多 60 秒；健康目标仍按正常周期采集，成功后立即解除退避。运行时周期变化会按原失败时刻重算既有截止时间。调度使用单调时钟，同时把同一延迟换算成只读 `nextRetryAt` 供页面准确倒计时。
- SSH 退出码、命令缺失和 CSV 格式错误被映射为有限状态；原始 stderr 不跨越浏览器边界。
- 失败后保留最后成功数据并标记为陈旧，实时汇总只纳入当前 `online` 节点。
- SSE 每 15 秒发送心跳，浏览器使用原生 `EventSource` 自动重连。
- `/healthz` 是存活检查；`/readyz` 在没有发现目标或尚无成功样本时返回 503。
- 服务默认回环监听。远程访问的 TLS、身份认证和授权属于部署代理责任。

采集器主要成本是 SSH 网络等待，当前基线不支持为了性能重写为 Rust。只有达到 200 台、低于 2 秒采样、单核饱和或 512 MiB RSS 等已定义阈值时，才应在相同工作负载上重新测量并评估语言或 Agent 架构。性能证据见 [docs/PERFORMANCE.md](docs/PERFORMANCE.md)，威胁模型见 [docs/SECURITY.md](docs/SECURITY.md)。
