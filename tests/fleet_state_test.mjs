import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

await import("../src/mocop/static/fleet-state.js");

const state = globalThis.MocopFleetState.create();
const busyPct = 10;
const gpu = (utilization, processes = [], extra = {}) => ({
  utilization_gpu_pct: utilization, processes, processes_available: true, ...extra,
});
const process = (usedMiB) => ({ pid: 1, used_memory_mib: usedMiB });

// The busy boundary is inclusive, exactly like stats.busyGpus.
assert.equal(state.activity(gpu(10), { online: true, busyPct }), "compute");
assert.equal(state.activity(gpu(9.9), { online: true, busyPct }), "free");
assert.equal(state.activity(gpu(null), { online: true, busyPct }), "unknown");
assert.equal(state.activity(gpu(90), { online: false, busyPct }), "offline");

// A process at the holder floor occupies the card; one just under it does not,
// and a process whose memory is unknown is never treated as tiny.
assert.equal(state.activity(gpu(0, [process(256)]), { online: true, busyPct }), "occupied");
assert.equal(state.activity(gpu(0, [process(255)]), { online: true, busyPct }), "free");
assert.equal(state.activity(gpu(0, [process(null)]), { online: true, busyPct }), "occupied");
assert.equal(
  state.activity(gpu(0, [], { processes_available: false }), { online: true, busyPct }),
  "unknown",
  "a card without a process sample cannot be called free",
);

const servers = [
  { host: "a", status: "online", gpus: [gpu(95), gpu(0, [process(4096)]), gpu(0), gpu(null)] },
  { host: "b", status: "offline", stale: true, gpus: [gpu(80), gpu(0)] },
];
const counts = state.breakdown(servers, busyPct);
assert.deepEqual(
  { ...counts },
  { compute: 1, occupied: 1, free: 1, unknown: 1, offline: 2, online: 4 },
);
assert.equal(
  counts.online,
  servers[0].gpus.length,
  "online GPUs equal stats.gpus, which only counts online hosts",
);

assert.equal(state.isShared("lustre"), true);
assert.equal(state.isShared("fuse.juicefs"), true);
assert.equal(state.isShared("nfs4"), true);
assert.equal(state.isShared("ext4"), false);
assert.equal(state.isShared("overlay"), false);

const disk = (device, type, usedMiB, totalMiB) => ({
  device, filesystem_type: type, used_mib: usedMiB, total_mib: totalMiB,
});
const storage = state.storageTotals([
  {
    status: "online",
    system: { disks: [disk("/dev/sda1", "ext4", 10, 100), disk("lfs@tcp:/share", "lustre", 500, 1000)] },
  },
  {
    status: "online",
    system: {
      disks: [
        disk("/dev/nvme0n1p1", "xfs", 20, 200),
        disk("/dev/nvme0n1p1", "xfs", 20, 200),
        disk("lfs@tcp:/share", "lustre", 500, 1000),
      ],
    },
  },
  { status: "offline", system: { disks: [disk("/dev/sdb", "ext4", 1, 999)] } },
]);
assert.deepEqual({ ...storage.local }, { usedMiB: 30, totalMiB: 300 });
assert.deepEqual({ ...storage.shared }, { volumes: 1, usedMiB: 500, totalMiB: 1000 });

// The fixture is the contract shared with mocop/storage_totals.py.
const fixture = JSON.parse(readFileSync(new URL("./fixtures/storage_totals.json", import.meta.url), "utf-8"));
for (const kind of fixture.filesystems.shared) assert.equal(state.isShared(kind), true, kind);
for (const kind of fixture.filesystems.local) assert.equal(state.isShared(kind), false, kind);
for (const kind of state.SHARED_FILESYSTEMS) assert(fixture.filesystems.shared.includes(kind), `${kind} is missing from the fixture`);
for (const { name, servers: cases, expected } of fixture.cases) {
  const totals = state.storageTotals(cases);
  assert.deepEqual({ local: { ...totals.local }, shared: { ...totals.shared } }, expected, name);
}

console.log("fleet-state tests passed");
