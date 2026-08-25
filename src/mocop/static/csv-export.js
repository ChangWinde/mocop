// GPU inventory CSV projection, extracted from app.js under the ADR-0021
// leaf pattern. The leaf owns cell escaping (including spreadsheet formula
// injection defense) and row building; app.js keeps record selection, the
// download anchor, and object-URL lifecycle.
(() => {
  "use strict";

  function csvCell(value) {
    const raw = value == null ? "" : String(value);
    const safe = /^[\s\u0000-\u001F]*[=+\-@]/.test(raw) ? `'${raw}` : raw;
    return `"${safe.replaceAll('"', '""')}"`;
  }

  function create({ ratio, gpuProcessSummary, gpuState }) {
    function buildCsv(records) {
      const columns = [
        ["主机", ({ server }) => server.host],
        ["GPU Index", ({ gpu }) => gpu.index],
        ["UUID", ({ gpu }) => gpu.uuid],
        ["型号", ({ gpu }) => gpu.name],
        ["驱动", ({ gpu }) => gpu.driver_version],
        ["P-State", ({ gpu }) => gpu.pstate],
        ["GPU 利用率 %", ({ gpu }) => gpu.utilization_gpu_pct],
        ["显存利用率 %", ({ gpu }) => ratio(gpu.memory_used_mib, gpu.memory_total_mib).toFixed(2)],
        ["显存已用 MiB", ({ gpu }) => gpu.memory_used_mib],
        ["显存总量 MiB", ({ gpu }) => gpu.memory_total_mib],
        ["温度 °C", ({ gpu }) => gpu.temperature_c],
        ["功耗 W", ({ gpu }) => gpu.power_draw_w],
        ["功耗上限 W", ({ gpu }) => gpu.power_limit_w],
        ["进程数", ({ gpu }) => gpu.processes_available === false
          ? null : gpuProcessSummary(gpu).count],
        ["进程已知分配显存 MiB", ({ gpu }) => {
          const summary = gpuProcessSummary(gpu);
          return gpu.processes_available === false || !summary.knownMemoryCount
            ? null : summary.knownMemoryMiB;
        }],
        ["进程显存覆盖", ({ gpu }) => {
          const summary = gpuProcessSummary(gpu);
          return gpu.processes_available === false
            ? null : `${summary.knownMemoryCount}/${summary.count}`;
        }],
        ["进程采样状态", ({ gpu }) => gpu.processes_available === false
          ? "unavailable" : gpu.processes_sampled === false ? "cached" : "sampled"],
        ["进程采样时间", ({ gpu }) => gpu.processes_observed_at],
        ["CPU 利用率 %", ({ server }) => server.system?.cpu_usage_pct],
        ["系统内存利用率 %", ({ server }) => ratio(server.system?.memory_used_mib, server.system?.memory_total_mib).toFixed(2)],
        ["GPU 状态", ({ server, gpu }) => gpuState(gpu, server)[0]],
        ["采样时间", ({ server }) => server.lastAttemptAt],
      ];
      const lines = [columns.map(([label]) => csvCell(label)).join(",")];
      records.forEach((record) => {
        lines.push(columns.map(([, getter]) => csvCell(getter(record))).join(","));
      });
      return `\uFEFF${lines.join("\r\n")}\r\n`;
    }

    return Object.freeze({ buildCsv, csvCell });
  }

  globalThis.MocopCsvExport = Object.freeze({ create });
})();
