// The attention panel's decision logic, extracted from app.js under the
// ADR-0021 leaf pattern: which active conditions a host contributes, how the
// fleet's conditions fold into issues (shared-cause groups first, from
// attention-groups.js, then one issue per remaining host), and how issues
// rank. Pure over the snapshot and the incident payload: app.js injects the
// formatter, the alias sanitizer, the condition-message localizer, and the
// grouping leaf, and owns rendering.
(() => {
  "use strict";

  function create({ format, numeric, safeStoredHosts, conditionMessage, groups }) {
    const { sharedPathIssues, sharedStorageIssues } = groups;
    function conditionCategory(condition) {
      if (condition.kind === "connectivity") return "connection";
      if (condition.kind === "disk") return "storage";
      return "compute";
    }

    // Actionable active conditions of one host in the panel's own shape.
    // Connectivity ranks above every resource problem; a critical resource
    // problem ranks above a warning. A resource condition on a host that is
    // not online is frozen: the service keeps it open across failed probes,
    // so its value dates from the last successful sample, not from now.
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
          frozen: condition.category !== "connectivity" && server.status !== "online",
          device: String(condition.resource || ""),
          usage: condition.value == null ? -1 : numeric(condition.value, -1),
          sharedKey: condition.groupKey || null,
          source: condition,
        }));
    }

    function conditionLabel(condition) {
      return condition.frozen ? `${condition.message}（离线前）` : condition.message;
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
        .map(conditionLabel);
      if (disks.length) {
        messages.unshift(`${conditionLabel(disks[0])}${disks.length > 1 ? ` +${disks.length - 1}` : ""}`);
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
