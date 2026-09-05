import assert from "node:assert/strict";

await import("../src/mocop/static/keyed-loader.js");

// Deterministic timers: the loader only ever sees these two functions.
function fakeClock() {
  const timers = new Map();
  let next = 1;
  return {
    schedule(callback, delayMs) {
      const id = next++;
      timers.set(id, { callback, delayMs });
      return id;
    },
    cancel(id) {
      timers.delete(id);
    },
    pending() {
      return [...timers.values()].map((timer) => timer.delayMs);
    },
    fire() {
      const [id, timer] = [...timers.entries()][0];
      timers.delete(id);
      timer.callback();
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

{
  // Success path: one request per new key, settle callbacks around it, and
  // the key confirmed only after the value arrives.
  const clock = fakeClock();
  const loads = [];
  let settled = 0;
  const loader = globalThis.MocopKeyedLoader.create({
    load: async (key) => { loads.push(key); return { key }; },
    retry: () => {},
    onSettled: () => { settled += 1; },
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
  const first = loader.request("a");
  assert.equal(loader.state.loading, true);
  assert.equal(loader.state.fetchKey, "a");
  assert.equal(settled, 1);
  await first;
  assert.deepEqual(loader.state.value, { key: "a" });
  assert.equal(loader.state.key, "a");
  assert.equal(loader.state.loading, false);
  assert.equal(settled, 2);
  // Same key: no second request.
  await loader.request("a");
  assert.deepEqual(loads, ["a"]);
}

{
  // Failure path: bounded doubling backoff, one timer per failed key, the
  // retry callback fires when the timer does, and a new key cancels the wait.
  const clock = fakeClock();
  let fail = true;
  let retries = 0;
  const loader = globalThis.MocopKeyedLoader.create({
    load: async (key) => { if (fail) throw new Error("down"); return key; },
    retry: () => { retries += 1; },
    onSettled: () => {},
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
  await loader.request("a");
  assert.equal(loader.state.error, true);
  assert.equal(loader.state.value, null);
  assert.deepEqual(clock.pending(), [4_000]);
  // The failed key waits for its timer instead of hammering the service.
  await loader.request("a");
  assert.deepEqual(clock.pending(), [4_000]);
  clock.fire();
  assert.equal(retries, 1);
  await loader.request("a");
  assert.deepEqual(clock.pending(), [8_000]);
  clock.fire();
  await loader.request("a");
  clock.fire();
  await loader.request("a");
  clock.fire();
  await loader.request("a");
  assert.deepEqual(clock.pending(), [30_000], "the backoff is capped at 30 s");
  // A different key replaces the pending retry immediately.
  fail = false;
  await loader.request("b");
  assert.deepEqual(clock.pending(), []);
  assert.equal(loader.state.value, "b");
  assert.equal(loader.state.error, false);
  assert.equal(loader.state.retryDelayMs, 0);
}

{
  // Stale responses: a reset or a newer request discards the older answer,
  // and `undefined` from load() keeps the previous value without confirming.
  const clock = fakeClock();
  const gates = [];
  const loader = globalThis.MocopKeyedLoader.create({
    load: (key) => { const gate = deferred(); gates.push({ key, gate }); return gate.promise; },
    retry: () => {},
    onSettled: () => {},
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
  const slow = loader.request("a");
  loader.reset({ loading: true });
  assert.equal(loader.state.loading, true);
  gates[0].gate.resolve("late");
  await slow;
  assert.equal(loader.state.value, null, "a reset discards the in-flight answer");
  const current = loader.request("b");
  gates[1].gate.resolve(undefined);
  await current;
  assert.equal(loader.state.key, "", "undefined does not confirm the key");
  assert.equal(loader.state.loading, false);
  await tick();
  const confirmed = loader.request("b");
  gates[2].gate.resolve("b-value");
  await confirmed;
  assert.equal(loader.state.value, "b-value");
  assert.equal(loader.state.key, "b");
}

console.log("keyed loader contract passed");
