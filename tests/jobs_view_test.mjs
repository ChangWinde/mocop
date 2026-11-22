import assert from "node:assert/strict";
import { create } from "./fake_dom.mjs";

await import("../src/mocop/static/jobs-view.js");

const view = globalThis.MocopJobsView.create({
  create,
  format: (value) => String(Math.round(value)),
  memory: (mib) => `${mib / 1024} GiB`,
  durationSince: (timestamp) => `since ${timestamp}`,
});
const job = {
  key: "atlas-01\u0000alice\u0000train.py\u0000t1", host: "atlas-01", owner: "alice", program: "train.py",
  command: "python train.py --stage sft", gpus: [0, 1], utilization: 80, memoryMiB: 40_960, memoryKnown: true,
  startedAt: "2026-11-22T00:00:00Z", state: "compute",
};
const opened = [];
const row = view.row(job, { me: "alice", hostLabel: (host) => `[${host}]`, onOpen: (item) => opened.push(item.key) });
const cells = row.children;
assert.equal(cells.length, 7);
assert.equal(cells[0].find("job-owner").className, "job-owner mine", "the viewer's own jobs are marked");
assert.equal(cells[1].textContent, "train.pypython train.py --stage sft");
assert.equal(cells[2].textContent, "[atlas-01] · 0, 1");
assert.equal(cells[3].textContent, "80%");
assert.equal(cells[4].textContent, "40 GiB");
assert.equal(cells[5].textContent, "since 2026-11-22T00:00:00Z");
assert.equal(cells[6].find("job-state").dataset.state, "compute");
assert.equal(cells[6].textContent, "运行中");
row.click();
assert.deepEqual(opened, [job.key]);

const unowned = view.row({ ...job, owner: null, utilization: null, memoryKnown: false, startedAt: null, state: "light" }, {
  me: "", hostLabel: String, onOpen() {},
});
assert.equal(unowned.children[0].find("job-owner").className, "job-owner unowned");
assert.equal(unowned.children[0].textContent, "未未归属");
assert.equal(unowned.children[3].textContent, "—");
assert.equal(unowned.children[4].textContent, "≥ 40 GiB", "an unknown process memory makes the total a lower bound");
assert.equal(unowned.children[5].textContent, "—");
assert.equal(unowned.children[6].textContent, "轻量");
const unknownMemory = view.row({ ...job, memoryMiB: 0, memoryKnown: false }, { me: "", hostLabel: String, onOpen() {} });
assert.equal(unknownMemory.children[4].textContent, "未知", "no reported memory is unknown, not zero");
assert.deepEqual(Object.keys(view.STATE_LABELS), ["compute", "idle", "light", "offline"]);

console.log("jobs-view tests passed");
