import assert from "node:assert/strict";

await import("../src/mocop/static/format.js");
await import("../src/mocop/static/incident-text.js");
await import("../src/mocop/static/diagnosis-text.js");

const { format, numeric, age } = globalThis.MocopFormat.create();
const { incidentConditionMessage } = globalThis.MocopIncidentText.create({ format, numeric, age });
const { localizedDiagnosis } = globalThis.MocopDiagnosisText.create({ incidentConditionMessage });

{
  // Fixed copy per known category with the resource interpolated where it
  // matters, and the server's own diagnosis for everything else.
  const [title, summary, steps] = localizedDiagnosis({ category: "disk", resource: "/scratch" });
  assert.equal(title, "文件系统空间不足");
  assert.equal(summary, "/scratch 已超过配置的使用率阈值。");
  assert.equal(steps.length, 2);
  for (const category of ["connectivity", "swap", "memory", "cpu", "gpu_idle_memory", "gpu_temperature", "gpu_count", "gpu_ecc", "gpu_memory_repair", "gpu_slowdown"]) {
    const [heading, body, next] = localizedDiagnosis({ category, resource: "x" });
    assert.ok(heading && body && next.length >= 1, category);
  }
  assert.deepEqual(
    localizedDiagnosis({
      category: "pressure",
      resource: "内存压力",
      value: 42,
      diagnosis: { title: "服务端标题", summary: "服务端摘要", nextSteps: ["第一步"] },
    }),
    ["服务端标题", "服务端摘要", ["第一步"]],
  );
  assert.deepEqual(
    localizedDiagnosis({ category: "pressure", resource: "内存压力", value: 42 }),
    ["资源状态需要处理", "内存压力 42%", ["确认当前状态是否符合任务预期。"]],
  );
}

{
  // Connectivity guidance opens with the step that follows from the failure
  // classification in the condition's detail; unclassified failures keep the
  // two generic steps. Every classified message is one the failure table
  // translates, so the Python vocabulary drift test covers this map too.
  const generic = localizedDiagnosis({ category: "connectivity", resource: "SSH" })[2];
  assert.equal(generic.length, 2);
  const fallback = localizedDiagnosis({ category: "connectivity", resource: "SSH", detail: "SSH connection failed" })[2];
  assert.deepEqual(fallback, generic);
  const jump = localizedDiagnosis({
    category: "connectivity",
    resource: "SSH",
    detail: "SSH jump host could not reach the target",
  })[2];
  assert.equal(jump.length, 3);
  assert.match(jump[0], /跳板机/);
  assert.deepEqual(jump.slice(1), generic);
  const { CONNECTIVITY_MESSAGES } = globalThis.MocopDiagnosisText;
  assert.ok(CONNECTIVITY_MESSAGES.length >= 10);
  for (const message of CONNECTIVITY_MESSAGES) {
    assert.ok(message in globalThis.MocopIncidentText.FAILURE_TEXT, message);
    const steps = localizedDiagnosis({ category: "connectivity", resource: "SSH", detail: message })[2];
    assert.equal(steps.length, 3, message);
  }
  // Repeated calls never share a mutable steps array.
  const first = localizedDiagnosis({ category: "connectivity", resource: "SSH" })[2];
  first.push("mutated");
  assert.equal(localizedDiagnosis({ category: "connectivity", resource: "SSH" })[2].length, 2);
}

console.log("diagnosis-text contract ok");
