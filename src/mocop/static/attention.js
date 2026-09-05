// The attention panel's decision logic, extracted from app.js under the
// ADR-0021 leaf pattern: which active conditions a host contributes, how the
// fleet's conditions fold into issues (a configured shared path that several
// unreachable hosts traverse, a shared storage device that several hosts
// report, then one issue per remaining host), and how issues rank. Pure over
// the snapshot and the incident payload: app.js injects the formatter, the
// alias sanitizer, and the condition-message localizer, and owns rendering.
(() => {
  "use strict";

  function create({ format, numeric, safeStoredHosts, conditionMessage }) {
    function conditionCategory(condition) {
      if (condition.kind === "connectivity") return "connection";
      if (condition.kind === "disk") return "storage";
      return "compute";
    }

    // Actionable active conditions of one host in the panel's own shape.
    // Connectivity ranks above every resource problem; a critical resource
    // problem ranks above a warning.
    function serverConditions(server, activeConditions) {
      return activeConditions
        .filter((condition) => condition.actionable !== false)
        .map((condition) => ({
          id: condition.conditionKey,
          kind: condition.category,
          severity: condition.severity,
          priority: condition.category === "connectivity"
            ? 3 : condition.severity === "critical" ? 2 : 1,
          message: condition.category === "connectivity" && server.status === "online"
            ? "SSH 已恢复，等待稳定确认"
            : conditionMessage(condition),
          device: String(condition.resource || ""),
          usage: condition.value == null ? -1 : numeric(condition.value, -1),
          sharedKey: condition.groupKey || null,
          source: condition,
        }));
    }

    // One host's remaining conditions as a single issue; the fullest disk
    // leads and the other disks fold into a "+N" suffix.
    function issueFromConditions(server, conditions) {
      if (!conditions.length) return null;
      const disks = conditions
        .filter((condition) => condition.kind === "disk")
        .sort((a, b) => b.usage - a.usage);
      const messages = conditions
        .filter((condition) => condition.kind !== "disk")
        .map((condition) => condition.message);
      if (disks.length) {
        messages.unshift(`${disks[0].message}${disks.length > 1 ? ` +${disks.length - 1}` : ""}`);
      }
      return {
        server,
        hosts: [server.host],
        severity: conditions.some((condition) => condition.severity === "critical") ? "critical" : "warning",
        priority: Math.max(...conditions.map((condition) => condition.priority)),
        messages,
        categories: [...new Set(conditions.map(conditionCategory))].sort(),
        sortName: server.host,
        conditions,
      };
    }

    function sharedPathIssues(conditionsByHost, correlations, consumed) {
      const issues = [];
      correlations.forEach((correlation) => {
        if (
          correlation?.kind !== "configured_shared_path"
          || correlation.confidence !== "possible"
        ) return;
        const anchor = safeStoredHosts([correlation.anchor])[0];
        const hosts = safeStoredHosts(correlation.hosts).filter((host) =>
          conditionsByHost.get(host)?.some((condition) => condition.kind === "connectivity"));
        if (!anchor || hosts.length < 2) return;
        hosts.forEach((host) => {
          conditionsByHost.get(host)
            .filter((condition) => condition.kind === "connectivity")
            .forEach((condition) => consumed.add(`${host}|${condition.id}`));
        });
        issues.push({
          shared: true,
          sharedLabel: "可能的共享链路",
          hosts,
          severity: "critical",
          priority: 3,
          messages: [`${hosts.length} 台节点不可达 · 配置路径经过 ${anchor}`],
          categories: ["connection"],
          sortName: anchor,
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
          messages: [`${hottest.condition.device} ${format(hottest.condition.usage)}% · 影响 ${hosts.length} 台`],
          categories: ["storage"],
          sortName: hottest.condition.device,
        });
      });
      return issues;
    }

    // Shared issues consume the conditions they explain so a host is not
    // listed twice; priority, then severity, then name orders the result.
    function issues({ servers, conditionsByHost, correlations }) {
      const consumed = new Set();
      const result = [
        ...sharedPathIssues(conditionsByHost, correlations, consumed),
        ...sharedStorageIssues(conditionsByHost, consumed),
      ];
      servers.forEach((server) => {
        const remaining = (conditionsByHost.get(server.host) || []).filter(
          (condition) => !consumed.has(`${server.host}|${condition.id}`),
        );
        const issue = issueFromConditions(server, remaining);
        if (issue) result.push(issue);
      });
      return result.sort((a, b) => {
        if (a.priority !== b.priority) return b.priority - a.priority;
        if (a.severity !== b.severity) return a.severity === "critical" ? -1 : 1;
        return a.sortName.localeCompare(b.sortName);
      });
    }

    return Object.freeze({ serverConditions, issueFromConditions, issues });
  }

  globalThis.MocopAttention = Object.freeze({ create });
})();
