import assert from "node:assert/strict";

await import("../src/mocop/static/command-palette.js");

const palette = globalThis.MocopCommandPalette.create();

assert.deepEqual({ ...palette.parseDemand("找 2 张 80G") }, { gpuCount: 2, minVramGiB: 80 });
assert.deepEqual({ ...palette.parseDemand("2x24") }, { gpuCount: 2, minVramGiB: 24 });
assert.deepEqual({ ...palette.parseDemand("4 × ≥ 40 GiB") }, { gpuCount: 4, minVramGiB: 40 });
assert.deepEqual({ ...palette.parseDemand("找4张") }, { gpuCount: 4, minVramGiB: 0 });
assert.deepEqual({ ...palette.parseDemand("找 ２ 张") }, { gpuCount: 2, minVramGiB: 0 }, "full-width digits fold");
assert.equal(palette.parseDemand("atlas-01"), null);
assert.equal(palette.parseDemand("找 0 张"), null, "zero GPUs is not a demand");
assert.equal(palette.parseDemand("2x999"), null, "beyond the capacity API bound");

const list = palette.entries({
  pages: [{ id: "overview", label: "总览" }, { id: "alerts", label: "告警", keywords: "incident alert" }],
  hosts: [{ host: "atlas-01" }, { host: "atlas-02" }, { host: "vega-01", detail: "A 组" }],
  owners: ["alice", "bob"],
  programs: ["train.py"],
});

assert.deepEqual(
  palette.search(list, "").slice(0, 2).map((entry) => entry.id),
  ["alerts", "overview"],
  "an empty query starts with the pages",
);
assert.deepEqual(palette.search(list, "atlas").map((entry) => entry.id), ["atlas-01", "atlas-02"]);
assert.deepEqual(palette.search(list, "ATLAS-02").map((entry) => entry.id), ["atlas-02"], "case folds");
assert.deepEqual(palette.search(list, "01").map((entry) => entry.id), ["atlas-01", "vega-01"], "word starts match");
assert.deepEqual(palette.search(list, "incident").map((entry) => entry.id), ["alerts"], "keywords match");
assert.deepEqual(palette.search(list, "a 组").map((entry) => entry.id), ["vega-01"], "details match");
assert.deepEqual(palette.search(list, "zzz"), []);

const demand = palette.search(list, "找 2 张 80G");
assert.equal(demand[0].kind, "demand", "a demand is offered first");
assert.equal(demand[0].label, "找 2 张 · ≥ 80 GiB");
assert.equal(palette.search(list, "", 3).length, 3, "the limit bounds the list");

console.log("command-palette tests passed");
