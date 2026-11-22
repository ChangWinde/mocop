import assert from "node:assert/strict";
import { create, svg } from "./fake_dom.mjs";

await import("../src/mocop/static/format.js");
await import("../src/mocop/static/owner-usage.js");
await import("../src/mocop/static/trend-charts.js");
await import("../src/mocop/static/usage-view.js");

const { format, numeric, memory } = globalThis.MocopFormat.create();
const ownerUsage = globalThis.MocopOwnerUsage.create({
  format, numeric, memory, age: () => "刚刚", workloadLabels: { process: "进程", slurm: "Slurm" },
});
const charts = globalThis.MocopTrendCharts.create({ create, svg, format });
const usageView = globalThis.MocopUsageView.create({ create, format, memory, numeric, ownerUsage, charts });

// Current owners: a card per owner, host chips that drill into the host.
const opened = [];
const ranked = [{
  label: "alice", attributed: true, vramMiB: 40_960, unknownVramKeys: new Set(["x"]),
  gpus: new Set(["a:0", "a:1"]), hosts: new Set(Array.from({ length: 10 }, (_, index) => `node-${index}`)),
  processKeys: new Set(["1"]), kinds: new Set(["Slurm"]),
}];
const [card] = usageView.ownerCards(ranked, { hostLabels: new Map([["node-0", "Node Zero"]]), onHost: (host) => opened.push(host) });
assert.equal(card.find("capacity-candidate-identity").textContent, "aliceSlurm");
assert.match(card.find("capacity-candidate-heading").textContent, /^aliceSlurm至少 /, "unknown VRAM is a lower bound");
const chips = card.findAll("owner-host-chip");
assert.equal(chips.length, 8, "at most eight host chips");
assert.equal(chips[0].textContent, "Node Zero", "display names are presentation only");
chips[0].click();
assert.deepEqual(opened, ["node-0"]);
assert.equal(card.find("capacity-devices").children.at(-1).textContent, "+2");
assert.equal(usageView.offlineNote(0), null);
assert.equal(usageView.offlineNote(2).textContent, "2 台离线节点未计入");

// The bill keeps the unattributed owner explicit and flags idle-heavy owners.
const bill = usageView.billCards({
  owners: [
    { owner: "bob", gpuSeconds: 7200, gpus: 2, hosts: ["a"], processes: 3, idleSeconds: 5400, kinds: ["slurm"] },
    { owner: null, gpuSeconds: 3600, gpus: 1, hosts: null, processes: 1 },
  ],
});
assert.equal(bill.length, 2);
assert.equal(bill[0].find("capacity-candidate-identity").children[0].textContent, "bob");
assert.equal(bill[1].find("capacity-candidate-identity").children[0].textContent, "未归属");
assert.match(bill[1].find("capacity-candidate-metrics").textContent, /节点未知/);

// Utilization: fleet chart plus hosts ranked by busy GPU-hours.
const report = {
  generatedAt: "2026-11-22T03:10:00Z",
  sinceAt: "2026-11-22T00:00:00Z",
  windowHours: 4,
  fleet: [
    { hour: "2026-11-22T00:00:00Z", busyShare: 0.5, gpus: 6 },
    { hour: "2026-11-22T02:00:00Z", busyShare: 0.25, gpus: 6 },
  ],
  hosts: [
    { host: "small", hours: [{ hour: "2026-11-22T00:00:00Z", busyShare: 1, gpus: 2 }] },
    { host: "big", hours: [{ hour: "2026-11-22T00:00:00Z", busyShare: 0.5, gpus: 4 }, { hour: "2026-11-22T02:00:00Z", busyShare: 0.5, gpus: 4 }] },
  ],
};
const visited = [];
const result = usageView.utilization(report, { hostLabel: (host) => host.toUpperCase(), onHost: (host) => visited.push(host) });
assert.equal(result.range, "平均 37.5% · 2 / 4 小时有样本", "missing hours are reported, not averaged in as zero");
const table = result.nodes[2];
const rows = table.children[1].children;
assert.deepEqual(rows.map((row) => row.children[0].textContent), ["BIG", "SMALL"]);
assert.equal(rows[0].children[3].textContent, "4", "0.5 × 4 GPUs × 2 sampled hours");
rows[1].click();
assert.deepEqual(visited, ["small"]);

const empty = usageView.utilization({ ...report, fleet: [], hosts: [] }, { hostLabel: String, onHost() {} });
assert.equal(empty.nodes[0].className, "utilization-empty");

console.log("usage-view tests passed");
