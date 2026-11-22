// Alerts page views: active conditions split by how they are being handled,
// the alert-noise card, and hosts in maintenance. Builds nodes through the
// injected factory; incident wording comes from the caller's describe().
(() => {
  "use strict";

  const TABS = Object.freeze(["open", "acknowledged", "silenced"]);
  const CATEGORY_LABELS = Object.freeze({
    connectivity: "连接", cpu: "CPU", memory: "内存", swap: "Swap", disk: "磁盘",
    disk_availability: "磁盘", gpu_temperature: "GPU 温度", gpu_memory: "显存", gpu_idle_memory: "空闲显存",
    gpu_availability: "GPU", gpu_count: "GPU", gpu_ecc: "ECC", gpu_memory_repair: "显存修复",
    gpu_slowdown: "降频", psi_memory: "内存压力", psi_io: "I/O 压力",
  });

  // Silence (explicit or by maintenance) wins over acknowledgement: neither
  // notifies, and a silenced condition is the one an operator chose to mute.
  function tabOf(condition) {
    if (condition.action === "silenced" || condition.maintenanceSilenced) return "silenced";
    if (condition.action === "acknowledged") return "acknowledged";
    return "open";
  }

  function create({ create: element, format, age }) {
    function sideItem(title, detail, action = null) {
      const item = element("div", "side-item");
      const text = element("div");
      text.append(element("strong", "", title), element("small", "", detail));
      item.append(text);
      if (action) item.append(action);
      return item;
    }

    function alertRows(conditions, { tab, hostLabel, describe, onOpen }) {
      const visible = conditions
        .filter((condition) => tabOf(condition) === tab)
        .sort((first, second) => (
          Number(second.severity === "critical") - Number(first.severity === "critical")
          || String(first.firstObservedAt || "").localeCompare(String(second.firstObservedAt || ""))
          || first.host.localeCompare(second.host)
        ));
      if (!visible.length) return [element("div", "alert-list-empty", tab === "open" ? "没有待处理的告警" : "没有")];
      return visible.map((condition) => {
        const { title, detail } = describe(condition);
        const row = element("button", `alert-row ${condition.severity === "critical" ? "critical" : "warning"}`);
        row.type = "button";
        row.setAttribute("role", "listitem");
        row.dataset.host = condition.host;
        const heading = element("span", "alert-row-title");
        heading.append(element("strong", "", hostLabel(condition.host)), element("span", "", title));
        const body = element("span");
        body.append(heading, element("span", "alert-row-meta", detail));
        const since = element("time", "age-relative", age(condition.firstObservedAt || condition.observedAt));
        since.dataset.ageAt = condition.firstObservedAt || condition.observedAt || "";
        row.append(element("i"), body, since);
        row.addEventListener("click", () => onOpen(condition));
        return row;
      });
    }

    function tabCounts(conditions) {
      const counts = Object.fromEntries(TABS.map((tab) => [tab, 0]));
      conditions.forEach((condition) => { counts[tabOf(condition)] += 1; });
      return counts;
    }

    function noise(summary, { hostLabel, onRecurring }) {
      if (!summary) return [element("span", "muted-text", "简报暂不可用")];
      const rate = element("div", "noise-rate");
      const value = element("b", summary.state, format(summary.perHour, 1));
      rate.append(
        value,
        element("span", "", "次/小时"),
        element("small", "", `${format(summary.hours, summary.hours < 10 ? 1 : 0)} 小时内开启 ${format(summary.opened)} 次 · 目标 ≤ ${summary.target}`),
      );
      const items = summary.recurring.map((item, index) => {
        const action = element("button", "inline-action", "治理");
        action.type = "button";
        action.title = "为该主机设置维护窗口，或调大它的探测超时";
        action.addEventListener("click", () => onRecurring(item));
        const share = index === 0 && item.share ? ` · 占 ${format(item.share * 100)}%` : "";
        return sideItem(
          `${hostLabel(item.host)} · ${CATEGORY_LABELS[item.category] || item.category}${item.resource ? ` ${item.resource}` : ""}`,
          `开关 ${format(item.openings)} 次${share}`,
          action,
        );
      });
      return [rate, ...items];
    }

    function maintenance(servers, { hostLabel, onHost }) {
      const maintained = servers.filter((server) => server.maintenance);
      if (!maintained.length) return [element("span", "muted-text", "无")];
      return maintained.map((server) => {
        const open = element("button", "inline-action", "打开");
        open.type = "button";
        open.addEventListener("click", () => onHost(server.host));
        const until = server.maintenance.until ? `至 ${new Date(server.maintenance.until).toLocaleString("zh-CN", { hour12: false })}` : "维护中";
        return sideItem(hostLabel(server.host), [until, server.maintenance.reason].filter(Boolean).join(" · "), open);
      });
    }

    return Object.freeze({ TABS, alertRows, tabCounts, noise, maintenance });
  }

  globalThis.MocopAlertsView = Object.freeze({ create, tabOf });
})();
