// Self-check: evidence that what the dashboard shows is right, from referees
// other than the dashboard itself. Every headline number the browser derives
// is reconciled with the aggregate the server computed independently
// (snapshot stats, GET /api/capacity), and text colors are measured against
// WCAG 2.1 AA. Pure functions; the caller supplies inputs it read from the
// snapshot, the capacity API, and computed styles.
(() => {
  "use strict";

  const WCAG_AA_TEXT = 4.5;

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  // Computed colors come back as rgb()/rgba() or, for color-mix() results,
  // as color(srgb r g b / a) with 0-1 channels. Anything else is unreadable.
  function parseColor(text) {
    const value = String(text || "").trim();
    let match = value.match(/^color\(srgb ([\d.e-]+) ([\d.e-]+) ([\d.e-]+)(?: \/ ([\d.e-]+))?\)$/);
    if (match) return [match[1] * 255, match[2] * 255, match[3] * 255, match[4] == null ? 1 : Number(match[4])];
    match = value.match(/^rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)$/);
    if (match) return [Number(match[1]), Number(match[2]), Number(match[3]), match[4] == null ? 1 : Number(match[4])];
    return null;
  }

  function composite(foreground, background) {
    const alpha = foreground[3];
    return [0, 1, 2].map((index) => foreground[index] * alpha + background[index] * (1 - alpha)).concat(1);
  }

  function luminance(color) {
    const channel = (value) => {
      const share = value / 255;
      return share <= 0.03928 ? share / 12.92 : ((share + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2]);
  }

  // Contrast of text over a surface that may itself be translucent over the page.
  function contrastRatio(text, surface, page) {
    const base = composite(surface, page);
    const ink = composite(text, base);
    const [lighter, darker] = [luminance(ink), luminance(base)].sort((a, b) => b - a);
    return (lighter + 0.05) / (darker + 0.05);
  }

  function create({ fleetState }) {
    const row = (id, label, expected, actual, ok, source) => Object.freeze({ id, label, expected, actual, ok, source });
    const exact = (id, label, expected, actual, source) => row(id, label, expected, actual, expected === actual, source);
    const near = (id, label, expected, actual, tolerance, source) => row(
      id, label, expected, actual, finite(expected) && finite(actual) && Math.abs(expected - actual) <= tolerance, source,
    );

    function capacityTotals(result) {
      const candidates = result?.candidates || [];
      return {
        gpus: candidates.reduce((sum, candidate) => sum + candidate.available.length, 0),
        hosts: candidates.filter((candidate) => candidate.available.length).length,
      };
    }

    // Each row recomputes a number in the browser and compares it with the
    // server's own aggregate over the same snapshot.
    function reconcile({ snapshot, activeIncidents, browserCapacity, serverCapacity }) {
      const { stats, servers, thresholds } = snapshot;
      const online = servers.filter((server) => server.status === "online");
      const onlineGpus = online.flatMap((server) => server.gpus);
      const counts = fleetState.breakdown(servers, thresholds.gpu_busy_pct);
      const cpuValues = online.map((server) => server.system?.cpu_usage_pct).filter(finite);
      const cpuMean = cpuValues.length ? cpuValues.reduce((sum, value) => sum + value, 0) / cpuValues.length : null;
      const rows = [
        exact("gpus", "在线 GPU", stats.gpus, counts.online, "stats.gpus"),
        exact("busy-gpus", "计算中 GPU", stats.busyGpus, counts.compute, "stats.busyGpus"),
        exact("online-servers", "在线机器", stats.onlineServers, online.length, "stats.onlineServers"),
        near(
          "gpu-memory", "显存已用 MiB", stats.memoryUsedMiB,
          onlineGpus.reduce((sum, gpu) => sum + (gpu.memory_used_mib || 0), 0), 1, "stats.memoryUsedMiB",
        ),
        stats.cpuAveragePct == null && cpuMean == null
          ? row("cpu", "集群 CPU 平均 %", null, null, true, "stats.cpuAveragePct")
          : near("cpu", "集群 CPU 平均 %", stats.cpuAveragePct, cpuMean, 0.01, "stats.cpuAveragePct"),
      ];
      if (Array.isArray(activeIncidents)) {
        rows.push(
          exact("incidents", "活跃告警", stats.activeIncidents, activeIncidents.length, "stats.activeIncidents"),
          exact(
            "critical", "其中严重", stats.criticalIncidents,
            activeIncidents.filter((condition) => condition.severity === "critical").length, "stats.criticalIncidents",
          ),
        );
      }
      if (browserCapacity && serverCapacity) {
        const browser = capacityTotals(browserCapacity);
        const server = capacityTotals(serverCapacity);
        rows.push(
          exact("capacity-gpus", "可调度空闲 GPU", server.gpus, browser.gpus, "GET /api/capacity"),
          exact("capacity-hosts", "有空闲卡的机器", server.hosts, browser.hosts, "GET /api/capacity"),
        );
      }
      return Object.freeze(rows);
    }

    function contrastRows(samples) {
      return Object.freeze(samples.map(({ id, label, ratio }) => row(
        id, label, `≥ ${WCAG_AA_TEXT}:1`, finite(ratio) ? `${ratio.toFixed(2)}:1` : "无法读取",
        finite(ratio) && ratio >= WCAG_AA_TEXT, "WCAG 2.1 AA",
      )));
    }

    function layoutRow({ viewportWidth, documentWidth }) {
      return row(
        "overflow", "页面无横向溢出", `${viewportWidth}px`, `${documentWidth}px`,
        documentWidth <= viewportWidth, "布局",
      );
    }

    return Object.freeze({ WCAG_AA_TEXT, reconcile, contrastRows, layoutRow, capacityTotals });
  }

  globalThis.MocopSelfCheck = Object.freeze({ create, parseColor, composite, contrastRatio });
})();
