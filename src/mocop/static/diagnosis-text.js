// Guidance copy for the incident detail dialog, extracted from incident-text.js
// under the ADR-0021 leaf pattern: fixed Chinese [title, summary, nextSteps]
// per condition category, with the first connectivity step chosen by the
// sanitized failure classification the probe attached to the condition. Pure:
// the caller injects the condition-message formatter, and app.js owns the DOM.
(() => {
  "use strict";

  // Keys are a subset of the failure vocabulary incident-text.js translates;
  // the contract test holds them to it. Unclassified failures keep the generic
  // connectivity steps.
  const CONNECTIVITY_STEPS = {
    "SSH host key changed": "确认节点是否被重装或更换了密钥，核实后再更新监控主机的 known_hosts；探针不会自动接受变化的密钥。",
    "SSH host key is not trusted": "在监控主机上手动 ssh 一次以记录主机密钥；探针不会自行接受新密钥。",
    "SSH authentication failed": "检查监控主机的公钥是否仍在节点 authorized_keys 中，以及别名的 IdentityFile 或 agent 是否仍提供该密钥。",
    "SSH name resolution failed": "检查别名的 HostName 与监控主机的 DNS 解析。",
    "SSH jump host could not reach the target": "在跳板机上直接测试目标节点的 SSH 端口，并确认其 sshd 允许 TCP 转发（AllowTcpForwarding、PermitOpen）。",
    "SSH connection was refused": "确认节点上的 sshd 正在运行且监听在配置的端口。",
    "SSH connection timed out": "检查监控主机到节点 SSH 端口之间的路由与防火墙规则。",
    "SSH network is unreachable": "检查监控主机的路由，以及别名依赖的 VPN 或隧道。",
    "SSH connection closed during key exchange": "检查节点 sshd 的负载与 MaxStartups，以及 fail2ban 或前置代理是否封禁了监控主机。",
    "SSH transport stopped responding": "节点可能已挂起或链路中断；检查电源、控制台与上联链路。",
    "SSH produced no output before the collection timeout": "登录节点检查是否卡在 I/O 或挂起的文件系统（例如无响应的网络挂载）。",
  };

  const GENERIC_CONNECTIVITY_STEPS = [
    "使用同一 OpenSSH 别名验证非交互连接。",
    "若多台节点同时失败，优先检查共享跳板机或隧道。",
  ];

  function create({ incidentConditionMessage }) {
    // [title, summary, nextSteps]; categories without fixed guidance fall
    // back to what the server diagnosed.
    function localizedDiagnosis(condition) {
      const resource = condition.resource || "资源";
      const specific = CONNECTIVITY_STEPS[condition.detail];
      const connectivitySteps = specific
        ? [specific, ...GENERIC_CONNECTIVITY_STEPS]
        : [...GENERIC_CONNECTIVITY_STEPS];
      const descriptions = {
        connectivity: ["采集链路不可用", "固定 SSH 探针未能完成，当前资源数据不可用。", connectivitySteps],
        disk: ["文件系统空间不足", `${resource} 已超过配置的使用率阈值。`, ["确认该文件系统是否仍在按预期增长。", "检查大目录以及日志、缓存和检查点保留策略。"]],
        swap: ["Swap 压力偏高", "Swap 使用率超过阈值，可能存在持续内存压力。", ["对照可用内存和当前任务规模。", "观察 Swap 是否持续增长或已趋于稳定。"]],
        memory: ["内存压力偏高", "内存使用率超过配置阈值。", ["检查当前节点上的主要任务。", "观察采样之间的可用内存是否恢复。"]],
        pressure: ["资源饱和导致任务等待", `${resource} 的等待比例（PSI）超过阈值，任务正在排队等待该资源。`, ["对照同一时段的 CPU、内存与 I/O 使用率。", "检查是否有任务占满磁盘带宽或触发内存回收。"]],
        cpu: ["CPU 负载偏高", "CPU 使用率超过配置阈值。", ["确认数据加载或预处理是否限制 GPU。", "对照同一时段的 CPU 与 GPU 利用率。"]],
        gpu_memory: ["显存占用偏高", `${resource} 使用率超过配置阈值。`, ["确认任务的批大小与显存预算。", "检查是否有残留进程仍占着显存。"]],
        gpu_idle_memory: ["显存占用但计算空闲", "显存仍被进程占用，但 GPU 计算负载持续低于忙碌阈值。", ["查看该 GPU 的进程与任务归属。", "确认进程是在正常等待，还是已经停滞。"]],
        gpu_processes: ["GPU 进程不可见", "GPU 指标照常更新，但 nvidia-smi 没有返回进程列表，占用与归属暂时无法判断。", ["在节点上运行 nvidia-smi --query-compute-apps 检查输出。", "容器或权限隔离会隐藏其他用户的进程；确认采集账号的权限。"]],
        gpu_temperature: ["GPU 温度偏高", "GPU 温度超过配置的警告阈值。", ["检查散热、风扇和相邻设备温度。", "继续长任务前确认硬件降频状态。"]],
        gpu_availability: condition.detail === "nvidia-smi is stuck in the NVIDIA driver"
          ? ["NVIDIA 驱动已挂起", "此前的 nvidia-smi 卡在 NVIDIA 驱动中（不可中断睡眠，状态 D）。在它消失前，Mocop 不在该节点启动任何 NVIDIA 命令；系统指标照常更新。", [
            "重启节点：状态 D 的进程不响应任何信号，SIGKILL 也无法结束它。",
            "重启前保存内核日志（dmesg），查找指明故障 GPU 的 NVRM 或 Xid 消息。",
            "先排空或保存 GPU 任务；重启前这些 GPU 不受监控。",
          ]]
          : ["NVIDIA 遥测不可用", "节点在线，但 nvidia-smi 没有返回可用的 GPU 数据。", ["在节点上运行 nvidia-smi，检查退出码与输出。", "确认 NVIDIA 驱动已加载，且与安装的 nvidia-smi 版本一致。"]],
        disk_availability: ["文件系统已挂起", "df 卡在挂起的文件系统上（不可中断睡眠，状态 D），通常是无响应的 NFS、CIFS 或 Lustre 挂载。在它消失前，Mocop 不再运行 df，磁盘数据与磁盘告警暂停更新。", [
          "在节点上查看 /proc/mounts 中的网络文件系统，并检查对应存储服务器是否可达。",
          "存储恢复后卡住的 df 会自行退出；无法恢复时用 umount -f -l 卸载该挂载，或重启节点。",
        ]],
        gpu_count: ["GPU 数量与配置不一致", "当前可见 GPU 数量与 expected_gpu_counts 不一致。", ["检查设备可见性和驱动初始化。", "仅在硬件确实调整后修改预期数量。"]],
        gpu_ecc: ["GPU 硬件健康异常", "检测到未纠正 ECC 错误。", ["保留任务上下文并按集群硬件维护流程处理。"]],
        gpu_memory_repair: ["GPU 显存需要修复", "硬件遥测报告待处理的显存修复状态。", ["保留任务上下文并按集群硬件维护流程处理。"]],
        gpu_slowdown: ["GPU 已触发硬件降频", "温度或功率相关硬件状态触发了降频。", ["检查温度、功耗上限和散热条件。"]],
      };
      return descriptions[condition.category] || [
        condition.diagnosis?.title || "资源状态需要处理",
        condition.diagnosis?.summary || incidentConditionMessage(condition),
        condition.diagnosis?.nextSteps || ["确认当前状态是否符合任务预期。"],
      ];
    }

    return Object.freeze({ localizedDiagnosis });
  }

  globalThis.MocopDiagnosisText = Object.freeze({
    create,
    CONNECTIVITY_MESSAGES: Object.freeze(Object.keys(CONNECTIVITY_STEPS)),
  });
})();
