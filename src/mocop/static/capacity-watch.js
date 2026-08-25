// The capacity watch state machine: a durable, per-browser demand that fires
// one notification when a satisfying idle GPU combination appears. Extracted
// from app.js under the ADR-0021 leaf pattern; the stateless matching
// projection lives in capacity-match.js. This leaf owns storage
// reconciliation, the armed/notified edge with its cooldown, and the pure
// text a watch presents. No DOM, no network.
(() => {
  "use strict";

  const STORAGE_KEY = "mocop.capacityWatch.v1";
  const NOTIFY_COOLDOWN_MS = 60_000;

  function validRequest(request) {
    return request != null
      && typeof request === "object"
      && Number.isSafeInteger(request.gpuCount)
      && request.gpuCount >= 1
      && request.gpuCount <= 256
      && Number.isInteger(request.minVramGiB)
      && request.minVramGiB >= 0
      && request.minVramGiB <= 512
      && typeof request.model === "string"
      && request.model.length >= 1
      // Match the probe's GPU-name bound so a watch on any real device model
      // saves instead of failing silently.
      && request.model.length <= 256;
  }

  // Pure presentation strings, kept in the leaf so app.js only assigns them
  // to DOM nodes and the wording stays with the state that produces it.
  function describeRequest(request) {
    const model = request.model === "any" ? "不限型号" : request.model;
    return `${request.gpuCount} 张 GPU · ${model} · 每卡空闲 ≥ ${request.minVramGiB} GiB`;
  }

  function controlText(watch, satisfiedCount) {
    if (watch.state === "notified") {
      return `已就绪 · ${satisfiedCount} 个节点满足 ${describeRequest(watch.request)}`;
    }
    return `守望中 · ${describeRequest(watch.request)}`;
  }

  function bannerText(watch, satisfiedCount) {
    return `GPU 已就绪：${satisfiedCount} 个节点满足 ${describeRequest(watch.request)}`;
  }

  function create({ storage, now = () => Date.now() } = {}) {
    function parseWatch(raw) {
      const stored = JSON.parse(raw || "null");
      if (
        stored == null
        || typeof stored !== "object"
        || stored.version !== 1
        || !validRequest(stored.request)
        || (stored.state !== "armed" && stored.state !== "notified")
        || (stored.lastNotifiedAt !== null && !Number.isFinite(stored.lastNotifiedAt))
      ) return null;
      return {
        version: 1,
        request: {
          gpuCount: stored.request.gpuCount,
          minVramGiB: stored.request.minVramGiB,
          model: stored.request.model,
        },
        state: stored.state,
        lastNotifiedAt: stored.lastNotifiedAt,
      };
    }

    // Distinguish "storage says no watch" from "storage is unavailable": only
    // the former lets another tab authoritatively clear this one, while the
    // latter must fall back to the in-memory watch.
    function readStored() {
      let raw;
      try {
        raw = storage.getItem(STORAGE_KEY);
      } catch (_error) {
        return { readable: false, watch: null };
      }
      try {
        return { readable: true, watch: parseWatch(raw) };
      } catch (_error) {
        return { readable: true, watch: null };
      }
    }

    function loadWatch() {
      return readStored().watch;
    }

    function sameRequest(first, second) {
      return (
        first.gpuCount === second.gpuCount
        && first.minVramGiB === second.minVramGiB
        && first.model === second.model
      );
    }

    function persist(watch) {
      try {
        storage.setItem(STORAGE_KEY, JSON.stringify(watch));
      } catch (_error) {
        // The in-memory watch still works for this document; it simply does
        // not survive a reload when storage is unavailable.
      }
      return watch;
    }

    function saveWatch(request) {
      if (!validRequest(request)) return null;
      return persist({
        version: 1,
        request: { ...request },
        state: "armed",
        lastNotifiedAt: null,
      });
    }

    function clearWatch() {
      try {
        storage.removeItem(STORAGE_KEY);
      } catch (_error) {
        // Nothing durable to remove in this mode.
      }
    }

    // One notification per satisfaction edge: armed -> notified fires once,
    // and the watch re-arms only after demand stops being satisfied. The
    // cooldown keeps a flapping fleet from turning edges into notification
    // spam; a rate-limited edge stays armed and retries on a later snapshot.
    function evaluateWatch(watch, satisfiedCount) {
      // Reconcile with shared storage first so a persisted transition can
      // never resurrect a watch another tab stopped, nor overwrite a newer
      // request another tab saved. When storage is unreadable there is no
      // sharing, so the in-memory watch stays authoritative.
      const current = readStored();
      if (current.readable) {
        if (current.watch === null || !sameRequest(current.watch.request, watch.request)) {
          return { watch: current.watch, shouldNotify: false };
        }
        watch = current.watch;
      }
      if (watch.state === "armed" && satisfiedCount > 0) {
        const at = now();
        // A wall-clock rollback (NTP step, manual change) makes the elapsed
        // difference negative; treat that as "cooldown elapsed" so the watch
        // is not frozen until the clock catches back up to lastNotifiedAt.
        const withinCooldown =
          watch.lastNotifiedAt !== null
          && at >= watch.lastNotifiedAt
          && at - watch.lastNotifiedAt < NOTIFY_COOLDOWN_MS;
        if (withinCooldown) {
          return { watch, shouldNotify: false };
        }
        return {
          watch: persist({ ...watch, state: "notified", lastNotifiedAt: at }),
          shouldNotify: true,
        };
      }
      if (watch.state === "notified" && satisfiedCount === 0) {
        return { watch: persist({ ...watch, state: "armed" }), shouldNotify: false };
      }
      return { watch, shouldNotify: false };
    }

    return Object.freeze({
      describeRequest,
      controlText,
      bannerText,
      loadWatch,
      saveWatch,
      clearWatch,
      evaluateWatch,
    });
  }

  globalThis.MocopCapacityWatch = Object.freeze({ create });
})();
