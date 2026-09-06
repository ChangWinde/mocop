// Shared-cause grouping for the attention panel, extracted from attention.js
// under the ADR-0021 leaf pattern: a correlation from /api/incidents (a
// configured path several unreachable hosts traverse, or a fleet-wide
// simultaneous loss that points at the monitor's own uplink or a shared
// relay) folds the connectivity conditions it explains into one issue, and a
// shared storage device several hosts report folds into one issue keyed by
// the hottest device. Each group marks the conditions it consumed so a host is
// never listed twice. Pure: app.js injects the formatter and alias sanitizer.
(() => {
  "use strict";

  function create({ format, safeStoredHosts }) {
  // A correlation groups the connectivity failures it explains: a configured
  // path names the anchor; a fleet-wide simultaneous loss names no node,
  // because the likely cause is the monitor's own uplink or a shared relay.
  function sharedPathIssues(conditionsByHost, correlations, consumed) {
    const issues = [];
    correlations.forEach((correlation) => {
      if (correlation?.confidence !== "possible") return;
      const simultaneous = correlation.kind === "simultaneous_connectivity_loss";
      if (!simultaneous && correlation.kind !== "configured_shared_path") return;
      const anchor = simultaneous ? null : safeStoredHosts([correlation.anchor])[0];
      // A host whose loss an earlier group already explains is not listed
      // again, so overlapping correlations never double-count a node.
      const hosts = safeStoredHosts(correlation.hosts).filter((host) =>
        conditionsByHost.get(host)?.some((condition) =>
          condition.kind === "connectivity" && !consumed.has(`${host}|${condition.id}`)));
      if ((!simultaneous && !anchor) || hosts.length < 2) return;
      hosts.forEach((host) => {
        conditionsByHost.get(host)
          .filter((condition) => condition.kind === "connectivity")
          .forEach((condition) => consumed.add(`${host}|${condition.id}`));
      });
      issues.push({
        shared: true,
        sharedLabel: simultaneous ? "疑似监控端链路故障" : "可能的共享链路",
        hosts,
        severity: "critical",
        priority: simultaneous ? 4 : 3,
        messages: [
          simultaneous
            ? `${hosts.length} 台节点在同一采集周期内失联 · 先检查监控端上行或共享中继`
            : `${hosts.length} 台节点不可达 · 配置路径经过 ${anchor}`,
        ],
        categories: ["connection"],
        sortName: simultaneous ? "" : anchor,
      });
    });
    return issues;
  }

  function sharedStorageIssues(conditionsByHost, consumed) {
    const sharedGroups = new Map();
    conditionsByHost.forEach((conditions, host) => {
      conditions.filter((condition) => condition.sharedKey).forEach((condition) => {
        const group = sharedGroups.get(condition.sharedKey) || [];
        group.push({ host, condition });
        sharedGroups.set(condition.sharedKey, group);
      });
    });
    const issues = [];
    sharedGroups.forEach((occurrences) => {
      const byHost = new Map();
      occurrences.forEach((occurrence) => {
        const current = byHost.get(occurrence.host);
        if (!current || occurrence.condition.usage > current.condition.usage) {
          byHost.set(occurrence.host, occurrence);
        }
      });
      if (byHost.size < 2) return;
      occurrences.forEach(({ host, condition }) => consumed.add(`${host}|${condition.id}`));
      const unique = [...byHost.values()];
      const hottest = unique.reduce(
        (current, candidate) => candidate.condition.usage > current.condition.usage ? candidate : current,
      );
      const hosts = unique.map(({ host }) => host).sort((a, b) => a.localeCompare(b));
      issues.push({
        shared: true,
        sharedLabel: "共享存储",
        hosts,
        severity: unique.some(({ condition }) => condition.severity === "critical") ? "critical" : "warning",
        priority: Math.max(...unique.map(({ condition }) => condition.priority)),
        messages: [
          `${hottest.condition.device} ${format(hottest.condition.usage)}%`
          + `${hottest.condition.frozen ? "（离线前）" : ""} · 影响 ${hosts.length} 台`,
        ],
        categories: ["storage"],
        sortName: hottest.condition.device,
      });
    });
    return issues;
  }

    return Object.freeze({ sharedPathIssues, sharedStorageIssues });
  }

  globalThis.MocopAttentionGroups = Object.freeze({ create });
})();
