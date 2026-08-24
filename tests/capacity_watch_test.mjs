import assert from "node:assert/strict";

await import("../src/mocop/static/capacity-watch.js");

class MemoryStorage {
  constructor(entries = []) {
    this.values = new Map(entries);
  }

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

class BrokenStorage {
  getItem() {
    throw new Error("storage disabled");
  }

  setItem() {
    throw new Error("storage disabled");
  }

  removeItem() {
    throw new Error("storage disabled");
  }
}

const STORAGE_KEY = "mocop.capacityWatch.v1";

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
  // Matching: ranking, maintenance exclusion, host blockers, GPU blockers.
  const watch = globalThis.MocopCapacityWatch.create({ storage: new MemoryStorage() });
  const result = watch.matches({
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
  const watch = globalThis.MocopCapacityWatch.create({ storage: new MemoryStorage() });
  const result = watch.matches({
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

{
  // Watch persistence round-trips and rejects malformed state.
  const storage = new MemoryStorage();
  const watch = globalThis.MocopCapacityWatch.create({ storage });
  assert.equal(watch.loadWatch(), null);
  assert.equal(watch.saveWatch({ gpuCount: 0, minVramGiB: 24, model: "any" }), null);
  const saved = watch.saveWatch(REQUEST);
  assert.equal(saved.state, "armed");
  assert.equal(saved.lastNotifiedAt, null);
  assert.deepEqual(watch.loadWatch(), saved);
  storage.setItem(STORAGE_KEY, JSON.stringify({ version: 1, request: REQUEST, state: "bogus", lastNotifiedAt: null }));
  assert.equal(watch.loadWatch(), null);
  storage.setItem(STORAGE_KEY, "{not json");
  assert.equal(watch.loadWatch(), null);
  watch.saveWatch(REQUEST);
  watch.clearWatch();
  assert.equal(watch.loadWatch(), null);
}

{
  // Notification edge: fire once, cool down, re-arm, fire again.
  let clock = 1_000_000;
  const watch = globalThis.MocopCapacityWatch.create({
    storage: new MemoryStorage(),
    now: () => clock,
  });
  let state = watch.saveWatch(REQUEST);

  let step = watch.evaluateWatch(state, 2);
  assert.equal(step.shouldNotify, true);
  assert.equal(step.watch.state, "notified");
  assert.equal(step.watch.lastNotifiedAt, clock);
  state = step.watch;

  // Still satisfied: no repeat notification.
  step = watch.evaluateWatch(state, 2);
  assert.equal(step.shouldNotify, false);
  assert.equal(step.watch.state, "notified");
  state = step.watch;

  // Demand stops being satisfied: silently re-arm.
  clock += 5_000;
  step = watch.evaluateWatch(state, 0);
  assert.equal(step.shouldNotify, false);
  assert.equal(step.watch.state, "armed");
  state = step.watch;

  // Satisfied again inside the cooldown: stay armed without notifying.
  clock += 10_000;
  step = watch.evaluateWatch(state, 1);
  assert.equal(step.shouldNotify, false);
  assert.equal(step.watch.state, "armed");
  state = step.watch;

  // After the cooldown the armed edge fires again.
  clock += 60_000;
  step = watch.evaluateWatch(state, 1);
  assert.equal(step.shouldNotify, true);
  assert.equal(step.watch.state, "notified");
}

{
  // Broken storage never breaks the in-memory watch lifecycle.
  const watch = globalThis.MocopCapacityWatch.create({ storage: new BrokenStorage() });
  assert.equal(watch.loadWatch(), null);
  const saved = watch.saveWatch(REQUEST);
  assert.equal(saved.state, "armed");
  const step = watch.evaluateWatch(saved, 3);
  assert.equal(step.shouldNotify, true);
  watch.clearWatch();
}

console.log("capacity watch contract passed");
