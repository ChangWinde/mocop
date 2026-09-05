// The dashboard's wording for collector failures and incident conditions,
// extracted from app.js under the ADR-0021 leaf pattern. The failure table is
// the Chinese counterpart of the exact messages the probe attaches to a host
// or GPU result; a repository test keeps the two vocabularies aligned so a new
// backend message cannot reach the operator untranslated. Dialog guidance
// lives in diagnosis-text.js. Pure: the caller injects the formatter, and
// app.js owns every DOM node the text lands in.
(() => {
  "use strict";

  const FAILURE_TEXT = {
    "SSH host key changed": "SSH 主机密钥发生变化",
    "SSH host key is not trusted": "SSH 主机密钥尚未信任",
    "SSH authentication failed": "SSH 身份认证失败",
    "SSH name resolution failed": "SSH 主机名解析失败",
    "SSH jump host could not reach the target": "SSH 跳板机无法连到目标节点",
    "SSH connection was refused": "SSH 连接被拒绝",
    "SSH connection timed out": "SSH 连接超时",
    "SSH network is unreachable": "SSH 网络不可达",
    "SSH connection closed during key exchange": "SSH 在密钥交换阶段被对端关闭",
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

    return Object.freeze({
      failureText,
      incidentConditionMessage,
      incidentStateLabel,
      incidentDescription,
      diagnosticEvidenceLabel,
      diagnosticEvidenceValue,
    });
  }

  globalThis.MocopIncidentText = Object.freeze({
    create,
    FAILURE_TEXT,
    FAILURE_PREFIXES: Object.freeze(FAILURE_PREFIXES.map(([prefix]) => prefix)),
  });
})();
