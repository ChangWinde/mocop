import assert from "node:assert/strict";

await import("../src/mocop/static/gpu-jobs.js");

const jobs = globalThis.MocopGpuJobs.create({
  processName: (process) => process.name,
  holderMinMiB: 256,
});
const busyPct = 10;
const proc = (pid, name, owner, startedAt, usedMiB, extra = {}) => ({
  pid, name, used_memory_mib: usedMiB, workload: { owner, started_at: startedAt, command: `${name} --run`, ...extra },
});
const servers = [
  {
    host: "atlas-01",
    status: "online",
    gpus: [
      { index: 0, utilization_gpu_pct: 90, processes: [proc(11, "train.py", "alice", "t1", 20_000)] },
      { index: 1, utilization_gpu_pct: 70, processes: [proc(12, "train.py", "alice", "t1", 20_000)] },
      { index: 2, utilization_gpu_pct: 0, processes: [proc(13, "notebook", "bob", "t2", 4_000)] },
      { index: 3, utilization_gpu_pct: 0, processes: [proc(14, "rustdesk", "carol", "t3", 3)] },
    ],
  },
  {
    host: "atlas-02",
    status: "online",
    gpus: [
      { index: 0, utilization_gpu_pct: 50, processes: [proc(21, "worker", "dave", "t4", 100, { workload_id: "slurm-7" })] },
      { index: 1, utilization_gpu_pct: 30, processes: [proc(22, "worker", "dave", "t5", 100, { workload_id: "slurm-7" })] },
    ],
  },
  {
    host: "atlas-03",
    status: "offline",
    stale: true,
    gpus: [{ index: 0, utilization_gpu_pct: 99, processes: [proc(31, "eval.py", "erin", "t6", 9_000)] }],
  },
];

const listed = jobs.list(servers, busyPct);
const byProgram = Object.fromEntries(listed.map((job) => [job.program, job]));
assert.equal(listed.length, 5);
assert.deepEqual([...byProgram["train.py"].gpus], [0, 1], "one launch across two GPUs is one job");
assert.equal(byProgram["train.py"].utilization, 80, "utilization is the mean over its GPUs");
assert.equal(byProgram["train.py"].state, "compute");
assert.equal(byProgram["train.py"].memoryMiB, 40_000);
assert.equal(byProgram.notebook.state, "idle", "holds memory below the busy threshold");
assert.equal(byProgram.rustdesk.state, "light", "a few MiB is a helper, not an idle job");
assert.equal(byProgram.worker.gpus.length, 2, "a scheduler id joins processes with different starts");
assert.equal(byProgram.worker.workloadId, "slurm-7");
assert.equal(byProgram["eval.py"].state, "offline", "a stale host's last processes are not live");

assert.deepEqual(
  jobs.filter(listed, { state: "idle" }).map((job) => job.program),
  ["notebook"],
);
assert.deepEqual(jobs.filter(listed, { owner: "dave" }).map((job) => job.program), ["worker"]);
assert.deepEqual(jobs.filter(listed, { query: "ATLAS-01 alice" }).map((job) => job.program), ["train.py"]);
assert.deepEqual(jobs.filter(listed, { query: "ｓｌｕｒｍ" }).map((job) => job.program), ["worker"], "NFKC folding");

assert.deepEqual(
  jobs.sort(listed).map((job) => job.program),
  ["train.py", "worker", "eval.py", "notebook", "rustdesk"],
  "more GPUs, then more memory",
);

const unknownMemory = jobs.list([{
  host: "h", status: "online",
  gpus: [{ index: 0, utilization_gpu_pct: 0, processes: [{ pid: 1, name: "x", used_memory_mib: null, workload: null }] }],
}], busyPct);
assert.equal(unknownMemory[0].state, "idle", "unknown memory is never called light");
assert.equal(unknownMemory[0].owner, null);

console.log("gpu-jobs tests passed");
