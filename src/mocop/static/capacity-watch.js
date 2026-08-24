// Capacity matching and the capacity watch state machine, extracted from
// app.js under the ADR-0021 leaf pattern. The leaf owns snapshot-projection
// logic and durable watch state; app.js keeps DOM rendering, notification
// delivery, and dashboard lifecycle. No network access happens here.
(() => {
  "use strict";

  const STORAGE_KEY = "mocop.capacityWatch.v1";
  const NOTIFY_COOLDOWN_MS = 60_000;
  const HOST_BLOCKERS = Object.freeze(
    new Set(["connectivity", "gpu_availability", "gpu_count"]),
  );
  const GPU_BLOCKERS = Object.freeze(
    new Set(["gpu_ecc", "gpu_memory_repair", "gpu_slowdown", "gpu_temperature"]),
  );

  function numeric(value, fallback = 0) {
    return Number.isFinite(value) ? value : fallback;
  }

  function optionalMetric(record, key) {
    const value = record?.[key];
    return Number.isFinite(value) ? value : Number.NaN;
  }

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
      && request.model.length <= 120;
  }

  function create({ storage, now = () => Date.now() } = {}) {
    function gpuHasBlocker(gpu, conditions) {
      const identity = String(gpu.uuid || gpu.index);
      const resourcePrefix = `GPU ${gpu.index}`;
      return conditions.some((condition) => {
        if (!GPU_BLOCKERS.has(condition.category)) return false;
        const key = String(condition.conditionKey || "");
        const resource = String(condition.resource || "");
        return key.endsWith(`:${identity}`)
          || resource === resourcePrefix
          || resource.startsWith(`${resourcePrefix} `);
      });
    }

    // Pure projection of the current snapshot: same-host, same-model groups
    // ranked by fit. Maintenance hosts, host-level blockers, and GPUs with
    // hardware alerts never become candidates.
    function matches({ servers, activeConditions, request, busyPct, temperatureC }) {
      const minimumFreeMiB = request.minVramGiB * 1024;
      const conditionsByHost = new Map();
      (Array.isArray(activeConditions) ? activeConditions : []).forEach((condition) => {
        const group = conditionsByHost.get(condition.host) || [];
        group.push(condition);
        conditionsByHost.set(condition.host, group);
      });
      const candidates = [];
      let excludedMaintenance = 0;
      let excludedHealth = 0;

      servers.forEach((server) => {
        if (server.status !== "online" || server.stale) return;
        if (server.maintenance) {
          excludedMaintenance += 1;
          return;
        }
        const conditions = conditionsByHost.get(server.host) || [];
        if (conditions.some((condition) => HOST_BLOCKERS.has(condition.category))) {
          excludedHealth += 1;
          return;
        }
        const groups = new Map();
        server.gpus.forEach((gpu) => {
          const model = gpu.name || "Unknown NVIDIA GPU";
          if (request.model !== "any" && model !== request.model) return;
          const group = groups.get(model) || [];
          group.push(gpu);
          groups.set(model, group);
        });
        groups.forEach((gpus, model) => {
          const available = gpus.filter((gpu) => {
            const utilization = optionalMetric(gpu, "utilization_gpu_pct");
            const freeMemory = optionalMetric(gpu, "memory_free_mib");
            const temperature = optionalMetric(gpu, "temperature_c");
            return Number.isFinite(utilization)
              && utilization < busyPct
              && Number.isFinite(freeMemory)
              && freeMemory >= minimumFreeMiB
              && (!Number.isFinite(temperature) || temperature < temperatureC)
              && !gpuHasBlocker(gpu, conditions);
          });
          const freeValues = available.map((gpu) => numeric(gpu.memory_free_mib));
          const utilizationValues = available.map((gpu) => numeric(gpu.utilization_gpu_pct));
          candidates.push({
            host: server.host,
            model,
            total: gpus.length,
            available,
            satisfies: available.length >= request.gpuCount,
            deficit: Math.max(0, request.gpuCount - available.length),
            minimumFreeMiB: freeValues.length ? Math.min(...freeValues) : 0,
            averageUtilization: utilizationValues.length
              ? utilizationValues.reduce((sum, value) => sum + value, 0) / utilizationValues.length
              : 101,
            cpuUsage: optionalMetric(server.system || {}, "cpu_usage_pct"),
          });
        });
      });
      candidates.sort((first, second) => (
        Number(second.satisfies) - Number(first.satisfies)
        || first.deficit - second.deficit
        || second.available.length - first.available.length
        || second.minimumFreeMiB - first.minimumFreeMiB
        || first.averageUtilization - second.averageUtilization
        || first.host.localeCompare(second.host)
      ));
      return { candidates, excludedMaintenance, excludedHealth };
    }

    function loadWatch() {
      try {
        const stored = JSON.parse(storage.getItem(STORAGE_KEY) || "null");
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
      } catch (_error) {
        return null;
      }
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
      if (watch.state === "armed" && satisfiedCount > 0) {
        const at = now();
        if (watch.lastNotifiedAt !== null && at - watch.lastNotifiedAt < NOTIFY_COOLDOWN_MS) {
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
      hostBlockerCategories: HOST_BLOCKERS,
      gpuBlockerCategories: GPU_BLOCKERS,
      gpuHasBlocker,
      matches,
      loadWatch,
      saveWatch,
      clearWatch,
      evaluateWatch,
    });
  }

  globalThis.MocopCapacityWatch = Object.freeze({ create });
})();
