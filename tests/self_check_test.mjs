import assert from "node:assert/strict";

await import("../src/mocop/static/fleet-state.js");
await import("../src/mocop/static/self-check.js");

const { parseColor, composite, contrastRatio } = globalThis.MocopSelfCheck;
const check = globalThis.MocopSelfCheck.create({ fleetState: globalThis.MocopFleetState.create() });

assert.deepEqual(parseColor("rgb(255, 255, 255)"), [255, 255, 255, 1]);
assert.deepEqual(parseColor("rgba(0, 0, 0, 0.5)"), [0, 0, 0, 0.5]);
assert.deepEqual(parseColor("color(srgb 1 0 0 / 0.25)"), [255, 0, 0, 0.25]);
assert.equal(parseColor("hsl(0 0% 0%)"), null, "an unreadable color is reported, not guessed");
assert.deepEqual(composite([255, 255, 255, 0.5], [0, 0, 0, 1]), [127.5, 127.5, 127.5, 1]);

const white = [255, 255, 255, 1];
const black = [0, 0, 0, 1];
assert.equal(contrastRatio(white, black, black).toFixed(1), "21.0");
assert.equal(contrastRatio(black, black, black), 1);
// Text at 46 % white on #1c1c1e measured 4.58:1 in the v13 prototype.
assert.equal(contrastRatio([255, 255, 255, 0.46], [28, 28, 30, 1], black).toFixed(2), "4.58");

const thresholds = { gpu_busy_pct: 10 };
const gpu = (utilization, memory, processes = []) => ({
  utilization_gpu_pct: utilization, memory_used_mib: memory, processes, processes_available: true,
});
const snapshot = {
  thresholds,
  stats: {
    gpus: 3, busyGpus: 1, onlineServers: 1, memoryUsedMiB: 3072.0, cpuAveragePct: 25, activeIncidents: 2, criticalIncidents: 1,
    diskTotalMiB: 100, sharedDisks: 1,
  },
  servers: [
    {
      host: "a",
      status: "online",
      system: {
        cpu_usage_pct: 25,
        disks: [
          { device: "/dev/sda1", filesystem_type: "ext4", total_mib: 100, used_mib: 10 },
          { device: "lfs@tcp:/share", filesystem_type: "lustre", total_mib: 1000, used_mib: 500 },
        ],
      },
      gpus: [gpu(90, 1024), gpu(0, 1024), gpu(5, 1024)],
    },
    { host: "b", status: "offline", system: { cpu_usage_pct: 99 }, gpus: [gpu(100, 9999)] },
  ],
};
const capacity = (available) => ({ candidates: [{ host: "a", available: available.map((index) => ({ index })) }] });
const rows = check.reconcile({
  snapshot,
  activeIncidents: [{ severity: "critical" }, { severity: "warning" }],
  browserCapacity: capacity([1, 2]),
  serverCapacity: capacity([1, 2]),
});
assert.deepEqual(rows.filter((item) => !item.ok), [], "a consistent snapshot passes every row");
assert.deepEqual(rows.map((item) => item.id), [
  "gpus", "busy-gpus", "online-servers", "gpu-memory", "cpu", "disk-local", "disk-shared", "incidents", "critical",
  "capacity-gpus", "capacity-hosts",
]);

// Every row must be able to fail: a check that cannot fail proves nothing.
const drifted = check.reconcile({
  snapshot: {
    ...snapshot,
    stats: {
      ...snapshot.stats, busyGpus: 2, gpus: 4, onlineServers: 2, memoryUsedMiB: 9, cpuAveragePct: 26,
      activeIncidents: 3, criticalIncidents: 0, diskTotalMiB: 1100, sharedDisks: 2,
    },
  },
  activeIncidents: [{ severity: "critical" }, { severity: "warning" }],
  browserCapacity: capacity([1, 2]),
  serverCapacity: { candidates: [{ host: "a", available: [{ index: 1 }] }, { host: "c", available: [{ index: 0 }, { index: 1 }] }] },
});
assert.deepEqual(drifted.filter((item) => item.ok).map((item) => item.id), [], "every row detects drift");

const partial = check.reconcile({ snapshot, activeIncidents: null, browserCapacity: null, serverCapacity: null });
assert.deepEqual(partial.map((item) => item.id), ["gpus", "busy-gpus", "online-servers", "gpu-memory", "cpu", "disk-local", "disk-shared"],
  "rows without an independent referee are omitted, not passed");

const contrast = check.contrastRows([{ id: "muted", label: "说明文字", ratio: 4.51 }, { id: "faint", label: "最淡", ratio: 2.7 }, { id: "x", label: "?", ratio: NaN }]);
assert.deepEqual(contrast.map((item) => item.ok), [true, false, false]);
assert.equal(contrast[2].actual, "无法读取");

assert.equal(check.layoutRow({ viewportWidth: 390, documentWidth: 390 }).ok, true);
assert.equal(check.layoutRow({ viewportWidth: 390, documentWidth: 412 }).ok, false);

console.log("self-check tests passed");
