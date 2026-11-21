import assert from "node:assert/strict";

await import("../src/mocop/static/format.js");
await import("../src/mocop/static/event-stream.js");

const { appendStreamChunk } = globalThis.MocopFormat.create();
const { IDLE_TIMEOUT_MS, read } = globalThis.MocopEventStream;
const encoder = new TextEncoder();

// Deterministic timers: the reader only ever sees these two functions.
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

// A fetch stand-in whose body errors when the signal aborts, as fetch does.
function streamingServer({ status = 200 } = {}) {
  let body;
  const server = { signal: null, requests: 0 };
  server.request = (signal) => {
    server.requests += 1;
    server.signal = signal;
    const stream = new ReadableStream({
      start(controller) {
        body = controller;
      },
    });
    signal.addEventListener("abort", () => body.error(signal.reason));
    return Promise.resolve(new Response(stream, { status }));
  };
  server.send = (text) => body.enqueue(encoder.encode(text));
  server.close = () => body.close();
  return server;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

// The idle limit outlasts two heartbeats (15 s each) plus the server's poll.
assert.ok(IDLE_TIMEOUT_MS > 30_000 && IDLE_TIMEOUT_MS <= 60_000);

{
  // Frames split across reads, with CRLF line endings, arrive whole; a
  // stream that then goes silent is aborted by the watchdog and rejects
  // instead of leaving the dashboard reading a half-open connection forever.
  const clock = fakeClock();
  const server = streamingServer();
  const frames = [];
  let opened = 0;
  const reading = read({
    request: server.request,
    appendChunk: appendStreamChunk,
    onOpen: () => {
      opened += 1;
    },
    onFrame: (frame) => frames.push(frame),
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
  await settle();
  assert.equal(opened, 1);
  server.send("event: heartbeat\r\ndata: {}\r");
  await settle();
  assert.deepEqual(frames, []);
  server.send("\n\r\nevent: snapshot\ndata: {\"version\":2}\n\n");
  await settle();
  assert.deepEqual(frames, [
    "event: heartbeat\ndata: {}",
    "event: snapshot\ndata: {\"version\":2}",
  ]);
  // Every chunk rearmed the single watchdog rather than adding another.
  assert.deepEqual(clock.pending(), [IDLE_TIMEOUT_MS]);
  clock.fire();
  await assert.rejects(reading, /idle for 35000 ms/);
  assert.equal(server.signal.aborted, true);
  assert.deepEqual(clock.pending(), []);
}

{
  // A request whose response headers never arrive is bounded too.
  const clock = fakeClock();
  let signal;
  const reading = read({
    request: (requestSignal) => {
      signal = requestSignal;
      return new Promise((_resolve, reject) => {
        requestSignal.addEventListener("abort", () => reject(requestSignal.reason));
      });
    },
    appendChunk: appendStreamChunk,
    onOpen: () => assert.fail("a request without headers never opens"),
    onFrame: () => assert.fail("a request without headers has no frames"),
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
  await settle();
  assert.deepEqual(clock.pending(), [IDLE_TIMEOUT_MS]);
  clock.fire();
  await assert.rejects(reading, /idle/);
  assert.equal(signal.aborted, true);
}

{
  // A refused stream (the 17th client gets 503) rejects without opening and
  // leaves no timer behind.
  const clock = fakeClock();
  const server = streamingServer({ status: 503 });
  await assert.rejects(
    read({
      request: server.request,
      appendChunk: appendStreamChunk,
      onOpen: () => assert.fail("a refused stream never opens"),
      onFrame: () => assert.fail("a refused stream has no frames"),
      schedule: clock.schedule,
      cancel: clock.cancel,
    }),
    /HTTP 503/,
  );
  assert.deepEqual(clock.pending(), []);
}

{
  // A stream the server ends resolves; the caller reconnects on its own.
  const clock = fakeClock();
  const server = streamingServer();
  const frames = [];
  const reading = read({
    request: server.request,
    appendChunk: appendStreamChunk,
    onOpen: () => {},
    onFrame: (frame) => frames.push(frame),
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
  await settle();
  server.send("event: heartbeat\ndata: {}\n\n");
  server.close();
  await reading;
  assert.deepEqual(frames, ["event: heartbeat\ndata: {}"]);
  assert.deepEqual(clock.pending(), []);
}

{
  // A dashboard that stops (re-authentication required) releases the
  // connection after the next read instead of holding it open.
  const clock = fakeClock();
  const server = streamingServer();
  let active = true;
  const reading = read({
    request: server.request,
    appendChunk: appendStreamChunk,
    onOpen: () => {},
    onFrame: () => {
      active = false;
    },
    active: () => active,
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
  await settle();
  server.send("event: heartbeat\ndata: {}\n\n");
  await reading;
  assert.equal(server.signal.aborted, true);
  assert.deepEqual(clock.pending(), []);
}

{
  // A refused or failed stream backs off to 30 s; a stream that ended
  // normally (no failure) reconnects after the 1.2 s floor.
  const { reconnectDelayMs } = globalThis.MocopEventStream;
  assert.deepEqual(
    [0, 1, 2, 3, 4, 5, 6, 7, 1_000].map(reconnectDelayMs),
    [1_200, 1_200, 2_400, 4_800, 9_600, 19_200, 30_000, 30_000, 30_000],
  );
}

console.log("event-stream tests passed");
