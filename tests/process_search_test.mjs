import assert from "node:assert/strict";

await import("../mocop/static/process-search.js");

const createSearch = (overrides = {}) => globalThis.MocopProcessSearch.create({
  maxResults: 2,
  maxQueryLength: 12,
  workloadLabels: { slurm: "Slurm" },
  processName: (process) => String(process.name || "unknown").split("/").at(-1),
  numeric: (value, fallback = 0) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  },
  ...overrides,
});

const search = createSearch();
assert.deepEqual(search.normalizedSearchTerms("  ＴＲＡＩＮ．ＰＹ  "), ["train.py"]);
assert.equal(search.normalizedSearchTerms("abcdefghijklmnop").join(""), "abcdefghijkl");

const snapshot = {
  servers: [
    {
      host: "gpu-b",
      stale: false,
      gpus: [{
        index: 1,
        uuid: "GPU-B",
        name: "NVIDIA H100",
        processes_available: true,
        processes: [
          { pid: 30, name: "/work/train.py", used_memory_mib: 30, workload: null },
          { pid: 20, name: "/work/train.py", used_memory_mib: 20, workload: null },
          { pid: 10, name: "/work/train.py", used_memory_mib: 10, workload: null },
        ],
      }],
    },
    {
      host: "gpu-a",
      stale: true,
      gpus: [{
        index: 0,
        uuid: "GPU-A",
        name: "NVIDIA A100",
        processes_available: false,
        processes: [],
      }],
    },
  ],
};

const result = search.searchProcessRecords(snapshot, "train.py");
assert.equal(result.total, 3);
assert.equal(result.matches.length, 2);
assert.deepEqual(result.matches.map(({ process }) => process.pid), [30, 20]);
assert.equal(result.unavailableGpuCount, 1);
assert.equal(result.staleCount, 0);
assert.equal(search.searchProcessRecords(snapshot, "train.py", "gpu-a").total, 0);

const terms = search.normalizedSearchTerms("gpu-b h100");
assert.equal(
  search.gpuRecordMatchesSearch(snapshot.servers[0], snapshot.servers[0].gpus[0], terms),
  true,
);
assert.deepEqual(search.searchProcessRecords(null, "train"), {
  matches: [], total: 0, unavailableGpuCount: 0, staleCount: 0,
});

assert.throws(() => createSearch({ maxResults: 0 }), /positive integer/);
assert.throws(() => createSearch({ processName: null }), /must be functions/);

console.log("process search contract passed");
