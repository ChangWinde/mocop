// The dashboard's wording for collector failures and incident conditions,
// extracted from app.js under the ADR-0021 leaf pattern. The failure table is
// the Chinese counterpart of the exact messages the probe attaches to a host
// or GPU result; a repository test keeps the two vocabularies aligned so a new
// backend message cannot reach the operator untranslated. Pure: the caller
// injects the formatter, and app.js owns every DOM node the text lands in.
(() => {
  "use strict";

  const FAILURE_TEXT = {
    "SSH host key changed": "SSH 主机密钥发生变化",
    "SSH host key is not trusted": "SSH 主机密钥尚未信任",
    "SSH authentication failed": "SSH 身份认证失败",
    "SSH name resolution failed": "SSH 主机名解析失败",
    "SSH connection was refused": "SSH 连接被拒绝",
    "SSH connection timed out": "SSH 连接超时",
    "SSH network is unreachable": "SSH 网络不可达",
    "SSH connection failed": "SSH 连接失败",
    "SSH transport stopped responding": "SSH 传输失去响应（keepalive 超时）",
    "SSH produced no output before the collection timeout": "SSH 在采集超时前无任何输出",
    "Local SSH client could not be started": "本机 SSH 客户端无法启动",
    "Local resource collection timed out": "本机资源采集超时",
    "Local resource probe could not be started": "本机资源探针无法启动",
    "Local resource output was not recognized": "本机资源数据格式异常",
    "Local resource output exceeded the configured limit": "本机资源输出超过安全上限",
    "Remote resource output was not recognized": "远端资源数据格式异常",
    "Remote resource output exceeded the configured limit": "远端资源输出超过安全上限",
    "Remote collection stalled after partial output": "远端采集在部分输出后停滞",
    "Resource collection cancelled": "资源采集已取消",
    "Unexpected collector error": "采集器发生未预期错误",
    "nvidia-smi is unavailable": "系统在线，但未安装 nvidia-smi",
    "nvidia-smi query failed": "系统在线，但 GPU 查询失败",
    "nvidia-smi output was malformed": "系统在线，但 GPU 数据格式异常",
  };

  // Messages carrying a dynamic exit code only match by prefix; the original
  // parenthesised detail is preserved verbatim.
  const FAILURE_PREFIXES = [
    ["Remote resource query failed", "远端资源查询失败"],
    ["Local resource query failed", "本机资源查询失败"],
  ];

  const STATE_LABELS = {
    opened: "触发",
    resolved: "已恢复",
    escalated: "升级",
    deescalated: "已降级",
  };

  const EVIDENCE_LABELS = {
    current: "当前值",
    threshold: "告警阈值",
    consecutiveFailures: "连续失败",
    lastSuccessAt: "最近成功",
    gpuUtilizationPct: "GPU 负载",
    memoryUsedMiB: "进程显存",
    processCount: "活跃进程",
  };

  function create({ format, numeric, age }) {
    function failureText(message) {
      if (FAILURE_TEXT[message]) return FAILURE_TEXT[message];
      if (typeof message === "string") {
        const prefixed = FAILURE_PREFIXES.find(([prefix]) => message.startsWith(prefix));
        if (prefixed) return prefixed[1] + message.slice(prefixed[0].length);
      }
      return message || "采集失败";
    }

    function incidentConditionMessage(condition) {
      const value = condition.value == null ? null : numeric(condition.value);
      const expected = condition.threshold == null ? null : numeric(condition.threshold);
      const resource = condition.resource || "资源";
      if (condition.category === "connectivity") return failureText(condition.detail);
      if (condition.category === "gpu_availability") return failureText(condition.detail);
      if (condition.category === "gpu_count") {
        return `GPU 数量 ${format(value)} / 预期 ${format(expected)}`;
      }
      if (condition.category === "gpu_processes") return `${resource} 数据不可用`;
      if (condition.category === "gpu_ecc") return `${resource} · ${format(value)} 个未纠正错误`;
      if (condition.category === "gpu_memory_repair") return `${resource} · 存在待处理显存修复`;
      if (condition.category === "gpu_slowdown") return `${resource} · 硬件降频已触发`;
      if (condition.category === "gpu_idle_memory") {
        return `${resource} ${format(value, 1)}% · 持续低负载`;
      }
      if (condition.category === "gpu_temperature") return `${resource} ${format(value, 1)}°C`;
      if (value != null) return `${resource} ${format(value, 1)}%`;
      return condition.detail || resource;
    }

    function incidentStateLabel(state) {
      return STATE_LABELS[state] || "变化";
    }

    function incidentDescription(event) {
      return event.state === "resolved"
        ? `${event.resource} 恢复正常`
        : incidentConditionMessage(event);
    }

    function diagnosticEvidenceLabel(label) {
      return EVIDENCE_LABELS[label] || label;
    }

    function diagnosticEvidenceValue(item) {
      if (item.value == null) return "—";
      if (item.label === "lastSuccessAt") return age(item.value);
      if (typeof item.value === "number") {
        return `${format(item.value, 1)}${item.unit || ""}`;
      }
      return String(item.value);
    }

    // [title, summary, nextSteps] for the incident detail dialog; categories
    // without fixed guidance fall back to what the server diagnosed.
    function localizedDiagnosis(condition) {
      const resource = condition.resource || "资源";
      const descriptions = {
        connectivity: [
          "采集链路不可用",
          "固定 SSH 探针未能完成，当前资源数据不可用。",
          ["使用同一 OpenSSH 别名验证非交互连接。", "若多台节点同时失败，优先检查共享跳板机或隧道。"],
        ],
        disk: [
          "文件系统空间不足",
          `${resource} 已超过配置的使用率阈值。`,
          ["确认该文件系统是否仍在按预期增长。", "检查大目录以及日志、缓存和检查点保留策略。"],
        ],
        swap: [
          "Swap 压力偏高",
          "Swap 使用率超过阈值，可能存在持续内存压力。",
          ["对照可用内存和当前任务规模。", "观察 Swap 是否持续增长或已趋于稳定。"],
        ],
        memory: [
          "内存压力偏高",
          "内存使用率超过配置阈值。",
          ["检查当前节点上的主要任务。", "观察采样之间的可用内存是否恢复。"],
        ],
        cpu: [
          "CPU 负载偏高",
          "CPU 使用率超过配置阈值。",
          ["确认数据加载或预处理是否限制 GPU。", "对照同一时段的 CPU 与 GPU 利用率。"],
        ],
        gpu_idle_memory: [
          "显存占用但计算空闲",
          "显存仍被进程占用，但 GPU 计算负载持续低于忙碌阈值。",
          ["查看该 GPU 的进程与任务归属。", "确认进程是在正常等待，还是已经停滞。"],
        ],
        gpu_temperature: [
          "GPU 温度偏高",
          "GPU 温度超过配置的警告阈值。",
          ["检查散热、风扇和相邻设备温度。", "继续长任务前确认硬件降频状态。"],
        ],
        gpu_count: [
          "GPU 数量与配置不一致",
          "当前可见 GPU 数量与 expected_gpu_counts 不一致。",
          ["检查设备可见性和驱动初始化。", "仅在硬件确实调整后修改预期数量。"],
        ],
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

    return Object.freeze({
      failureText,
      incidentConditionMessage,
      incidentStateLabel,
      incidentDescription,
      diagnosticEvidenceLabel,
      diagnosticEvidenceValue,
      localizedDiagnosis,
    });
  }

  globalThis.MocopIncidentText = Object.freeze({
    create,
    FAILURE_TEXT,
    FAILURE_PREFIXES: Object.freeze(FAILURE_PREFIXES.map(([prefix]) => prefix)),
  });
})();
