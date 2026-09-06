import assert from "node:assert/strict";

await import("../src/mocop/static/format.js");
await import("../src/mocop/static/owner-usage.js");

const { format, numeric, memory } = globalThis.MocopFormat.create();
const NOW = Date.parse("2026-09-06T08:00:00Z");
const ownerUsage = globalThis.MocopOwnerUsage.create({
  format,
  numeric,
  memory,
  age: (timestamp) => `age(${Math.round((NOW - Date.parse(timestamp)) / 60_000)}m)`,
  workloadLabels: { process: "进程", slurm: "Slurm", kubernetes: "Kubernetes" },
});

const process = (pid, memoryMiB, workload = null) => ({
  pid, name: `/opt/bin/python${pid}`, used_memory_mib: memoryMiB, workload,
});
const gpu = (index, processes, observedAt = "2026-09-06T07:59:00Z") => ({
  index, uuid: `GPU-${index}`, processes, processes_observed_at: observedAt,
});
const researcher = { kind: "slurm", owner: "researcher" };
const snapshot = {
  servers: [
    {
      host: "a-01", status: "online", displayName: "Alpha 1",
      gpus: [
        // One PID on two GPUs of the same host: two GPU records, one process.
        gpu(0, [process(100, 40_960, researcher), process(900, 512)]),
        gpu(1, [process(100, 20_480, researcher)], "2026-09-06T07:30:00Z"),
        gpu(2, [], "2026-09-06T01:00:00Z"), // idle: its timestamp does not count
      ],
    },
    {
      host: "a-02", status: "online",
      gpus: [gpu(0, [process(200, null, { kind: "kubernetes", owner: "intern" }), process(201, 1_024)])],
    },
    {
      // Not online: its last-success processes are excluded and counted once.
      host: "a-03", status: "unreachable",
      gpus: [gpu(0, [process(300, 70_000, researcher)]), gpu(1, [process(301, 70_000, researcher)])],
    },
    { host: "a-04", status: "error", gpus: [] },
  ],
};

{
  const { ranked, excludedOfflineHosts, oldestObservedAt, hostLabels } =
    ownerUsage.currentOwners(snapshot);
  assert.equal(excludedOfflineHosts, 1, "only the offline host that still lists processes counts");
  assert.equal(oldestObservedAt, "2026-09-06T07:30:00Z", "oldest sample among GPUs that have processes");
  assert.equal(hostLabels.get("a-01"), "Alpha 1");
  assert.equal(hostLabels.get("a-02"), "a-02");
  assert.deepEqual(
    ranked.map((entry) => [entry.label, entry.attributed, entry.vramMiB, entry.processKeys.size, entry.gpus.size, entry.hosts.size, [...entry.kinds], entry.unknownVramKeys.size]),
    [
      ["researcher", true, 61_440, 1, 2, 1, ["slurm"], 0],
      ["未归属", false, 1_536, 2, 2, 2, [], 0],
      ["intern", true, 0, 1, 1, 1, ["kubernetes"], 1],
    ],
  );
  assert.equal(
    ownerUsage.currentSummary({ ranked, visibleCount: 50, oldestObservedAt }),
    "3 个归属方 · 共占用至少 61.5 GiB · 含未归属进程 · 部分进程显存未知 · 数据截至 age(30m)",
  );
  assert.equal(
    ownerUsage.currentSummary({ ranked: ranked.slice(0, 1), visibleCount: 50, oldestObservedAt: "" }),
    "1 个归属方 · 共占用 60 GiB",
  );
  assert.equal(
    ownerUsage.currentSummary({ ranked, visibleCount: 2, oldestObservedAt: "" }),
    "3 个归属方 · 共占用至少 61.5 GiB · 含未归属进程 · 部分进程显存未知 · 仅展示前 2 项",
  );
}

{
  // Ranking ties on VRAM break by GPU count, then by label.
  const tie = ownerUsage.currentOwners({
    servers: [{
      host: "t-01", status: "online",
      gpus: [
        gpu(0, [process(1, 1_024, { owner: "zed" }), process(2, 1_024, { owner: "amy" })]),
        gpu(1, [process(3, 0, { owner: "amy" })]),
      ],
    }],
  }).ranked;
  assert.deepEqual(tie.map((entry) => entry.label), ["amy", "zed"]);
  assert.deepEqual(ownerUsage.currentOwners({ servers: [] }).ranked, []);
}

{
  // GPU-hours wording switches unit at one minute and at 1.5 hours.
  assert.equal(ownerUsage.gpuHoursLabel(59), "59 卡·秒");
  assert.equal(ownerUsage.gpuHoursLabel(60), "1 卡·分");
  assert.equal(ownerUsage.gpuHoursLabel(5_399), "90 卡·分");
  assert.equal(ownerUsage.gpuHoursLabel(5_400), "1.5 卡·时");
  assert.equal(ownerUsage.gpuHoursLabel(144_015.5), "40 卡·时");
  assert.equal(ownerUsage.gpuHoursLabel("bad"), "0 卡·秒");
}

{
  // The bill headline: retention that starts more than a minute inside the
  // window is disclosed, as is every device whose full timeline begins
  // inside it; a report covering the whole window has neither caveat.
  const base = {
    totalOwners: 2, totalGpuSeconds: 144_015.5,
    sinceAt: "2026-09-05T08:00:00Z", earliestDataAt: "2026-09-05T08:00:30Z", partialGpus: 0,
  };
  assert.equal(ownerUsage.usageSummary(base), "2 个归属方 · 共 40 卡·时");
  assert.equal(
    ownerUsage.usageSummary({ ...base, earliestDataAt: "2026-09-06T02:00:00Z", partialGpus: 3 }),
    "2 个归属方 · 共 40 卡·时 · 数据自 age(360m)起 · 3 张卡的时间线不完整",
  );
  assert.equal(
    ownerUsage.usageSummary({ totalOwners: 1, totalGpuSeconds: 30, sinceAt: "2026-09-05T08:00:00Z" }),
    "1 个归属方 · 共 30 卡·秒",
    "a report without earliestDataAt or partialGpus carries no caveat",
  );
  // The history report names its source and states coverage through
  // coveredFromAt (bounded by retention), not the earliest record.
  assert.equal(
    ownerUsage.usageSummary({
      ...base, source: "history", resolution: "hour",
      coveredFromAt: "2026-09-05T08:00:00Z", earliestDataAt: "2026-09-06T02:00:00Z",
    }),
    "2 个归属方 · 共 40 卡·时 · 历史库统计",
  );
  assert.equal(
    ownerUsage.usageSummary({ ...base, source: "history", coveredFromAt: "2026-09-06T02:00:00Z" }),
    "2 个归属方 · 共 40 卡·时 · 历史库统计 · 数据自 age(360m)起",
  );
}

{
  // Per-day chips come from the history report only, oldest first, with the
  // month-day and the GPU-hours label; malformed entries are skipped.
  assert.deepEqual(ownerUsage.usageDays({ owners: [] }), []);
  assert.deepEqual(
    ownerUsage.usageDays({
      days: [
        { day: "2026-09-05", gpuSeconds: 7200 },
        { day: "2026-09-06", gpuSeconds: 90 },
        { day: 7, gpuSeconds: 1 },
        { day: "2026-09-07" },
      ],
    }),
    [{ day: "09-05", label: "2 卡·时" }, { day: "09-06", label: "2 卡·分" }],
  );
}

{
  assert.deepEqual(ownerUsage.usageKinds({ kinds: { slurm: 4, process: 1, ray: 2 } }), ["Slurm", "ray"]);
  assert.deepEqual(ownerUsage.usageKinds({ kinds: null }), []);
  assert.deepEqual(ownerUsage.usageKinds({}), []);
  assert.equal(ownerUsage.idleShare({ idleShare: 0.25 }), 0.25);
  assert.equal(ownerUsage.idleShare({ idleShare: "0.5" }), 0.5);
  assert.ok(Number.isNaN(ownerUsage.idleShare({ idleShare: null })));
  assert.ok(Number.isNaN(ownerUsage.idleShare({})));
}

console.log("owner-usage contract ok");
