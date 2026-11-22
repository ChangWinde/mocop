// Usage page views: current owners, the occupancy bill, and fleet busy share.
// Builds nodes through the injected factories from data the caller already
// fetched; owner-usage.js owns the wording and aggregation rules.
(() => {
  "use strict";

  const MAX_HOST_CHIPS = 8;
  const MAX_CARDS = 50;

  function create({ create: element, format, memory, numeric, ownerUsage, charts }) {
    function card(attributed, name, nameTitle, kind, amount) {
      const node = element("article", `capacity-candidate ${attributed ? "match" : "near"}`);
      const heading = element("div", "capacity-candidate-heading");
      const identity = element("span", "capacity-candidate-identity");
      const owner = element("strong", "", name);
      owner.title = nameTitle;
      const kindLabel = element("small", "", kind);
      kindLabel.title = kind;
      identity.append(owner, kindLabel);
      heading.append(identity, amount);
      node.append(heading);
      return node;
    }

    function metrics(...items) {
      const node = element("div", "capacity-candidate-metrics");
      node.append(...items.map(([text, className = ""]) => element("span", className, text)));
      return node;
    }

    function ownerCards(ranked, { hostLabels, onHost }) {
      return ranked.slice(0, MAX_CARDS).map((entry) => {
        const kind = entry.kinds.size ? [...entry.kinds].sort().join(" · ") : "进程";
        const amount = element("em", "", `${entry.unknownVramKeys.size ? "至少 " : ""}${memory(entry.vramMiB)}`);
        if (entry.unknownVramKeys.size) amount.title = `${entry.unknownVramKeys.size} 个进程未返回显存占用`;
        const node = card(entry.attributed, entry.label, entry.label, kind, amount);
        if (!entry.attributed) node.title = "启用 workloads.mode=identity 可显示属主与完整命令行";
        const devices = element("div", "capacity-devices");
        [...entry.hosts].sort().slice(0, MAX_HOST_CHIPS).forEach((host) => {
          const chip = element("button", "owner-host-chip", hostLabels.get(host) || host);
          chip.type = "button";
          chip.title = `${host} · 点击查看该节点`;
          chip.addEventListener("click", () => onHost(host));
          devices.append(chip);
        });
        if (entry.hosts.size > MAX_HOST_CHIPS) devices.append(element("span", "", `+${entry.hosts.size - MAX_HOST_CHIPS}`));
        node.append(
          metrics([`${entry.gpus.size} 张 GPU`], [`${entry.hosts.size} 个节点`], [`${entry.processKeys.size} 个进程`]),
          devices,
        );
        return node;
      });
    }

    function offlineNote(count) {
      if (!count) return null;
      const note = element("div", "owners-footnote", `${count} 台离线节点未计入`);
      note.title = "离线节点仅保留最后一次成功采集的进程，不代表当前占用";
      return note;
    }

    function billCards(usage) {
      const nodes = [];
      const days = ownerUsage.usageDays(usage);
      if (days.length) {
        const strip = element("div", "owners-usage-days");
        strip.setAttribute("aria-label", "按天占用");
        strip.append(...days.map(({ day, label }) => element("span", "owner-day-chip", `${day} · ${label}`)));
        nodes.push(strip);
      }
      for (const entry of (usage.owners || []).slice(0, MAX_CARDS)) {
        const kinds = ownerUsage.usageKinds(entry);
        const node = card(
          Boolean(entry.owner), entry.owner || "未归属", entry.owner || "启用 workloads.mode=identity 可区分用户",
          kinds.length ? kinds.join(" · ") : "进程", element("em", "", ownerUsage.gpuHoursLabel(entry.gpuSeconds)),
        );
        const idle = ownerUsage.idleShare(entry);
        node.append(metrics(
          [`${numeric(entry.gpus)} 张 GPU`],
          [Array.isArray(entry.hosts) ? `${entry.hosts.length} 个节点` : "节点未知"],
          [`${numeric(entry.processes)} 段占用`],
          [Number.isFinite(idle) ? `闲置占比 ${format(idle * 100)}%` : "闲置占比未知", Number.isFinite(idle) && idle >= 0.5 ? "owner-idle-high" : ""],
        ));
        nodes.push(node);
      }
      return nodes;
    }

    // Busy share per hour for the fleet plus one row per host, over the
    // report's own window. Busy GPU-hours are the mean busy share times the
    // GPUs and the hours that have samples, so missing hours never count.
    function utilization(report, { hostLabel, onHost }) {
      const window = (rows) => charts.hourlyValues(rows, report.sinceAt, report.generatedAt, charts.busySharePct);
      const fleet = window(report.fleet);
      const known = fleet.values.filter((value) => value != null).length;
      if (!known) {
        return { range: `${report.windowHours} 小时内没有样本`, nodes: [element("div", "utilization-empty", "窗口内没有 GPU 利用率样本")] };
      }
      const axis = element("div", "chart-axis");
      axis.append(element("span", "", `${report.windowHours} 小时前`), element("span", "", "现在"));
      const rows = (report.hosts || []).map((series) => {
        const values = window(series.hours).values;
        const gpus = Math.max(0, ...(series.hours || []).map((row) => numeric(row.gpus)));
        const busy = charts.mean(values);
        const sampled = values.filter((value) => value != null).length;
        return { host: series.host, gpus, busy, busyGpuHours: busy == null ? 0 : (busy / 100) * gpus * sampled };
      }).sort((first, second) => second.busyGpuHours - first.busyGpuHours || first.host.localeCompare(second.host));
      const table = element("table", "data-table utilization-hosts");
      const head = element("thead");
      const headRow = element("tr");
      [["机器", ""], ["GPU", "numeric"], ["忙碌占比", "numeric"], ["忙碌 GPU 小时", "numeric"]].forEach(([label, className]) => {
        const cell = element("th", className, label);
        cell.setAttribute("scope", "col");
        headRow.append(cell);
      });
      head.append(headRow);
      const body = element("tbody");
      rows.forEach((row) => {
        const line = element("tr");
        line.addEventListener("click", () => onHost(row.host));
        line.append(
          element("td", "", hostLabel(row.host)),
          element("td", "numeric", format(row.gpus)),
          element("td", "numeric", row.busy == null ? "—" : `${format(row.busy, 1)}%`),
          element("td", "numeric", format(row.busyGpuHours, 1)),
        );
        body.append(line);
      });
      table.append(head, body);
      return {
        range: `平均 ${format(charts.mean(fleet.values), 1)}% · ${known} / ${fleet.values.length} 小时有样本`,
        nodes: [charts.areaChart(fleet.values), axis, table],
      };
    }

    return Object.freeze({ ownerCards, offlineNote, billCards, utilization });
  }

  globalThis.MocopUsageView = Object.freeze({ create });
})();
