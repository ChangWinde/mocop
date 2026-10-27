"use strict";

// The selected-server inventory projection and its empty-state wording stay
// separate from the query matcher so both views agree on what is observable.
((root) => {
  function createProcessInventory({ maxResults, numeric }) {
    if (!Number.isInteger(maxResults) || maxResults < 1) {
      throw new RangeError("maxResults must be a positive integer");
    }
    if (typeof numeric !== "function") throw new TypeError("numeric must be a function");

    function processMemoryRank(a, b) {
      return numeric(b.used_memory_mib, -1) - numeric(a.used_memory_mib, -1)
        || numeric(a.pid) - numeric(b.pid);
    }

    function listProcessRecords(snapshot, host = "all") {
      if (!snapshot) return { matches: [], total: 0, unavailableGpuCount: 0, staleCount: 0 };
      const matches = [];
      let unavailableGpuCount = 0;
      let staleCount = 0;
      snapshot.servers.forEach((server) => {
        if (host !== "all" && server.host !== host) return;
        server.gpus.forEach((gpu) => {
          if (gpu.processes_available === false) {
            unavailableGpuCount += 1;
            return;
          }
          (Array.isArray(gpu.processes) ? gpu.processes : []).forEach((process) => {
            if (server.stale) staleCount += 1;
            matches.push({ server, gpu, process, rank: 0 });
          });
        });
      });
      matches.sort((a, b) => processMemoryRank(a.process, b.process)
        || String(a.server.host).localeCompare(String(b.server.host))
        || Number(a.gpu.index) - Number(b.gpu.index)
        || Number(a.process.pid) - Number(b.process.pid));
      return { matches: matches.slice(0, maxResults), total: matches.length,
        unavailableGpuCount, staleCount };
    }

    function panelCopy(scope, serverProcessMode, result) {
      const notes = [];
      if (!result.total) {
        notes.push(serverProcessMode
          ? `${scope}没有可见的活跃 GPU 进程`
          : `${scope}没有匹配的活跃程序`);
      } else if (result.total > result.matches.length) {
        notes.push(serverProcessMode
          ? `共 ${result.total} 条活跃进程，显示显存占用最高的 ${result.matches.length} 条`
          : `找到 ${result.total} 条匹配记录，显示相关度最高的 ${result.matches.length} 条`);
      } else {
        notes.push(serverProcessMode
          ? `${scope}当前有 ${result.total} 条活跃 GPU 进程`
          : `找到 ${result.total} 条匹配的 GPU 进程记录`);
      }
      if (result.staleCount) notes.push(`含 ${result.staleCount} 条历史样本`);
      if (result.unavailableGpuCount) {
        notes.push(`${result.unavailableGpuCount} 张 GPU 的任务数据不可用`);
      }
      const empty = serverProcessMode && result.unavailableGpuCount
        ? "当前 GPU 的任务数据不可用，恢复采样后会显示进程"
        : serverProcessMode
          ? "当前服务器没有可见的活跃 GPU 进程"
          : "换一个名称、PID、用户或 workload 关键词重试";
      return { summary: notes.join(" · "), empty };
    }

    function sampleLabel(server, gpu) {
      return server.stale ? "历史样本"
        : gpu.processes_sampled === false ? "复用样本" : "当前样本";
    }

    function sampleIsStale(server, gpu) {
      return server.stale || gpu.processes_sampled === false;
    }

    function detailSampleLabel(server, gpu) {
      if (server.stale) return "GPU 历史数据";
      if (gpu.processes_available === false) return "GPU 当前 · 进程不可用";
      return gpu.processes_sampled === false ? "GPU 当前 · 进程复用" : "GPU 与进程当前";
    }

    return Object.freeze({
      detailSampleLabel,
      listProcessRecords,
      panelCopy,
      sampleIsStale,
      sampleLabel,
    });
  }

  root.MocopProcessInventory = Object.freeze({ create: createProcessInventory });
})(globalThis);
