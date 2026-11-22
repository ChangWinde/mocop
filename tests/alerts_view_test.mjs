import assert from "node:assert/strict";
import { create } from "./fake_dom.mjs";

await import("../src/mocop/static/alerts-view.js");

const { tabOf } = globalThis.MocopAlertsView;
const view = globalThis.MocopAlertsView.create({
  create,
  format: (value, digits = 0) => Number(value).toFixed(digits),
  age: (timestamp) => `age(${timestamp})`,
});

assert.equal(tabOf({}), "open");
assert.equal(tabOf({ action: "acknowledged" }), "acknowledged");
assert.equal(tabOf({ action: "silenced" }), "silenced");
assert.equal(tabOf({ action: "acknowledged", maintenanceSilenced: true }), "silenced", "maintenance mutes an acknowledged condition too");

const conditions = [
  { host: "b", severity: "warning", firstObservedAt: "2026-11-22T01:00:00Z", category: "disk" },
  { host: "a", severity: "critical", firstObservedAt: "2026-11-22T02:00:00Z", category: "connectivity" },
  { host: "c", severity: "critical", firstObservedAt: "2026-11-22T00:00:00Z", category: "connectivity", action: "acknowledged" },
];
assert.deepEqual({ ...view.tabCounts(conditions) }, { open: 2, acknowledged: 1, silenced: 0 });

const opened = [];
const rows = view.alertRows(conditions, {
  tab: "open",
  hostLabel: (host) => host.toUpperCase(),
  describe: (condition) => ({ title: `title-${condition.category}`, detail: `detail-${condition.host}` }),
  onOpen: (condition) => opened.push(condition.host),
});
assert.deepEqual(rows.map((row) => row.dataset.host), ["a", "b"], "critical first, then oldest");
assert.equal(rows[0].className, "alert-row critical");
assert.equal(rows[0].find("alert-row-title").textContent, "Atitle-connectivity");
assert.equal(rows[0].find("alert-row-meta").textContent, "detail-a");
rows[1].click();
assert.deepEqual(opened, ["b"]);
assert.equal(view.alertRows(conditions, { tab: "silenced", hostLabel: String, describe: () => ({}), onOpen() {} })[0].className, "alert-list-empty");

const governed = [];
const noise = view.noise({
  hours: 15.5, opened: 246, perHour: 15.87, state: "critical", target: 6,
  recurring: [{ host: "vega-02", category: "connectivity", resource: "SSH", openings: 237, share: 0.963 }, { host: "vega-07", category: "cpu", resource: null, openings: 6, share: 0.02 }],
}, { hostLabel: (host) => host, onRecurring: (item) => governed.push(item.host) });
assert.equal(noise[0].children[0].className, "critical");
assert.equal(noise[0].children[0].textContent, "15.9");
assert.equal(noise[0].children[2].textContent, "16 小时内开启 246 次 · 目标 ≤ 6", "windows of ten hours or more round to whole hours");
assert.equal(noise[1].textContent.startsWith("vega-02 · 连接 SSH开关 237 次 · 占 96%"), true, "the top item shows its share");
assert.equal(noise[2].textContent.includes("占"), false, "only the top item shows a share");
noise[1].children[1].click();
assert.deepEqual(governed, ["vega-02"]);
assert.equal(view.noise(null, { hostLabel: String, onRecurring() {} })[0].textContent, "简报暂不可用");

const maintained = view.maintenance(
  [{ host: "a" }, { host: "b", maintenance: { until: null, reason: "换卡" } }],
  { hostLabel: (host) => host, onHost() {} },
);
assert.equal(maintained.length, 1);
assert.equal(maintained[0].children[0].textContent, "b维护中 · 换卡");
assert.equal(view.maintenance([], { hostLabel: String, onHost() {} })[0].textContent, "无");

console.log("alerts-view tests passed");
