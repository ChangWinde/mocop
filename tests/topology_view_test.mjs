import assert from "node:assert/strict";
import { create } from "./fake_dom.mjs";

await import("../src/mocop/static/topology-view.js");

const elements = Object.fromEntries(
  ["tree", "unmapped", "unmappedList", "status", "nodeCount", "frpCount", "liveSummary"].map((name) => [name, create("div")]),
);
const visited = [];
const view = globalThis.MocopTopologyView.create({
  create,
  elements,
  displayHost: (value) => (typeof value === "string" ? value : value.displayName || value.host),
  numeric: (value, fallback = 0) => (Number.isFinite(value) ? value : fallback),
  format: (value) => String(Math.round(value)),
  onHost: (host) => visited.push(host),
});

view.render({ topology: null, revision: 0, loading: true, error: "", servers: [] });
assert.equal(elements.status.textContent, "正在读取本地拓扑配置…");
view.render({ topology: null, revision: 1, loading: false, error: "拓扑读取失败", servers: [] });
assert.equal(elements.status.className, "topology-status error");
view.render({ topology: { root: null, links: [] }, revision: 2, loading: false, error: "", servers: [] });
assert.equal(elements.liveSummary.textContent, "无拓扑配置");

const topology = {
  root: "monitor",
  links: [
    { source: "monitor", target: "jump", transport: "ssh" },
    { source: "jump", target: "atlas-01", transport: "frp-stcp", label: "edge" },
    { source: "jump", target: "atlas-02", transport: "frp-xtcp" },
  ],
};
const servers = [
  { host: "atlas-01", displayName: "Atlas One", status: "online", stale: false, gpus: [{}, {}], system: { cpu_usage_pct: 12.4 } },
  { host: "atlas-02", status: "unreachable", stale: true, gpus: [] },
  { host: "orphan", status: "online", stale: false, gpus: [], system: null },
];
view.render({ topology, revision: 3, loading: false, error: "", servers });
assert.equal(elements.status.hidden, true);
assert.equal(elements.nodeCount.textContent, "4");
assert.equal(elements.frpCount.textContent, "2");
assert.equal(elements.liveSummary.textContent, "1 / 2 个监控节点在线", "the unmapped host is not part of the path summary");
const nodes = elements.tree.findAll("topology-node");
assert.deepEqual(nodes.map((node) => node.dataset.host), ["monitor", "jump", "atlas-01", "atlas-02"]);
assert.equal(nodes[0].disabled, true, "infrastructure aliases are not collected");
assert.equal(nodes[2].className, "topology-node online");
assert.equal(nodes[2].find("topology-node-meta").textContent, "2 GPU · CPU 12%");
assert.equal(nodes[2].find("topology-node-heading").children[1].textContent, "Atlas One");
assert.equal(nodes[3].find("topology-node-meta").textContent, "数据陈旧");
assert.equal(elements.tree.findAll("topology-link-label").map((label) => label.textContent).join(","), "SSH,edge,FRP · XTCP");
nodes[2].click();
assert.deepEqual(visited, ["atlas-01"]);
assert.equal(elements.unmapped.hidden, false);
elements.unmappedList.children[0].click();
assert.deepEqual(visited, ["atlas-01", "orphan"]);

// The same revision reuses the tree and only refreshes live state.
const tree = elements.tree.children[0];
view.render({ topology, revision: 3, loading: false, error: "", servers: [{ ...servers[0], status: "unreachable" }, servers[1]] });
assert.equal(elements.tree.children[0], tree, "the tree is not rebuilt for telemetry updates");
assert.equal(nodes[2].className, "topology-node offline");
assert.equal(elements.unmapped.hidden, true);
view.render({ topology: { ...topology, links: topology.links.slice(0, 1) }, revision: 4, loading: false, error: "", servers });
assert.notEqual(elements.tree.children[0], tree, "a new revision rebuilds the tree");

console.log("topology-view tests passed");
