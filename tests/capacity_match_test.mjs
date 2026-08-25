import assert from "node:assert/strict";

await import("../src/mocop/static/capacity-match.js");

const match = globalThis.MocopCapacityMatch.create();

function gpu(index, overrides = {}) {
  return {
    index,
    uuid: `GPU-${index}`,
    name: "NVIDIA H100 80GB HBM3",
    utilization_gpu_pct: 2,
    memory_free_mib: 80_000,
    temperature_c: 40,
    ...overrides,
  };
}

function server(host, gpus, overrides = {}) {
  return {
    host,
    status: "online",
    stale: false,
    maintenance: null,
    system: { cpu_usage_pct: 10 },
    gpus,
    ...overrides,
  };
}

const REQUEST = { gpuCount: 2, minVramGiB: 24, model: "any" };
const BOUNDS = { busyPct: 10, temperatureC: 80 };

{
  // Ranking, maintenance exclusion, host blockers, and GPU blockers.
  const result = match.matches({
    servers: [
      server("busy-host", [gpu(0, { utilization_gpu_pct: 96 }), gpu(1, { utilization_gpu_pct: 97 })]),
      server("ready-host", [gpu(0), gpu(1), gpu(2, { utilization_gpu_pct: 55 })]),
      server("maintenance-host", [gpu(0), gpu(1)], { maintenance: { reason: "window" } }),
      server("alerting-host", [gpu(0), gpu(1)]),
      server("faulty-gpu-host", [gpu(0), gpu(1, { uuid: "GPU-BAD" })]),
      server("offline-host", [gpu(0), gpu(1)], { status: "offline" }),
    ],
    activeConditions: [
      { host: "alerting-host", category: "connectivity", conditionKey: "c", resource: "SSH" },
      { host: "faulty-gpu-host", category: "gpu_ecc", conditionKey: "gpu_ecc:GPU-BAD", resource: "GPU 1" },
    ],
    request: REQUEST,
    ...BOUNDS,
  });
  assert.equal(result.excludedMaintenance, 1);
  assert.equal(result.excludedHealth, 1);
  const byHost = new Map(result.candidates.map((candidate) => [candidate.host, candidate]));
  assert.equal(byHost.get("ready-host").satisfies, true);
  assert.equal(byHost.get("ready-host").available.length, 2);
  assert.equal(byHost.get("busy-host").satisfies, false);
  assert.equal(byHost.get("busy-host").deficit, 2);
  assert.equal(byHost.get("faulty-gpu-host").satisfies, false);
  assert.equal(byHost.get("faulty-gpu-host").available.length, 1);
  assert.equal(byHost.has("offline-host"), false);
  assert.equal(result.candidates[0].host, "ready-host");
}

{
  // Model filter narrows candidate groups.
  const result = match.matches({
    servers: [server("mixed-host", [
      gpu(0),
      gpu(1, { name: "NVIDIA GeForce RTX 4090" }),
    ])],
    activeConditions: [],
    request: { gpuCount: 1, minVramGiB: 1, model: "NVIDIA GeForce RTX 4090" },
    ...BOUNDS,
  });
  assert.equal(result.candidates.length, 1);
  assert.equal(result.candidates[0].model, "NVIDIA GeForce RTX 4090");
}

console.log("capacity match contract passed");
