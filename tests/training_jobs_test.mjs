import assert from "node:assert/strict";
import "../src/mocop/static/training_summary.js";
import "../src/mocop/static/training_jobs.js";

const { latest, valueText, timeText, errorText, create } = globalThis.MocopTrainingJobs;
const points = [{ ts: 20, value: null }, { ts: 10, value: 0 }, { ts: null, value: 99 }];
assert.deepEqual(latest(points), { ts: 20, value: null });
assert.deepEqual(points.map(p => p.ts), [20, 10, null]);
assert.equal(latest(undefined), undefined);
assert.equal(valueText(0), "0");
for (const value of [null, undefined, NaN, Infinity, "0", false]) assert.equal(valueText(value), "未知");
assert.equal(timeText(null), "未知");
assert.equal(errorText(429), "读取频率受限，请稍后重试");
assert.equal(errorText(403), "平台读取权限不足");
assert.equal(errorText(501), "当前厂商不提供此数据");
assert.equal(errorText(503), "暂时无法读取");

const listeners = new Map();
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    hidden: true, open: false, value: "active", dataset: {},
    addEventListener(name, callback) { listeners.set(selector + name, callback); },
    classList: { add() {}, toggle() {} },
    replaceChildren() {},
  });
  return elements.get(selector);
}
globalThis.document = {
  hidden: false,
  querySelector: element,
  addEventListener(name, callback) { listeners.set(name, callback); },
};
globalThis.window = {
  addEventListener(name, callback) { listeners.set(name, callback); },
};
const originalInterval = globalThis.setInterval;
const originalClear = globalThis.clearInterval;
let scheduled = 0;
globalThis.setInterval = () => ++scheduled;
globalThis.clearInterval = () => {};
try {
  let requests = 0;
  const monitor = create({ request: async path => {
    assert.equal(path, "/api/training-jobs");
    requests += 1;
    return { ok: true, json: async () => ({ enabled: false }) };
  } });
  await monitor.refresh();
  assert.equal(requests, 0, "no requests before dashboard authentication");
  monitor.start();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests, 1);
  assert.equal(element("#training-jobs").hidden, true);
  monitor.start();
  assert.equal(scheduled, 1, "one polling timer");
  document.hidden = true;
  await monitor.refresh();
  assert.equal(requests, 1, "hidden tabs do not poll");
  listeners.get("pagehide")();
  document.hidden = false;
  listeners.get("pageshow")({ persisted: true });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(scheduled, 2, "back-forward cache restores polling");
  assert.equal(requests, 2);
} finally {
  globalThis.setInterval = originalInterval;
  globalThis.clearInterval = originalClear;
}
console.log("training jobs contracts passed");
