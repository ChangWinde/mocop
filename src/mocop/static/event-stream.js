// One authenticated read of the Server-Sent Events stream, extracted from
// app.js under the ADR-0021 leaf pattern. The service writes a named heartbeat
// after 15 quiet seconds, so a stream that delivers no byte for longer than
// the idle limit is half-open: a sleeping laptop, a NAT or proxy that dropped
// the flow, or a tunnel that lost its peer without sending FIN or RST. The
// watchdog aborts such a request, which makes the read reject, so the caller
// falls back to polling and reconnects instead of waiting on it forever. No
// DOM and no state: the caller supplies the request and the frame handler.
(() => {
  "use strict";

  // Two missed heartbeats plus a grace period for the server's stop poll.
  const IDLE_TIMEOUT_MS = 35_000;

  async function read({
    request,
    appendChunk,
    onOpen,
    onFrame,
    active = () => true,
    idleTimeoutMs = IDLE_TIMEOUT_MS,
    schedule = globalThis.setTimeout,
    cancel = globalThis.clearTimeout,
  }) {
    const controller = new AbortController();
    let timer = null;
    let stalled = false;
    // Each byte rearms the watchdog; the first arm also bounds a request
    // whose response headers never arrive.
    const arm = () => {
      if (timer != null) cancel(timer);
      timer = schedule(() => {
        stalled = true;
        controller.abort();
      }, idleTimeoutMs);
    };
    try {
      arm();
      const response = await request(controller.signal);
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
      onOpen();
      arm();
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (active()) {
        const { done, value } = await reader.read();
        if (done) return;
        arm();
        buffer = appendChunk(buffer, decoder.decode(value, { stream: true }));
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          onFrame(frame);
          boundary = buffer.indexOf("\n\n");
        }
      }
    } catch (error) {
      if (stalled) throw new Error(`event stream idle for ${idleTimeoutMs} ms`);
      throw error;
    } finally {
      if (timer != null) cancel(timer);
      // Release the connection when the dashboard stops or the read failed.
      controller.abort();
    }
  }

  // Delay before the next connect after consecutive failed reads: 1.2 s,
  // doubling to a 30 s ceiling, so a client refused with 503 at the stream
  // limit stops retrying every 1.2 s while the service stays reachable.
  function reconnectDelayMs(failures) {
    return Math.min(30_000, 1_200 * 2 ** Math.max(0, Math.min(failures, 16) - 1));
  }

  globalThis.MocopEventStream = Object.freeze({ IDLE_TIMEOUT_MS, read, reconnectDelayMs });
})();
