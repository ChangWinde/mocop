import assert from "node:assert/strict";

await import("../src/mocop/static/format.js");
await import("../src/mocop/static/attention-groups.js");
await import("../src/mocop/static/attention.js");

const { format, numeric } = globalThis.MocopFormat.create();
const SAFE_ALIAS = /^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$/;
const safeStoredHosts = (hosts) => (Array.isArray(hosts) ? hosts : [])
  .filter((host) => typeof host === "string" && SAFE_ALIAS.test(host));
const attention = globalThis.MocopAttention.create({
  format,
  numeric,
  safeStoredHosts,
  conditionMessage: (condition) => `msg:${condition.conditionKey}`,
  groups: globalThis.MocopAttentionGroups.create({ format, safeStoredHosts }),
});

const server = (host, status = "online") => ({ host, status });
const condition = (conditionKey, category, overrides = {}) => ({
  conditionKey,
  category,
  severity: "warning",
  resource: "",
  value: null,
  groupKey: null,
  actionable: true,
  ...overrides,
});

{
  // Host conditions: silenced (non-actionable) ones drop out, connectivity
  // outranks critical resources, a host that is back online explains its
  // lingering connectivity condition, and usage falls back to -1.
  const conditions = attention.serverConditions(server("gpu-01"), [
    condition("connectivity", "connectivity", { severity: "critical", value: "3" }),
    condition("disk:/data", "disk", { resource: "/data", value: 91.5, groupKey: "nfs:/data" }),
    condition("cpu", "cpu", { severity: "critical" }),
    condition("gpu-idle", "gpu_idle_memory", { actionable: false }),
  ]);
  assert.deepEqual(
    conditions.map(({ id, kind, priority, message, device, usage, sharedKey }) => ({ id, kind, priority, message, device, usage, sharedKey })),
    [
      { id: "connectivity", kind: "connectivity", priority: 3, message: "SSH 已恢复，等待稳定确认", device: "", usage: 3, sharedKey: null },
      { id: "disk:/data", kind: "disk", priority: 1, message: "msg:disk:/data", device: "/data", usage: 91.5, sharedKey: "nfs:/data" },
      { id: "cpu", kind: "cpu", priority: 2, message: "msg:cpu", device: "", usage: -1, sharedKey: null },
    ],
  );
  assert.equal(conditions[0].source.conditionKey, "connectivity");
  const offline = attention.serverConditions(server("gpu-02", "unreachable"), [
    condition("connectivity", "connectivity"),
    condition("gpu_memory:GPU-1", "gpu_memory", { resource: "GPU 1 VRAM", value: 95.3 }),
  ]);
  assert.equal(offline[0].message, "msg:connectivity");
  assert.equal(offline[0].frozen, false);
  assert.equal(offline[1].frozen, true);
  assert.equal(offline[1].message, "msg:gpu_memory:GPU-1");
  assert.equal(conditions.every((item) => item.frozen === false), true);
}

{
  // A host that is not online keeps its resource conditions frozen at the
  // last successful sample (a live deployment showed a node unreachable for
  // 533 probes with two-day-old VRAM figures); the issue says so per
  // condition, while the connectivity failure itself is current.
  const host = server("gpu-04", "unreachable");
  const conditions = attention.serverConditions(host, [
    condition("connectivity", "connectivity", { severity: "critical" }),
    condition("gpu_memory:GPU-5", "gpu_memory", { severity: "critical", value: 95.3 }),
    condition("disk:/data", "disk", { resource: "/data", value: 98 }),
    condition("disk:/", "disk", { resource: "/", value: 90 }),
  ]);
  const issue = attention.issueFromConditions(host, conditions);
  assert.deepEqual(issue.messages, [
    "msg:disk:/data（离线前） +1",
    "msg:connectivity",
    "msg:gpu_memory:GPU-5（离线前）",
  ]);
  const online = attention.issueFromConditions(
    server("gpu-05"),
    attention.serverConditions(server("gpu-05"), [
      condition("gpu_memory:GPU-5", "gpu_memory", { severity: "critical", value: 95.3 }),
    ]),
  );
  assert.deepEqual(online.messages, ["msg:gpu_memory:GPU-5"]);
}

{
  // One host's issue: the fullest disk leads with a +N suffix, categories are
  // the sorted distinct kinds, and any critical condition makes it critical.
  const host = server("gpu-03");
  const conditions = attention.serverConditions(host, [
    condition("disk:/", "disk", { resource: "/", value: 80 }),
    condition("disk:/scratch", "disk", { resource: "/scratch", value: 97, severity: "critical" }),
    condition("memory", "memory", { value: 92 }),
  ]);
  const issue = attention.issueFromConditions(host, conditions);
  assert.equal(issue.server, host);
  assert.deepEqual(issue.hosts, ["gpu-03"]);
  assert.equal(issue.severity, "critical");
  assert.equal(issue.priority, 2);
  assert.deepEqual(issue.messages, ["msg:disk:/scratch +1", "msg:memory"]);
  assert.deepEqual(issue.categories, ["compute", "storage"]);
  assert.equal(issue.sortName, "gpu-03");
  assert.equal(attention.issueFromConditions(host, []), null);
}

function fleet() {
  const servers = [
    server("a-01", "unreachable"),
    server("a-02", "unreachable"),
    server("a-03"),
    server("b-01"),
    server("b-02"),
    server("c-01"),
  ];
  const active = {
    "a-01": [condition("connectivity", "connectivity", { severity: "critical" })],
    "a-02": [
      condition("connectivity", "connectivity", { severity: "critical" }),
      condition("disk:/", "disk", { resource: "/", value: 88 }),
    ],
    "a-03": [],
    "b-01": [condition("disk:/nfs", "disk", { resource: "/nfs", value: 93, groupKey: "nfs:volume" })],
    "b-02": [
      condition("disk:/nfs", "disk", { resource: "/nfs", value: 96, severity: "critical", groupKey: "nfs:volume" }),
      condition("disk:/nfs-old", "disk", { resource: "/nfs-old", value: 50, groupKey: "nfs:volume" }),
    ],
    "c-01": [condition("cpu", "cpu", { value: 99 })],
  };
  return {
    servers,
    conditionsByHost: new Map(servers.map((item) => [item.host, attention.serverConditions(item, active[item.host])])),
  };
}

{
  // Fleet grouping: the shared path swallows both hosts' connectivity
  // conditions (a-02 keeps its disk problem as its own issue), the shared
  // storage groups two hosts by the hottest device and counts each host once,
  // c-01 stands alone, and the order is priority, severity, then name.
  const { servers, conditionsByHost } = fleet();
  const issues = attention.issues({
    servers,
    conditionsByHost,
    correlations: [
      { kind: "configured_shared_path", confidence: "possible", anchor: "gateway", hosts: ["a-01", "a-02", "a-03"] },
      { kind: "configured_shared_path", confidence: "confirmed", anchor: "other", hosts: ["b-01", "b-02"] },
      { kind: "configured_shared_path", confidence: "possible", anchor: "bad host", hosts: ["a-01", "a-02"] },
    ],
  });
  assert.deepEqual(
    issues.map((issue) => [issue.shared ? issue.sharedLabel : issue.server.host, issue.priority, issue.severity, issue.sortName]),
    [
      ["可能的共享链路", 3, "critical", "gateway"],
      ["共享存储", 2, "critical", "/nfs"],
      ["a-02", 1, "warning", "a-02"],
      ["c-01", 1, "warning", "c-01"],
    ],
  );
  assert.deepEqual(issues[0].hosts, ["a-01", "a-02"]);
  assert.deepEqual(issues[0].messages, ["2 台节点不可达 · 配置路径经过 gateway"]);
  assert.deepEqual(issues[0].categories, ["connection"]);

  // A fleet-wide simultaneous loss outranks path groups, names no anchor,
  // and consumes the connectivity conditions of every listed host; a
  // configured-path group whose hosts it already explains is not listed again.
  const fleetWide = attention.issues({
    servers,
    conditionsByHost,
    correlations: [
      { kind: "simultaneous_connectivity_loss", confidence: "possible", anchor: null, hosts: ["a-01", "a-02", "c-01"] },
      { kind: "configured_shared_path", confidence: "possible", anchor: "gateway", hosts: ["a-01", "a-02"] },
    ],
  });
  assert.deepEqual(
    fleetWide.map((issue) => [issue.shared ? issue.sharedLabel : issue.server.host, issue.priority]),
    [["疑似监控端链路故障", 4], ["共享存储", 2], ["a-02", 1], ["c-01", 1]],
  );
  assert.deepEqual(fleetWide[0].hosts, ["a-01", "a-02"]);
  assert.deepEqual(fleetWide[0].messages, ["2 台节点在同一采集周期内失联 · 先检查监控端上行或共享中继"]);
  assert.equal(fleetWide[0].sortName, "");
  assert.deepEqual(issues[1].hosts, ["b-01", "b-02"]);
  assert.deepEqual(issues[1].messages, ["/nfs 96% · 影响 2 台"]);
  assert.deepEqual(issues[2].messages, ["msg:disk:/（离线前）"]);
  assert.equal(issues[2].conditions.length, 1);
}

{
  // A shared storage group whose hottest reading comes from an offline host
  // carries the same marker; an online hottest host does not.
  const servers = [server("s-01", "unreachable"), server("s-02")];
  const shared = (usage) => condition("disk:/nfs", "disk", { resource: "/nfs", value: usage, groupKey: "nfs:volume" });
  const frozenHottest = attention.issues({
    servers,
    conditionsByHost: new Map([
      ["s-01", attention.serverConditions(servers[0], [shared(97)])],
      ["s-02", attention.serverConditions(servers[1], [shared(90)])],
    ]),
    correlations: [],
  });
  assert.deepEqual(frozenHottest[0].messages, ["/nfs 97%（离线前） · 影响 2 台"]);
  const liveHottest = attention.issues({
    servers,
    conditionsByHost: new Map([
      ["s-01", attention.serverConditions(servers[0], [shared(90)])],
      ["s-02", attention.serverConditions(servers[1], [shared(97)])],
    ]),
    correlations: [],
  });
  assert.deepEqual(liveHottest[0].messages, ["/nfs 97% · 影响 2 台"]);
}

{
  // Without a qualifying correlation the unreachable hosts are separate
  // issues ranked above the storage group; a shared key seen on one host
  // only is not a shared issue.
  const { servers, conditionsByHost } = fleet();
  conditionsByHost.set("b-01", []);
  const issues = attention.issues({ servers, conditionsByHost, correlations: [] });
  assert.deepEqual(
    issues.map((issue) => (issue.shared ? issue.sharedLabel : issue.server.host)),
    ["a-01", "a-02", "b-02", "c-01"],
  );
  assert.deepEqual(issues[2].messages, ["msg:disk:/nfs +1"]);
  assert.equal(issues[1].messages.length, 2);
  assert.deepEqual(issues[1].categories, ["connection", "storage"]);
}

{
  // Hosts missing from the condition map contribute nothing, and a shared
  // path needs at least two hosts that are actually unreachable.
  const { servers } = fleet();
  const conditionsByHost = new Map([
    ["a-01", attention.serverConditions(servers[0], [condition("connectivity", "connectivity")])],
  ]);
  const issues = attention.issues({
    servers,
    conditionsByHost,
    correlations: [
      { kind: "configured_shared_path", confidence: "possible", anchor: "gateway", hosts: ["a-01", "a-02"] },
    ],
  });
  assert.deepEqual(issues.map((issue) => issue.server.host), ["a-01"]);
}

console.log("attention contract ok");
