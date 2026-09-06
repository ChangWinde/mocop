import assert from "node:assert/strict";

await import("../src/mocop/static/format.js");
await import("../src/mocop/static/attention-groups.js");

const { format } = globalThis.MocopFormat.create();
const SAFE_ALIAS = /^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$/;
const groups = globalThis.MocopAttentionGroups.create({
  format,
  safeStoredHosts: (hosts) => (Array.isArray(hosts) ? hosts : [])
    .filter((host) => typeof host === "string" && SAFE_ALIAS.test(host)),
});

// Conditions as attention.js projects them: kind, id, and for storage the
// shared device key with its usage and priority.
const connectivity = (host) => ({ id: `${host}|connectivity`, kind: "connectivity" });
const storage = (host, usage, severity = "warning") => ({
  id: `${host}|disk`,
  kind: "storage",
  sharedKey: "nfs:/nfs",
  device: "/nfs",
  usage,
  severity,
  priority: severity === "critical" ? 2 : 1,
  frozen: false,
});

{
  // A configured path groups the unreachable hosts under its anchor and marks
  // their connectivity conditions consumed; an unsafe anchor, a non-possible
  // confidence, or fewer than two matching hosts yields nothing.
  const conditionsByHost = new Map([
    ["a-01", [connectivity("a-01")]],
    ["a-02", [connectivity("a-02")]],
    ["a-03", [storage("a-03", 91)]],
  ]);
  const consumed = new Set();
  const issues = groups.sharedPathIssues(conditionsByHost, [
    { kind: "configured_shared_path", confidence: "possible", anchor: "gateway", hosts: ["a-01", "a-02", "a-03"] },
    { kind: "configured_shared_path", confidence: "confirmed", anchor: "other", hosts: ["a-01", "a-02"] },
    { kind: "configured_shared_path", confidence: "possible", anchor: "bad host", hosts: ["a-01", "a-02"] },
    { kind: "unknown_kind", confidence: "possible", anchor: "gateway", hosts: ["a-01", "a-02"] },
  ], consumed);
  assert.equal(issues.length, 1);
  assert.deepEqual(issues[0].hosts, ["a-01", "a-02"]);
  assert.equal(issues[0].sharedLabel, "可能的共享链路");
  assert.equal(issues[0].priority, 3);
  assert.deepEqual(issues[0].messages, ["2 台节点不可达 · 配置路径经过 gateway"]);
  assert.deepEqual([...consumed].sort(), ["a-01|a-01|connectivity", "a-02|a-02|connectivity"]);
}

{
  // A fleet-wide simultaneous loss names no anchor, outranks path groups, and
  // a later group whose hosts it already explains is not listed again.
  const conditionsByHost = new Map([
    ["a-01", [connectivity("a-01")]],
    ["a-02", [connectivity("a-02")]],
    ["c-01", [connectivity("c-01")]],
  ]);
  const consumed = new Set();
  const issues = groups.sharedPathIssues(conditionsByHost, [
    { kind: "simultaneous_connectivity_loss", confidence: "possible", anchor: null, hosts: ["a-01", "a-02", "c-01"] },
    { kind: "configured_shared_path", confidence: "possible", anchor: "gateway", hosts: ["a-01", "a-02"] },
  ], consumed);
  assert.equal(issues.length, 1);
  assert.equal(issues[0].sharedLabel, "疑似监控端链路故障");
  assert.equal(issues[0].priority, 4);
  assert.equal(issues[0].sortName, "");
  assert.deepEqual(issues[0].hosts, ["a-01", "a-02", "c-01"]);
  assert.deepEqual(issues[0].messages, ["3 台节点在同一采集周期内失联 · 先检查监控端上行或共享中继"]);
  // Hosts named by the correlation but without a live connectivity condition
  // drop out; below two the group is not shown at all.
  const stale = groups.sharedPathIssues(
    new Map([["a-01", [connectivity("a-01")]]]),
    [{ kind: "simultaneous_connectivity_loss", confidence: "possible", anchor: null, hosts: ["a-01", "a-02", "c-01"] }],
    new Set(),
  );
  assert.deepEqual(stale, []);
}

{
  // Shared storage: one issue per device reported by two or more hosts, keyed
  // by the hottest occurrence, with each host counted once and every
  // occurrence consumed; a device only one host reports stays with that host.
  const conditionsByHost = new Map([
    ["a-01", [storage("a-01", 91), { ...storage("a-01", 88), id: "a-01|disk-2" }]],
    ["a-02", [storage("a-02", 96, "critical")]],
    ["b-01", [{ ...storage("b-01", 50), sharedKey: "nfs:/other", device: "/other" }]],
  ]);
  const consumed = new Set();
  const issues = groups.sharedStorageIssues(conditionsByHost, consumed);
  assert.equal(issues.length, 1);
  assert.equal(issues[0].sharedLabel, "共享存储");
  assert.deepEqual(issues[0].hosts, ["a-01", "a-02"]);
  assert.equal(issues[0].severity, "critical");
  assert.equal(issues[0].priority, 2);
  assert.deepEqual(issues[0].messages, ["/nfs 96% · 影响 2 台"]);
  assert.equal(issues[0].sortName, "/nfs");
  assert.equal(consumed.size, 3);
  assert.equal(consumed.has("b-01|b-01|disk"), false);
  // A frozen hottest occurrence carries the offline marker.
  const frozen = groups.sharedStorageIssues(
    new Map([
      ["a-01", [{ ...storage("a-01", 91), frozen: true }]],
      ["a-02", [storage("a-02", 80)]],
    ]),
    new Set(),
  );
  assert.deepEqual(frozen[0].messages, ["/nfs 91%（离线前） · 影响 2 台"]);
}

console.log("attention-groups contract ok");
