import assert from "node:assert/strict";

await import("../src/mocop/static/use-check.js");

const use = globalThis.MocopUseCheck.create({
  format: (value) => String(Math.round(value)),
  rate: (value) => `${value} B/s`,
});
const thresholds = {
  cpu_warning_pct: 85,
  memory_warning_pct: 90,
  swap_warning_pct: 50,
  disk_warning_pct: 85,
  psi_memory_some_pct: 20,
  psi_io_some_pct: 30,
};
const system = (overrides = {}) => ({
  cpu_cores: 8,
  cpu_usage_pct: 20,
  load_1m: 4,
  memory_total_mib: 1000,
  memory_used_mib: 500,
  swap_total_mib: 0,
  swap_used_mib: 0,
  network_rx_bps: 10,
  network_tx_bps: 20,
  disks: [{ used_pct: 40 }, { used_pct: 70 }],
  pressure: null,
  ...overrides,
});
const byResource = (rows) => Object.fromEntries(rows.map((row) => [row.resource, row.cells]));

assert.deepEqual(use.rows({ system: null, gpus: [] }, thresholds), [], "no sample, no rows");

const calm = byResource(use.rows({ system: system(), gpus: [] }, thresholds));
assert.deepEqual(Object.keys(calm), ["CPU", "内存", "磁盘", "网络"], "no GPU row without GPUs");
assert.equal(calm.CPU[0].text, "20%");
assert.equal(calm.CPU[1].text, "负载/核 0.50 · PSI —");
assert.equal(calm.CPU[2].text, use.NOT_COLLECTED, "errors the probe cannot see stay explicit");
assert.equal(calm.内存[1].text, "Swap 未配置 · PSI —");
assert.equal(calm.磁盘[0].text, "最满 70%");
assert.equal(calm.网络[0].text, "↓ 10 B/s ↑ 20 B/s");
assert.ok(Object.values(calm).flat().every((item) => item.state === ""));

const strained = byResource(use.rows({
  system: system({
    cpu_usage_pct: 90,
    load_1m: 16,
    swap_total_mib: 100,
    swap_used_mib: 60,
    disks: [{ used_pct: 96 }],
    pressure: { memory: { some_avg10: 5 }, io: { some_avg10: 31 }, cpu: { some_avg10: 2 } },
  }),
  gpus: [
    { utilization_gpu_pct: 100, utilization_memory_pct: 40, health: { thermal_slowdown: true } },
    { utilization_gpu_pct: 0, utilization_memory_pct: 0, health: { ecc_uncorrected_volatile: 2 } },
  ],
}, thresholds));
assert.equal(strained.CPU[0].state, "warning");
assert.equal(strained.CPU[1].state, "warning", "two runnable threads per core is saturation");
assert.equal(strained.内存[1].state, "warning", "swap above its threshold");
assert.equal(strained.磁盘[0].state, "critical", "a mount at 95 % or more is critical");
assert.equal(strained.磁盘[1].state, "warning");
assert.equal(strained.GPU[0].text, "平均 50%");
assert.equal(strained.GPU[1].text, "显存带宽 20% · 1 张降频");
assert.equal(strained.GPU[1].state, "warning");
assert.deepEqual({ ...strained.GPU[2] }, { text: "1 张", state: "critical" });

const unknownHealth = byResource(use.rows({
  system: system(),
  gpus: [{ utilization_gpu_pct: null, utilization_memory_pct: null, health: null }],
}, thresholds));
assert.equal(unknownHealth.GPU[0].text, use.NOT_COLLECTED);
assert.equal(unknownHealth.GPU[2].text, use.NOT_COLLECTED, "no health block is not a clean bill");

console.log("use-check tests passed");
