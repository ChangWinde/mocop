// Bounded-backoff loading of one keyed resource (a host's trend history, a
// GPU's history), extracted from app.js under the ADR-0021 leaf pattern. At
// most one request is in flight; a key is confirmed only by a successful load
// so failures stay retryable; one retry timer per failed key doubles from 4 s
// to a 30 s ceiling; and a response for a superseded request is ignored. No
// DOM, no storage: the caller supplies the fetch and a settle callback.
(() => {
  "use strict";

  const MIN_RETRY_MS = 4_000;
  const MAX_RETRY_MS = 30_000;

  function create({
    load,
    retry,
    onSettled,
    schedule = globalThis.setTimeout,
    cancel = globalThis.clearTimeout,
  }) {
    const state = {
      value: null,
      key: "",
      loading: false,
      error: false,
      fetchKey: null,
      retryKey: "",
      retryTimer: null,
      retryDelayMs: 0,
      request: 0,
    };

    function clearRetry() {
      if (state.retryTimer != null) cancel(state.retryTimer);
      state.retryTimer = null;
      state.retryKey = "";
    }

    // Forget the current resource: a new key will load from scratch, an
    // in-flight response is discarded, and any pending retry is dropped.
    function reset({ loading = false } = {}) {
      clearRetry();
      state.value = null;
      state.key = "";
      state.fetchKey = null;
      state.error = false;
      state.retryDelayMs = 0;
      state.loading = loading;
      state.request += 1;
    }

    async function request(key) {
      if (key === state.key || key === state.fetchKey) return;
      if (state.retryTimer != null) {
        // A failed key waits for its single backoff timer; only a new key
        // justifies an immediate replacement fetch.
        if (key === state.retryKey) return;
        clearRetry();
      }
      state.fetchKey = key;
      state.loading = true;
      const id = ++state.request;
      onSettled();
      try {
        const value = await load(key);
        if (id !== state.request) return;
        // `undefined` means the caller recognised a response that no longer
        // matches its selection; keep the previous value and stay retryable.
        if (value === undefined) return;
        state.value = value;
        state.key = key;
        state.error = false;
        state.retryDelayMs = 0;
      } catch (_error) {
        if (id !== state.request) return;
        // Existing samples stay on screen through a transient failure.
        state.error = true;
        state.retryDelayMs = Math.min(
          MAX_RETRY_MS,
          Math.max(MIN_RETRY_MS, state.retryDelayMs * 2),
        );
        state.retryKey = key;
        state.retryTimer = schedule(() => {
          state.retryTimer = null;
          state.retryKey = "";
          retry();
        }, state.retryDelayMs);
      } finally {
        if (state.fetchKey === key) state.fetchKey = null;
        if (id === state.request) {
          state.loading = false;
          onSettled();
        }
      }
    }

    return Object.freeze({ state, request, reset });
  }

  globalThis.MocopKeyedLoader = Object.freeze({ create });
})();
