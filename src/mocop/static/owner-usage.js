// The owners dialog's two projections, extracted from app.js under the
// ADR-0021 leaf pattern: the current per-owner aggregation over the online
// part of the snapshot, and the wording of the usage bill that /api/usage
// returns (GPU-hours, the coverage caveats, per-owner kinds and idle share).
// Pure: app.js injects the formatters and owns every DOM node.
(() => {
  "use strict";

  const UNATTRIBUTED_KEY = "\u0000unattributed";

  function create({ format, numeric, memory, age, workloadLabels }) {
    // Current owners from the snapshot. Hosts that are not online are
    // skipped and counted, because their last-success processes may already
    // be gone; VRAM is a per-card sum while one PID spanning several GPUs of
    // a host counts as one process.
    function currentOwners(snapshot) {
      const owners = new Map();
      const hostLabels = new Map();
      let excludedOfflineHosts = 0;
      let oldestObservedAt = "";
      let oldestObservedMs = Infinity;
      for (const server of snapshot.servers) {
        hostLabels.set(server.host, server.displayName || server.host);
        if (server.status !== "online") {
          if (server.gpus.some((gpu) => (gpu.processes || []).length > 0)) {
            excludedOfflineHosts += 1;
          }
          continue;
        }
        for (const gpu of server.gpus) {
          const processes = gpu.processes || [];
          const observedMs = processes.length && gpu.processes_observed_at
            ? Date.parse(gpu.processes_observed_at) : NaN;
          if (observedMs < oldestObservedMs) {
            oldestObservedMs = observedMs;
            oldestObservedAt = gpu.processes_observed_at;
          }
          for (const process of processes) {
            const owner = process.workload?.owner;
            const key = owner || UNATTRIBUTED_KEY;
            let entry = owners.get(key);
            if (!entry) {
              entry = {
                label: owner || "未归属",
                attributed: Boolean(owner),
                vramMiB: 0,
                unknownVramKeys: new Set(),
                processKeys: new Set(),
                gpus: new Set(),
                hosts: new Set(),
                kinds: new Set(),
              };
              owners.set(key, entry);
            }
            const processKey = `${server.host}\u0000${process.pid}`;
            const usedMemory = process.used_memory_mib == null
              ? NaN : numeric(process.used_memory_mib, NaN);
            if (Number.isFinite(usedMemory)) entry.vramMiB += usedMemory;
            else entry.unknownVramKeys.add(processKey);
            entry.processKeys.add(processKey);
            entry.gpus.add(`${server.host}\u0000${gpu.uuid || gpu.index}`);
            entry.hosts.add(server.host);
            const kind = process.workload?.kind;
            if (kind && kind !== "process") entry.kinds.add(kind);
          }
        }
      }
      const ranked = [...owners.values()].sort(
        (first, second) => second.vramMiB - first.vramMiB
          || second.gpus.size - first.gpus.size
          || first.label.localeCompare(second.label),
      );
      return { ranked, excludedOfflineHosts, oldestObservedAt, hostLabels };
    }

    function currentSummary({ ranked, visibleCount, oldestObservedAt }) {
      const totalVram = ranked.reduce((sum, entry) => sum + entry.vramMiB, 0);
      const hasUnknownVram = ranked.some((entry) => entry.unknownVramKeys.size > 0);
      const attributedCount = ranked.filter((entry) => entry.attributed).length;
      return `${ranked.length} 个归属方 · 共占用${hasUnknownVram ? "至少 " : " "}${memory(totalVram)}`
        + (attributedCount < ranked.length ? " · 含未归属进程" : "")
        + (hasUnknownVram ? " · 部分进程显存未知" : "")
        + (ranked.length > visibleCount ? ` · 仅展示前 ${visibleCount} 项` : "")
        + (oldestObservedAt ? ` · 数据截至 ${age(oldestObservedAt)}` : "");
    }

    function gpuHoursLabel(seconds) {
      const value = numeric(seconds);
      if (value < 60) return `${Math.round(value)} 卡·秒`;
      if (value < 5400) return `${Math.round(value / 60)} 卡·分`;
      return `${format(value / 3600, 1)} 卡·时`;
    }

    // The bill's headline. Retention that starts inside the window and
    // devices whose full timeline begins inside it are both caveats the
    // reader needs before comparing owners.
    function usageSummary(usage) {
      const history = usage.source === "history";
      // The history report's coverage is bounded by retention and stated as
      // coveredFromAt; the in-memory rollup's by the earliest retained record.
      const coverageFrom = history ? usage.coveredFromAt : usage.earliestDataAt;
      const coverageGap = coverageFrom && usage.sinceAt
        && Date.parse(coverageFrom) > Date.parse(usage.sinceAt) + 60_000;
      return `${numeric(usage.totalOwners)} 个归属方 · 共 ${gpuHoursLabel(usage.totalGpuSeconds)}`
        + (history ? " · 历史库统计" : "")
        + (coverageGap ? ` · 数据自 ${age(coverageFrom)}起` : "")
        + (usage.partialGpus > 0 ? ` · ${usage.partialGpus} 张卡的时间线不完整` : "");
    }

    // The history report's per-day split, oldest first, as chip labels; the
    // in-memory rollup has none.
    function usageDays(usage) {
      if (!Array.isArray(usage.days)) return [];
      return usage.days
        .filter((entry) => entry && typeof entry.day === "string" && Number.isFinite(entry.gpuSeconds))
        .map((entry) => ({ day: entry.day.slice(5), label: gpuHoursLabel(entry.gpuSeconds) }));
    }

    function usageKinds(entry) {
      return entry.kinds && typeof entry.kinds === "object"
        ? Object.keys(entry.kinds)
          .filter((kind) => kind !== "process")
          .map((kind) => workloadLabels[kind] || kind)
        : [];
    }

    function idleShare(entry) {
      return entry.idleShare == null ? NaN : numeric(entry.idleShare, NaN);
    }

    return Object.freeze({
      currentOwners,
      currentSummary,
      gpuHoursLabel,
      usageSummary,
      usageDays,
      usageKinds,
      idleShare,
    });
  }

  globalThis.MocopOwnerUsage = Object.freeze({ create });
})();
