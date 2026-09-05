// Capacity matching: a pure projection of the current snapshot into ranked
// same-host, same-model GPU candidates. Extracted from capacity-watch.js
// under the ADR-0021 leaf pattern so the stateless projection and the durable
// watch state machine stay in separate cohesive files. No DOM, no storage,
// no network. mocop/capacity.py is the server-side twin behind
// GET /api/capacity; tests/fixtures/capacity_match.json pins both to one
// ranking, so change the fixture and both implementations together.
(() => {
  "use strict";

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

  function create() {
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

    // Same-host, same-model groups ranked by fit. Maintenance hosts,
    // host-level blockers, and GPUs with hardware alerts never become
    // candidates.
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
      // Code-point host order (not localeCompare) keeps the ranking identical
      // to the server-side twin in mocop/capacity.py on every locale.
      candidates.sort((first, second) => (
        Number(second.satisfies) - Number(first.satisfies)
        || first.deficit - second.deficit
        || second.available.length - first.available.length
        || second.minimumFreeMiB - first.minimumFreeMiB
        || first.averageUtilization - second.averageUtilization
        || (first.host < second.host ? -1 : first.host > second.host ? 1 : 0)
      ));
      return { candidates, excludedMaintenance, excludedHealth };
    }

    return Object.freeze({ matches });
  }

  globalThis.MocopCapacityMatch = Object.freeze({ create });
})();
