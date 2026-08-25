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
const REQUEST = { gpuCount: 2, minVramGiB: 24, model: "any" };

{
  // Pure presentation strings live with the state that produces them.
  const watch = globalThis.MocopCapacityWatch.create({ storage: new MemoryStorage() });
  assert.match(watch.describeRequest(REQUEST), /2 张 GPU/);
  assert.match(watch.describeRequest({ ...REQUEST, model: "any" }), /不限型号/);
  assert.match(
    watch.controlText({ state: "notified", request: REQUEST }, 3),
    /已就绪 · 3 个节点/,
  );
  assert.match(
    watch.controlText({ state: "armed", request: REQUEST }, 0),
    /^守望中/,
  );
  assert.match(watch.bannerText({ state: "notified", request: REQUEST }, 5), /5 个节点/);
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

  // A wall-clock rollback must not freeze notifications: re-arm, then step the
  // clock backwards and confirm the next satisfaction edge still fires.
  step = watch.evaluateWatch(step.watch, 0);
  assert.equal(step.watch.state, "armed");
  clock -= 3_600_000;
  step = watch.evaluateWatch(step.watch, 1);
  assert.equal(step.shouldNotify, true, "rollback does not freeze the watch");
}

{
  // A watch on a long-but-real GPU model name (up to the probe's 256 bound)
  // saves instead of failing validation.
  const watch = globalThis.MocopCapacityWatch.create({ storage: new MemoryStorage() });
  const longModel = "NVIDIA " + "H".repeat(240);
  assert.equal(longModel.length <= 256, true);
  const saved = watch.saveWatch({ gpuCount: 1, minVramGiB: 1, model: longModel });
  assert.notEqual(saved, null);
  assert.equal(saved.request.model, longModel);
  assert.equal(
    watch.saveWatch({ gpuCount: 1, minVramGiB: 1, model: "x".repeat(257) }),
    null,
  );
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

{
  // Two tabs share one storage. A watch stopped in one tab must not be
  // resurrected by the other tab's next evaluation, and a newer request must
  // not be overwritten by a tab holding a stale copy.
  const storage = new MemoryStorage();
  const tabA = globalThis.MocopCapacityWatch.create({ storage });
  const tabB = globalThis.MocopCapacityWatch.create({ storage });

  const held = tabA.saveWatch(REQUEST);
  tabB.clearWatch();
  const revived = tabA.evaluateWatch(held, 2);
  assert.equal(revived.watch, null, "a stopped watch is not resurrected");
  assert.equal(revived.shouldNotify, false);
  assert.equal(storage.getItem(STORAGE_KEY), null);

  const staleA = tabA.saveWatch(REQUEST);
  tabB.saveWatch({ gpuCount: 8, minVramGiB: 80, model: "any" });
  const outcome = tabA.evaluateWatch(staleA, 1);
  assert.equal(outcome.watch.request.gpuCount, 8, "newer request wins");
  assert.equal(outcome.shouldNotify, false);
}

console.log("capacity watch contract passed");
