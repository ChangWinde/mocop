import assert from "node:assert/strict";

await import("../src/mocop/static/gpu-tasks.js");

const tasks = globalThis.MocopGpuTasks.create();

function process(name, command, overrides = {}) {
  const { workload, ...rest } = overrides;
  return {
    pid: 4242,
    name,
    used_memory_mib: 1024,
    workload: command === null && !workload
      ? null
      : { kind: "process", command, ...workload },
    ...rest,
  };
}

// --- taskEntry: interpreters surface the real entry point ---------------

assert.equal(
  tasks.taskEntry(process(
    "/home/x/miniforge3/envs/vggt/bin/python3.10",
    "/home/x/envs/vggt/bin/python3.10 -u -m train.dragon_video2motion --config c.yaml",
  )),
  "train.dragon_video2motion",
  "-m module wins even behind -u",
);
assert.equal(
  tasks.taskEntry(process("/usr/bin/python3", "python3 train.py --epochs 3")),
  "train.py",
);
assert.equal(
  tasks.taskEntry(process("/usr/bin/python3", "python3 /workspace/jobs/finetune.py")),
  "finetune.py",
  "script paths reduce to their basename",
);
assert.equal(
  tasks.taskEntry(process("/usr/bin/torchrun", "torchrun --nproc_per_node=8 train.py")),
  "train.py",
  "launcher options are skipped",
);
assert.equal(
  tasks.taskEntry(process("/usr/bin/bash", "bash run_eval.sh --suite full")),
  "run_eval.sh",
);
assert.equal(
  tasks.taskEntry(process("/usr/bin/python3", 'python3 -c "print(1)"')),
  null,
  "inline code has no nameable entry",
);
assert.equal(
  tasks.taskEntry(process("/usr/bin/python3", "python3 --config data.yaml")),
  null,
  "non-script arguments never fake an entry point",
);
assert.equal(
  tasks.taskEntry(process("/opt/bin/ollama", "ollama serve")),
  null,
  "real binaries keep their own name",
);
assert.equal(tasks.taskEntry(process("/usr/bin/python3", null)), null);

// --- environmentName: conda, uv tools, poetry, project venvs ------------

assert.equal(
  tasks.environmentName(process("/home/x/miniforge3/envs/vggt/bin/python3.10", null)),
  "vggt",
);
assert.equal(
  tasks.environmentName(process("/home/x/.local/share/uv/tools/mocop/bin/python", null)),
  "mocop",
);
assert.equal(
  tasks.environmentName(process(
    "/home/x/.cache/pypoetry/virtualenvs/api-9dJq/bin/python", null,
  )),
  "api-9dJq",
);
assert.equal(
  tasks.environmentName(process("/home/x/cw/project/RAT-Image/.venv/bin/python", null)),
  "RAT-Image",
  "a project-local venv names the project",
);
assert.equal(tasks.environmentName(process("/usr/bin/python3", null)), null);

// --- footprint: average busy cores and resident memory ------------------

const nowMs = Date.parse("2026-08-26T12:00:00Z");
const busy = tasks.footprint(process("/usr/bin/python3", "python3 t.py", {
  workload: {
    started_at: "2026-08-26T09:00:00Z", // 3 h runtime
    cpu_seconds: 43_200, // 4 busy cores
    rss_mib: 8_192,
  },
}), nowMs);
assert.equal(busy.averageCores, 4);
assert.equal(busy.memoryMiB, 8_192);

const unknown = tasks.footprint(process("/usr/bin/python3", "python3 t.py"), nowMs);
assert.equal(unknown.averageCores, null, "no cpu sample means no estimate");
assert.equal(unknown.memoryMiB, null);

const noStart = tasks.footprint(process("/usr/bin/python3", "python3 t.py", {
  workload: { cpu_seconds: 100, rss_mib: 512 },
}), nowMs);
assert.equal(noStart.averageCores, null, "cpu needs a start time to average");
assert.equal(noStart.memoryMiB, 512);

// A clock skew that puts the start in the future must not produce negative
// or explosive core counts: runtime clamps at one second.
const skewed = tasks.footprint(process("/usr/bin/python3", "python3 t.py", {
  workload: { started_at: "2026-08-26T13:00:00Z", cpu_seconds: 30, rss_mib: 64 },
}), nowMs);
assert.equal(skewed.averageCores, 30);

// --- summarize: per-card projection stays faithful -----------------------

const summary = tasks.summarize({
  processes: [
    process("/usr/bin/python3", "python3 a.py", {
      pid: 1, used_memory_mib: 2_000,
      workload: { owner: "alice", started_at: "2026-08-26T08:00:00Z" },
    }),
    process("/usr/bin/python3", "python3 b.py", {
      pid: 2, used_memory_mib: 5_000,
      workload: { owner: "bob", started_at: "2026-08-26T10:00:00Z" },
    }),
    process("nvtop", null, { pid: 3, used_memory_mib: null }),
  ],
});
assert.equal(summary.count, 3);
assert.equal(summary.knownMemoryMiB, 7_000);
assert.equal(summary.knownMemoryCount, 2);
assert.equal(summary.topProcess.pid, 2);
assert.equal(summary.ownedCount, 2);
assert.equal(summary.identifiedCount, 2);
assert.equal(summary.ownerCount, 2);
assert.equal(summary.oldestProcess.pid, 1);

const empty = tasks.summarize({ processes: null });
assert.equal(empty.count, 0);
assert.equal(empty.topProcess, null);

// --- processName / processStartMs ----------------------------------------

assert.equal(tasks.processName({ name: "C:\\tools\\python.exe" }), "python.exe");
assert.equal(tasks.processName({ name: "" }), "unknown process");
assert.equal(
  tasks.processStartMs({ workload: null, first_seen_at: "2026-08-26T10:00:00Z" }),
  Date.parse("2026-08-26T10:00:00Z"),
);
assert.equal(
  tasks.processStartMs({ workload: null }),
  Number.MAX_SAFE_INTEGER,
  "undated processes sort behind every dated one",
);

console.log("gpu-tasks contract: all assertions passed");
