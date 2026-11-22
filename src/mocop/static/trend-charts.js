// Trend charts: SVG sparklines for short live history and hourly utilization
// reports. Builds nodes through the injected element factories and holds no
// state. Gaps stay visible: a missing reading or a pause clearly longer than
// the observed cadence splits the drawn path instead of bridging it.
(() => {
  "use strict";

  const WIDTH = 220;
  const BASELINE = 50;
  const PLOT_HEIGHT = 44;
  const AREA_WIDTH = 600;
  const AREA_HEIGHT = 150;
  const HOUR_MS = 3_600_000;

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function create({ create: element, svg, format }) {
    function historyDuration(points) {
      if (points.length < 2) return `${points.length} 个样本`;
      const elapsed = Math.max(0, Date.parse(points.at(-1).observedAt) - Date.parse(points[0].observedAt));
      const minutes = Math.max(1, Math.round(elapsed / 60000));
      return `${points.length} 个样本 · 最近 ${minutes} 分钟`;
    }

    // X follows observedAt so uneven sampling keeps its real spacing.
    function chartPositions(points) {
      const fallback = {
        xs: points.map((_, index) => (points.length > 1 ? (index / (points.length - 1)) * WIDTH : WIDTH)),
        gapBefore: points.map(() => false),
      };
      if (points.length < 2) return fallback;
      const times = points.map((point) => Date.parse(point.observedAt));
      if (!times.every(Number.isFinite)) return fallback;
      const span = times.at(-1) - times[0];
      if (!(span > 0)) return fallback;
      const deltas = times.slice(1).map((time, index) => time - times[index]);
      const positive = deltas.filter((delta) => delta > 0).sort((a, b) => a - b);
      const median = positive.length ? positive[Math.floor(positive.length / 2)] : 0;
      const gapThreshold = median > 0 ? median * 3 : Infinity;
      return {
        xs: times.map((time) => ((time - times[0]) / span) * WIDTH),
        gapBefore: times.map((_, index) => index > 0 && deltas[index - 1] > gapThreshold),
      };
    }

    function canvas() {
      const node = svg("svg", { viewBox: `0 0 ${WIDTH} 54`, preserveAspectRatio: "none", "aria-hidden": "true" });
      node.append(svg("line", { x1: "0", x2: String(WIDTH), y1: String(BASELINE), y2: String(BASELINE), class: "chart-baseline" }));
      return node;
    }

    // Split finite readings into drawable runs at missing values and long pauses.
    function segments(values, gapBefore = []) {
      const runs = [];
      let current = [];
      values.forEach((value, index) => {
        if (!finite(value)) {
          if (current.length) runs.push(current);
          current = [];
          return;
        }
        if (gapBefore[index] && current.length) {
          runs.push(current);
          current = [];
        }
        current.push(index);
      });
      if (current.length) runs.push(current);
      return runs;
    }

    function sparkline(points, accessor, color, maximum = null) {
      const node = canvas();
      const values = points.map(accessor);
      const ceiling = maximum ?? Math.max(1, ...values.filter(finite));
      const { xs, gapBefore } = chartPositions(points);
      const y = (value) => BASELINE - Math.min(1, Math.max(0, value / ceiling)) * PLOT_HEIGHT;
      segments(values, gapBefore).forEach((run) => {
        if (run.length === 1) {
          const dot = svg("circle", { cx: xs[run[0]].toFixed(1), cy: y(values[run[0]]).toFixed(1), r: "1.6" });
          dot.style.setProperty("fill", color);
          node.append(dot);
          return;
        }
        const line = svg("polyline", {
          points: run.map((index) => `${xs[index].toFixed(1)},${y(values[index]).toFixed(1)}`).join(" "),
          fill: "none",
          "stroke-width": "2",
          "vector-effect": "non-scaling-stroke",
        });
        line.style.setProperty("stroke", color);
        node.append(line);
      });
      return node;
    }

    function trendCard(label, points, accessor, formatter, color, maximum = null) {
      const card = element("article", "trend-card");
      const values = points.map(accessor).filter(finite);
      // The current reading is the latest sample only; a missing latest value
      // shows a gap instead of silently holding the previous one.
      const latest = points.length ? accessor(points.at(-1)) : Number.NaN;
      const top = element("div", "trend-card-top");
      top.append(element("span", "", label), element("strong", "", finite(latest) ? formatter(latest) : "—"));
      card.append(top, sparkline(points, accessor, color, maximum));
      const peak = values.length ? Math.max(...values) : null;
      card.append(element("span", "trend-card-foot", peak == null ? "等待有效速率" : `峰值 ${formatter(peak)}`));
      return card;
    }

    function transportRetryCard(points) {
      const { xs } = chartPositions(points);
      const retried = xs.filter((_, index) => Boolean(points[index].transportRetried));
      const card = element("article", "trend-card");
      const top = element("div", "trend-card-top");
      top.append(element("span", "", "链路重试"), element("strong", "", `${format(retried.length)} 次`));
      const node = canvas();
      retried.forEach((x) => {
        const mark = svg("line", {
          x1: x.toFixed(1), x2: x.toFixed(1), y1: "18", y2: String(BASELINE),
          "stroke-width": "2", "vector-effect": "non-scaling-stroke",
        });
        mark.style.setProperty("stroke", "var(--amber)");
        node.append(mark);
      });
      card.append(top, node, element("span", "trend-card-foot", "样本期间 SSH 传输层重连标记"));
      return card;
    }

    // Hourly report rows to one value per hour across the window; hours the
    // report omits (no samples) become null so the chart shows the gap.
    function hourlyValues(hours, sinceAt, untilAt, accessor) {
      const start = Math.floor(Date.parse(sinceAt) / HOUR_MS) * HOUR_MS;
      const end = Math.floor(Date.parse(untilAt) / HOUR_MS) * HOUR_MS;
      if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return { start: Number.NaN, values: [] };
      const byHour = new Map((hours || []).map((row) => [Date.parse(row.hour), row]));
      const values = [];
      for (let time = start; time <= end; time += HOUR_MS) {
        const row = byHour.get(time);
        const value = row ? accessor(row) : null;
        values.push(finite(value) ? value : null);
      }
      return { start, values };
    }

    // memoryUsedAvgMiB averages per device sample while memoryTotalMiB sums
    // the devices (docs/API.md), so the share needs the device count.
    function memorySharePct(row) {
      return row.memoryTotalMiB > 0 && finite(row.memoryUsedAvgMiB) && row.gpus > 0
        ? (row.memoryUsedAvgMiB * row.gpus / row.memoryTotalMiB) * 100 : null;
    }

    function busySharePct(row) {
      return finite(row.busyShare) ? row.busyShare * 100 : null;
    }

    function hourlyPoints({ start, values }) {
      return values.map((value, index) => ({ observedAt: new Date(start + index * HOUR_MS).toISOString(), value }));
    }

    function mean(values) {
      const known = values.filter(finite);
      return known.length ? known.reduce((sum, value) => sum + value, 0) / known.length : null;
    }

    // A wide percentage chart with dashed 50 % and 100 % guides.
    function areaChart(values, color = "var(--blue)") {
      const node = svg("svg", { viewBox: `0 0 ${AREA_WIDTH} ${AREA_HEIGHT}`, preserveAspectRatio: "none", role: "img" });
      const y = (value) => AREA_HEIGHT - 4 - (Math.min(100, Math.max(0, value)) / 100) * (AREA_HEIGHT - 12);
      [50, 100].forEach((level) => node.append(svg("line", {
        x1: "0", x2: String(AREA_WIDTH), y1: y(level).toFixed(1), y2: y(level).toFixed(1), class: "chart-grid",
      })));
      const x = (index) => (values.length > 1 ? (index / (values.length - 1)) * AREA_WIDTH : AREA_WIDTH / 2);
      segments(values).forEach((run) => {
        const coordinates = run.map((index) => `${x(index).toFixed(1)},${y(values[index]).toFixed(1)}`);
        if (run.length > 1) {
          const area = svg("polygon", {
            points: [`${x(run[0]).toFixed(1)},${AREA_HEIGHT}`, ...coordinates, `${x(run.at(-1)).toFixed(1)},${AREA_HEIGHT}`].join(" "),
            class: "utilization-area",
          });
          node.append(area);
        }
        const line = svg("polyline", { points: coordinates.join(" "), class: "utilization-line" });
        line.style.setProperty("stroke", color);
        node.append(line);
      });
      return node;
    }

    return Object.freeze({
      historyDuration, chartPositions, segments, sparkline, trendCard, transportRetryCard,
      hourlyValues, memorySharePct, busySharePct, hourlyPoints, mean, areaChart,
    });
  }

  globalThis.MocopTrendCharts = Object.freeze({ create });
})();
