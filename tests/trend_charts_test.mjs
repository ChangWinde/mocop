import assert from "node:assert/strict";
import { create, svg } from "./fake_dom.mjs";

await import("../src/mocop/static/trend-charts.js");

const charts = globalThis.MocopTrendCharts.create({ create, svg, format: (value) => String(value) });
const at = (minute) => new Date(Date.UTC(2026, 10, 22, 0, minute)).toISOString();

assert.equal(charts.historyDuration([]), "0 个样本");
assert.equal(charts.historyDuration([{ observedAt: at(0) }, { observedAt: at(10) }]), "2 个样本 · 最近 10 分钟");

// Uneven sampling keeps real spacing; a pause longer than three cadences is a gap.
const points = [0, 1, 2, 10, 11].map((minute) => ({ observedAt: at(minute), value: minute }));
const positions = charts.chartPositions(points);
assert.equal(positions.xs[0], 0);
assert.equal(positions.xs.at(-1), 220);
assert.deepEqual(positions.gapBefore, [false, false, false, true, false]);
assert.deepEqual(charts.segments([1, 2, null, 4, NaN, 6, 7]), [[0, 1], [3], [5, 6]]);

const line = charts.sparkline(points, (point) => point.value, "var(--blue)", 20);
const drawn = line.children.filter((child) => child.tagName === "POLYLINE");
assert.equal(drawn.length, 2, "the pause splits the path");
assert.equal(drawn[0].style.getPropertyValue("stroke"), "var(--blue)", "colors come from tokens, not literals");

const card = charts.trendCard("CPU", [{ observedAt: at(0), value: 10 }, { observedAt: at(1), value: null }], (point) => point.value ?? NaN, (value) => `${value}%`, "var(--blue)", 100);
assert.equal(card.find("trend-card-top").textContent, "CPU—", "a missing latest value is a gap, not the previous value");
assert.equal(card.find("trend-card-foot").textContent, "峰值 10%");

const retry = charts.transportRetryCard([{ observedAt: at(0), transportRetried: true }, { observedAt: at(1) }]);
assert.equal(retry.find("trend-card-top").textContent, "链路重试1 次");

// Hourly report rows fill a dense window; omitted hours stay null.
const hours = [
  { hour: "2026-11-22T00:00:00Z", gpus: 4, busyShare: 0.25, memoryUsedAvgMiB: 2048, memoryTotalMiB: 32768 },
  { hour: "2026-11-22T02:00:00Z", gpus: 4, busyShare: 0.5, memoryUsedAvgMiB: null, memoryTotalMiB: 32768 },
];
const busy = charts.hourlyValues(hours, "2026-11-22T00:20:00Z", "2026-11-22T03:05:00Z", charts.busySharePct);
assert.equal(busy.start, Date.parse("2026-11-22T00:00:00Z"));
assert.deepEqual(busy.values, [25, null, 50, null]);
assert.deepEqual(charts.hourlyValues(hours, "bad", "2026-11-22T03:00:00Z", charts.busySharePct).values, []);
// 2048 MiB per device on average across 4 devices of 8192 MiB is 25 %, not 6.25 %.
assert.equal(charts.memorySharePct(hours[0]), 25);
assert.equal(charts.memorySharePct(hours[1]), null);
assert.equal(charts.mean([10, null, 30]), 20);
assert.equal(charts.mean([null]), null);
assert.equal(charts.hourlyPoints(busy)[2].observedAt, "2026-11-22T02:00:00.000Z");

const area = charts.areaChart([10, 20, null, 40]);
assert.equal(area.children.filter((child) => child.getAttribute("class") === "chart-grid").length, 2);
assert.equal(area.children.filter((child) => child.getAttribute("class") === "utilization-area").length, 1, "a single-point run draws no area");
assert.equal(area.children.filter((child) => child.getAttribute("class") === "utilization-line").length, 2);

console.log("trend-charts tests passed");
