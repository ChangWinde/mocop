// Numeric and time formatting shared across the dashboard, extracted from
// app.js under the ADR-0021 leaf pattern. Every function is pure: no DOM,
// no dashboard state, no network. app.js owns when and where text appears.
(() => {
  "use strict";

  function create() {
    function numeric(value, fallback = 0) {
      const result = Number(value);
      return Number.isFinite(result) ? result : fallback;
    }

    function optionalMetric(point, key) {
      return point[key] == null ? NaN : numeric(point[key], NaN);
    }

    function combinedMetric(point, first, second) {
      if (point[first] == null && point[second] == null) return NaN;
      return numeric(point[first]) + numeric(point[second]);
    }

    function clamp(value) {
      return Math.min(100, Math.max(0, numeric(value)));
    }

    function format(value, digits = 0) {
      return numeric(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
    }

    function memory(mib) {
      const amount = numeric(mib);
      if (amount >= 1024) return `${format(amount / 1024, 1)} GiB`;
      return `${format(amount)} MiB`;
    }

    function storage(mib) {
      const amount = numeric(mib);
      if (amount >= 1024 ** 2) return `${format(amount / 1024 ** 2, 1)} TiB`;
      return memory(amount);
    }

    function rate(bytesPerSecond) {
      const value = numeric(bytesPerSecond, NaN);
      if (!Number.isFinite(value)) return "—";
      if (value >= 1024 ** 3) return `${format(value / 1024 ** 3, 1)} GiB/s`;
      if (value >= 1024 ** 2) return `${format(value / 1024 ** 2, 1)} MiB/s`;
      if (value >= 1024) return `${format(value / 1024, 1)} KiB/s`;
      return `${format(value)} B/s`;
    }

    function duration(seconds) {
      const value = numeric(seconds);
      const days = Math.floor(value / 86400);
      const hours = Math.floor((value % 86400) / 3600);
      if (days) return `${days} 天 ${hours} 小时`;
      return `${hours} 小时 ${Math.floor((value % 3600) / 60)} 分钟`;
    }

    function ratio(used, total) {
      return numeric(total) > 0 ? (numeric(used) / numeric(total)) * 100 : 0;
    }

    function age(timestamp, now = Date.now()) {
      if (!timestamp) return "等待数据";
      const seconds = Math.max(0, Math.round((now - Date.parse(timestamp)) / 1000));
      if (seconds < 3) return "刚刚";
      if (seconds < 60) return `${seconds} 秒前`;
      return `${Math.floor(seconds / 60)} 分钟前`;
    }

    // Elapsed-duration wording ("3 小时 12 分") for process runtimes, unlike
    // the "X 前" phrasing that age() produces for sample freshness.
    function durationSince(timestamp, now = Date.now()) {
      const elapsed = now - Date.parse(timestamp);
      if (!Number.isFinite(elapsed)) return "时长未知";
      const seconds = Math.max(0, Math.floor(elapsed / 1000));
      const days = Math.floor(seconds / 86400);
      const hours = Math.floor((seconds % 86400) / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      if (days) return `${days} 天 ${hours} 小时`;
      if (hours) return `${hours} 小时 ${minutes} 分`;
      if (minutes) return `${minutes} 分钟`;
      return `${seconds} 秒`;
    }

    function shortTime(timestamp) {
      const value = new Date(timestamp);
      if (!Number.isFinite(value.getTime())) return "未知时间";
      return new Intl.DateTimeFormat("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(value);
    }

    function retryCountdown(timestamp, now = Date.now()) {
      if (!timestamp) return "";
      const milliseconds = Date.parse(timestamp) - now;
      if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "等待重试";
      const seconds = Math.max(1, Math.ceil(milliseconds / 1000));
      if (seconds < 60) return `${seconds} 秒后重试`;
      return `${Math.ceil(seconds / 60)} 分钟后重试`;
    }

    // Pure text normalization for SSE framing: fold complete CRLF and lone-CR
    // line endings while retaining a final CR until the next chunk arrives,
    // so a CR/LF split across two reads cannot become a false blank line or
    // an unbounded partial frame.
    function appendStreamChunk(buffer, chunk) {
      return `${buffer}${chunk}`.replaceAll("\r\n", "\n").replace(/\r(?!$)/g, "\n");
    }

    return Object.freeze({
      age,
      appendStreamChunk,
      clamp,
      combinedMetric,
      duration,
      durationSince,
      format,
      memory,
      numeric,
      optionalMetric,
      rate,
      ratio,
      retryCountdown,
      shortTime,
      storage,
    });
  }

  globalThis.MocopFormat = Object.freeze({ create });
})();
