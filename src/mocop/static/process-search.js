"use strict";

// A dependency-free leaf module for the process-search projection. Keeping the
// bounded ranking algorithm outside app.js makes its input/output contract
// independently testable without introducing a browser build step.
((root) => {
  function createProcessSearch({
    maxResults,
    maxQueryLength,
    workloadLabels,
    processName,
    numeric,
  }) {
    if (!Number.isInteger(maxResults) || maxResults < 1) {
      throw new RangeError("maxResults must be a positive integer");
    }
    if (!Number.isInteger(maxQueryLength) || maxQueryLength < 1) {
      throw new RangeError("maxQueryLength must be a positive integer");
    }
    if (!workloadLabels || typeof workloadLabels !== "object") {
      throw new TypeError("workloadLabels must be an object");
    }
    if (typeof processName !== "function" || typeof numeric !== "function") {
      throw new TypeError("processName and numeric must be functions");
    }

    const processProjectionCache = new WeakMap();
    const gpuProjectionCache = new WeakMap();

    function normalizedSearchTerms(value) {
      const normalized = String(value ?? "")
        .slice(0, maxQueryLength)
        .normalize("NFKC")
        .toLowerCase()
        .trim();
      return normalized ? normalized.split(/\s+/u) : [];
    }

    function normalizedSearchProjection(values) {
      const text = values
        .filter((value) => value != null && String(value) !== "")
        .map(String)
        .join("\u0000")
        .normalize("NFKC")
        .toLowerCase();
      return { text, boundaryText: `\u0000${text}\u0000` };
    }

    function processSearchProjection(process) {
      const cached = processProjectionCache.get(process);
      if (cached) return cached;
      const workload = process.workload || {};
      const projection = normalizedSearchProjection([
        process.pid,
        process.name,
        processName(process),
        workload.kind,
        Object.hasOwn(workloadLabels, workload.kind)
          ? workloadLabels[workload.kind] : null,
        workload.workload_id,
        workload.name,
        workload.owner,
        workload.queue,
        workload.namespace,
        workload.command,
      ]);
      processProjectionCache.set(process, projection);
      return projection;
    }

    function gpuSearchProjection(server, gpu) {
      const cached = gpuProjectionCache.get(gpu);
      if (cached) return cached;
      const projection = normalizedSearchProjection([
        server.host, gpu.index, gpu.uuid, gpu.name,
      ]);
      gpuProjectionCache.set(gpu, projection);
      return projection;
    }

    function processMatchesSearch(process, terms, server = null, gpu = null) {
      if (!terms.length) return true;
      const processText = processSearchProjection(process).text;
      const placementText = server && gpu
        ? gpuSearchProjection(server, gpu).text : "";
      return terms.every(
        (term) => processText.includes(term) || placementText.includes(term),
      );
    }

    function processMemoryRank(a, b) {
      return numeric(b.used_memory_mib, -1) - numeric(a.used_memory_mib, -1)
        || numeric(a.pid) - numeric(b.pid);
    }

    function processSearchRank(record, terms) {
      const query = terms.join(" ");
      const projections = [
        gpuSearchProjection(record.server, record.gpu),
        processSearchProjection(record.process),
      ];
      if (projections.some(
        (projection) => projection.boundaryText.includes(`\u0000${query}\u0000`),
      )) return 0;
      if (projections.some(
        (projection) => projection.boundaryText.includes(`\u0000${query}`),
      )) return 1;
      return 2;
    }

    function compareProcessSearchRecords(a, b) {
      return a.rank - b.rank
        || a.server.host.localeCompare(b.server.host)
        || numeric(a.gpu.index) - numeric(b.gpu.index)
        || processMemoryRank(a.process, b.process)
        || String(a.process.name || "").localeCompare(String(b.process.name || ""));
    }

    // A max-heap keeps the worst retained match at index zero. Search can
    // report the full count while retaining only the configured render budget.
    function retainProcessSearchRecord(heap, record) {
      if (heap.length < maxResults) {
        heap.push(record);
        let index = heap.length - 1;
        while (index > 0) {
          const parent = Math.floor((index - 1) / 2);
          if (compareProcessSearchRecords(heap[parent], heap[index]) >= 0) break;
          [heap[parent], heap[index]] = [heap[index], heap[parent]];
          index = parent;
        }
        return;
      }
      if (compareProcessSearchRecords(record, heap[0]) >= 0) return;
      heap[0] = record;
      let index = 0;
      for (;;) {
        const left = index * 2 + 1;
        if (left >= heap.length) break;
        const right = left + 1;
        const worse = right < heap.length
          && compareProcessSearchRecords(heap[right], heap[left]) > 0 ? right : left;
        if (compareProcessSearchRecords(heap[index], heap[worse]) >= 0) break;
        [heap[index], heap[worse]] = [heap[worse], heap[index]];
        index = worse;
      }
    }

    function searchProcessRecords(snapshot, query, host = "all") {
      const terms = normalizedSearchTerms(query);
      if (!snapshot || !terms.length) {
        return { matches: [], total: 0, unavailableGpuCount: 0, staleCount: 0 };
      }
      const matches = [];
      let total = 0;
      let unavailableGpuCount = 0;
      let staleCount = 0;
      snapshot.servers.forEach((server) => {
        if (host !== "all" && server.host !== host) return;
        server.gpus.forEach((gpu) => {
          if (gpu.processes_available === false) unavailableGpuCount += 1;
          const processes = Array.isArray(gpu.processes) ? gpu.processes : [];
          processes.forEach((process) => {
            if (!processMatchesSearch(process, terms, server, gpu)) return;
            total += 1;
            if (server.stale) staleCount += 1;
            const record = { server, gpu, process, rank: 0 };
            record.rank = processSearchRank(record, terms);
            retainProcessSearchRecord(matches, record);
          });
        });
      });
      matches.sort(compareProcessSearchRecords);
      return { matches, total, unavailableGpuCount, staleCount };
    }

    function gpuRecordMatchesSearch(server, gpu, terms) {
      if (!terms.length) return true;
      const resourceText = gpuSearchProjection(server, gpu).text;
      if (terms.every((term) => resourceText.includes(term))) return true;
      const processes = Array.isArray(gpu.processes) ? gpu.processes : [];
      return processes.some(
        (process) => processMatchesSearch(process, terms, server, gpu),
      );
    }

    return Object.freeze({
      compareProcessSearchRecords,
      gpuRecordMatchesSearch,
      normalizedSearchTerms,
      processMatchesSearch,
      processMemoryRank,
      processSearchRank,
      searchProcessRecords,
    });
  }

  root.MocopProcessSearch = Object.freeze({ create: createProcessSearch });
})(globalThis);
