// USE check for one host: utilization, saturation, and errors for every
// resource the probe samples (Brendan Gregg's USE method). Pure projection of a
// snapshot server record against the snapshot thresholds; no DOM, no network.
// A cell the probe cannot observe reads NOT_COLLECTED instead of a guess.
(() => {
  "use strict";

  const NOT_COLLECTED = "—";
  const DISK_CRITICAL_PCT = 95;

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function create({ format, rate }) {
    const percent = (value) => (finite(value) ? `${format(value)}%` : NOT_COLLECTED);
    const level = (value, warning, critical = Infinity) => {
      if (!finite(value)) return "";
      if (value >= critical) return "critical";
      return value >= warning ? "warning" : "";
    };
    const cell = (text, state = "") => Object.freeze({ text, state });
    const worst = (...states) => (
      states.includes("critical") ? "critical" : states.includes("warning") ? "warning" : ""
    );

    function cpuRow(system, thresholds) {
      const perCore = finite(system.load_1m) && system.cpu_cores > 0
        ? system.load_1m / system.cpu_cores : null;
      const psi = system.pressure?.cpu?.some_avg10;
      return {
        resource: "CPU",
        cells: [
          cell(percent(system.cpu_usage_pct), level(system.cpu_usage_pct, thresholds.cpu_warning_pct)),
          // More runnable threads than cores means work is queueing.
          cell(`负载/核 ${finite(perCore) ? perCore.toFixed(2) : NOT_COLLECTED} · PSI ${percent(psi)}`, level(perCore, 1)),
          cell(NOT_COLLECTED),
        ],
      };
    }

    function memoryRow(system, thresholds) {
      const used = system.memory_total_mib > 0
        ? (system.memory_used_mib / system.memory_total_mib) * 100 : null;
      const swap = system.swap_total_mib > 0
        ? (system.swap_used_mib / system.swap_total_mib) * 100 : null;
      const psi = system.pressure?.memory?.some_avg10;
      return {
        resource: "内存",
        cells: [
          cell(percent(used), level(used, thresholds.memory_warning_pct)),
          cell(
            `Swap ${swap == null ? "未配置" : percent(swap)} · PSI ${percent(psi)}`,
            worst(level(swap, thresholds.swap_warning_pct), level(psi, thresholds.psi_memory_some_pct)),
          ),
          cell(NOT_COLLECTED),
        ],
      };
    }

    function diskRow(system, thresholds) {
      const fills = (system.disks || []).map((disk) => disk.used_pct).filter(finite);
      const fullest = fills.length ? Math.max(...fills) : null;
      const psi = system.pressure?.io?.some_avg10;
      return {
        resource: "磁盘",
        cells: [
          cell(
            finite(fullest) ? `最满 ${format(fullest)}%` : NOT_COLLECTED,
            level(fullest, thresholds.disk_warning_pct, DISK_CRITICAL_PCT),
          ),
          cell(`PSI I/O ${percent(psi)}`, level(psi, thresholds.psi_io_some_pct)),
          cell(NOT_COLLECTED),
        ],
      };
    }

    function networkRow(system) {
      const sampled = system.network_rx_bps != null || system.network_tx_bps != null;
      return {
        resource: "网络",
        cells: [
          cell(sampled ? `↓ ${rate(system.network_rx_bps)} ↑ ${rate(system.network_tx_bps)}` : NOT_COLLECTED),
          cell(NOT_COLLECTED),
          cell(NOT_COLLECTED),
        ],
      };
    }

    function gpuRow(gpus) {
      const average = (key) => {
        const values = gpus.map((gpu) => gpu[key]).filter(finite);
        return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
      };
      const slowed = gpus.filter((gpu) => gpu.health?.thermal_slowdown || gpu.health?.power_brake_slowdown).length;
      const faulty = gpus.filter((gpu) => (
        gpu.health?.ecc_uncorrected_volatile > 0
        || gpu.health?.remapped_rows_pending
        || gpu.health?.retired_pages_pending
      )).length;
      const healthKnown = gpus.some((gpu) => gpu.health);
      const bandwidth = average("utilization_memory_pct");
      return {
        resource: "GPU",
        cells: [
          cell(finite(average("utilization_gpu_pct")) ? `平均 ${percent(average("utilization_gpu_pct"))}` : NOT_COLLECTED),
          cell(
            `显存带宽 ${percent(bandwidth)}${slowed ? ` · ${slowed} 张降频` : ""}`,
            slowed ? "warning" : "",
          ),
          cell(healthKnown ? (faulty ? `${faulty} 张` : "无") : NOT_COLLECTED, faulty ? "critical" : ""),
        ],
      };
    }

    function rows(server, thresholds) {
      if (!server?.system) return [];
      const system = server.system;
      const result = [
        cpuRow(system, thresholds),
        memoryRow(system, thresholds),
        diskRow(system, thresholds),
        networkRow(system),
      ];
      if (server.gpus?.length) result.push(gpuRow(server.gpus));
      return result.map((row) => Object.freeze({ ...row, cells: Object.freeze(row.cells) }));
    }

    return Object.freeze({ NOT_COLLECTED, rows });
  }

  globalThis.MocopUseCheck = Object.freeze({ create });
})();
