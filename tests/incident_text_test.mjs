import assert from "node:assert/strict";

await import("../src/mocop/static/format.js");
await import("../src/mocop/static/incident-text.js");

const { format, numeric, age } = globalThis.MocopFormat.create();
const text = globalThis.MocopIncidentText.create({ format, numeric, age });
const NOW = Date.parse("2026-08-14T03:00:00Z");

{
  // Exact backend messages translate, exit-code messages match by prefix and
  // keep their detail, anything unknown passes through, and nothing at all
  // becomes the generic failure.
  assert.equal(text.failureText("SSH connection timed out"), "SSH 连接超时");
  assert.equal(text.failureText("nvidia-smi output was malformed"), "系统在线，但 GPU 数据格式异常");
  assert.equal(text.failureText("Remote resource query failed (exit 137)"), "远端资源查询失败 (exit 137)");
  assert.equal(text.failureText("Local resource query failed (exit 1)"), "本机资源查询失败 (exit 1)");
  assert.equal(text.failureText("Some new backend message"), "Some new backend message");
  assert.equal(text.failureText(null), "采集失败");
  assert.equal(text.failureText(""), "采集失败");
  // The exported vocabulary is what the Python side is compared against.
  assert.ok(Object.keys(globalThis.MocopIncidentText.FAILURE_TEXT).includes("SSH host key changed"));
  assert.deepEqual(globalThis.MocopIncidentText.FAILURE_PREFIXES, [
    "Remote resource query failed",
    "Local resource query failed",
  ]);
}

{
  // Condition wording per category; percentages and counts format through
  // the shared formatter, and connectivity reuses the failure table.
  const message = (category, extra = {}) => text.incidentConditionMessage({
    category, resource: "GPU 3", value: 87.456, threshold: 8, detail: "SSH connection was refused", ...extra,
  });
  assert.equal(message("connectivity"), "SSH 连接被拒绝");
  assert.equal(message("gpu_availability", { detail: "nvidia-smi is unavailable" }), "系统在线，但未安装 nvidia-smi");
  assert.equal(message("gpu_count", { value: 6 }), "GPU 数量 6 / 预期 8");
  assert.equal(message("gpu_processes"), "GPU 3 数据不可用");
  assert.equal(message("gpu_ecc", { value: 2 }), "GPU 3 · 2 个未纠正错误");
  assert.equal(message("gpu_memory_repair"), "GPU 3 · 存在待处理显存修复");
  assert.equal(message("gpu_slowdown"), "GPU 3 · 硬件降频已触发");
  assert.equal(message("gpu_idle_memory"), "GPU 3 87.5% · 持续低负载");
  assert.equal(message("gpu_temperature"), "GPU 3 87.5°C");
  assert.equal(message("disk", { resource: "/data" }), "/data 87.5%");
  assert.equal(message("cpu", { resource: null }), "资源 87.5%");
  assert.equal(message("custom", { value: null, detail: "something" }), "something");
  assert.equal(message("custom", { value: null, detail: null, resource: null }), "资源");
}

{
  // Event descriptions and state labels.
  assert.equal(text.incidentDescription({ state: "resolved", resource: "/data" }), "/data 恢复正常");
  assert.equal(
    text.incidentDescription({ state: "opened", category: "memory", resource: "内存", value: 93.2 }),
    "内存 93.2%",
  );
  assert.deepEqual(
    ["opened", "resolved", "escalated", "deescalated", "other"].map(text.incidentStateLabel),
    ["触发", "已恢复", "升级", "已降级", "变化"],
  );
}

{
  // Evidence rows: known labels translate, unknown ones pass through; values
  // format numbers with their unit, timestamps as ages, and null as a dash.
  assert.equal(text.diagnosticEvidenceLabel("threshold"), "告警阈值");
  assert.equal(text.diagnosticEvidenceLabel("customMetric"), "customMetric");
  assert.equal(text.diagnosticEvidenceValue({ label: "current", value: null }), "—");
  assert.equal(text.diagnosticEvidenceValue({ label: "current", value: 91.26, unit: "%" }), "91.3%");
  assert.equal(text.diagnosticEvidenceValue({ label: "processCount", value: 4 }), "4");
  assert.equal(text.diagnosticEvidenceValue({ label: "state", value: "P8" }), "P8");
  assert.match(text.diagnosticEvidenceValue({ label: "lastSuccessAt", value: new Date(NOW).toISOString() }), /前|刚刚/);
}

{
  // Diagnosis guidance: fixed copy per known category with the resource
  // interpolated where it matters, and the server's own diagnosis for others.
  const [title, summary, steps] = text.localizedDiagnosis({ category: "disk", resource: "/scratch" });
  assert.equal(title, "文件系统空间不足");
  assert.equal(summary, "/scratch 已超过配置的使用率阈值。");
  assert.equal(steps.length, 2);
  for (const category of ["connectivity", "swap", "memory", "cpu", "gpu_idle_memory", "gpu_temperature", "gpu_count", "gpu_ecc", "gpu_memory_repair", "gpu_slowdown"]) {
    const [heading, body, next] = text.localizedDiagnosis({ category, resource: "x" });
    assert.ok(heading && body && next.length >= 1, category);
  }
  assert.deepEqual(
    text.localizedDiagnosis({
      category: "pressure",
      resource: "内存压力",
      value: 42,
      diagnosis: { title: "服务端标题", summary: "服务端摘要", nextSteps: ["第一步"] },
    }),
    ["服务端标题", "服务端摘要", ["第一步"]],
  );
  assert.deepEqual(
    text.localizedDiagnosis({ category: "pressure", resource: "内存压力", value: 42 }),
    ["资源状态需要处理", "内存压力 42%", ["确认当前状态是否符合任务预期。"]],
  );
}

console.log("incident-text contract ok");
