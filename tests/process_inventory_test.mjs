import assert from "node:assert/strict";

await import("../src/mocop/static/process-inventory.js");

const inventory = globalThis.MocopProcessInventory.create({
  maxResults: 2,
  numeric: (value, fallback = 0) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  },
});

const snapshot = {
  servers: [
    {
      host: "gpu-b",
      stale: true,
      gpus: [{
        index: 1,
        processes_available: true,
        processes: [
          { pid: 30, used_memory_mib: 30 },
          { pid: 20, used_memory_mib: 20 },
          { pid: 10, used_memory_mib: 10 },
        ],
      }],
    },
    {
      host: "gpu-a",
      stale: false,
      gpus: [{
        index: 0,
        processes_available: false,
        processes: [{ pid: 99, used_memory_mib: 99 }],
      }],
    },
  ],
};

const listed = inventory.listProcessRecords(snapshot, "gpu-b");
assert.equal(listed.total, 3);
assert.deepEqual(listed.matches.map(({ process }) => process.pid), [30, 20]);
assert.equal(listed.staleCount, 3);
assert.deepEqual(inventory.listProcessRecords(snapshot, "gpu-a"), {
  matches: [], total: 0, unavailableGpuCount: 1, staleCount: 0,
});
assert.deepEqual(inventory.panelCopy("gpu-a", true, {
  matches: [], total: 0, unavailableGpuCount: 1, staleCount: 0,
}), {
  summary: "gpu-a没有可见的活跃 GPU 进程 · 1 张 GPU 的任务数据不可用",
  empty: "当前 GPU 的任务数据不可用，恢复采样后会显示进程",
});
assert.deepEqual(inventory.panelCopy("全局", false, {
  matches: [], total: 0, unavailableGpuCount: 0, staleCount: 0,
}), {
  summary: "全局没有匹配的活跃程序",
  empty: "换一个名称、PID、用户或 workload 关键词重试",
});
assert.equal(inventory.panelCopy("pai-2", true, {
  matches: [{}, {}], total: 3, unavailableGpuCount: 0, staleCount: 0,
}).summary, "共 3 条活跃进程，显示显存占用最高的 2 条");
assert.equal(inventory.panelCopy("全局", false, {
  matches: [{}], total: 2, unavailableGpuCount: 0, staleCount: 0,
}).summary, "找到 2 条匹配记录，显示相关度最高的 1 条");
assert.equal(inventory.sampleLabel({ stale: false }, { processes_sampled: false }), "复用样本");
assert.equal(inventory.detailSampleLabel({ stale: false }, {
  processes_available: true, processes_sampled: false,
}), "GPU 当前 · 进程复用");
assert.throws(
  () => globalThis.MocopProcessInventory.create({ maxResults: 0, numeric: Number }),
  /positive integer/,
);

console.log("process inventory contract passed");
