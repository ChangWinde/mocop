(() => {
  "use strict";
  const number = value => typeof value === "number" && Number.isFinite(value);
  const fmt = value => number(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 1 }) : "—";
  const node = (tag, cls, text) => { const e = document.createElement(tag); e.className = cls; if (text != null) e.textContent = text; return e; };
  const stats = snapshot => {
    const overview = snapshot.overview || {};
    const bill = snapshot.platform?.card_hours;
    const personal = bill?.personal;
    const cards = overview.known_running_cards;
    const cardText = snapshot.sampled_at === null ? "等待采集" : cards === 0 && overview.unknown_card_jobs ? "待同步" : `${fmt(cards)}${overview.unknown_card_jobs || snapshot.complete === false ? "+" : ""} 张`;
    return [
      ["运行中总卡数", cardText, `${overview.running_jobs || 0} 个运行任务${overview.unknown_card_jobs ? ` · ${overview.unknown_card_jobs} 个待补规格` : ""}`],
      ["运行卡型", Object.entries(overview.models || {}).map(([model, count]) => `${model} × ${count}`).join(" · ") || "等待任务规格", "按任务申请卡数统计，不用副本数代替卡数"],
      ["本周期原始卡时", `${fmt(personal?.raw_used_card_hours)} 卡·h`, "平台个人额度账单口径"],
      ["折算用量 / 剩余", `${fmt(personal?.used_weighted_card_hours)} / ${fmt(personal?.remaining_card_hours)}`, bill?.error_status ? "账单读取异常，保留旧值" : "卡·h · 含平台优先级折算"],
    ];
  };
  const render = (snapshot, container) => {
    container.replaceChildren();
    const grid = node("div", "tj-overview-grid");
    for (const [label, value, note] of stats(snapshot)) { const cell = node("div", "tj-overview-stat"); cell.append(node("small", "tj-muted", label), node("strong", "", value), node("small", "tj-muted", note)); grid.append(cell); }
    container.append(grid);
    const overview = snapshot.overview || {};
    container.append(node("p", "tj-muted", `我的活动任务申请合计 ${fmt(overview.known_active_cards)}${overview.unknown_active_card_jobs ? "+" : ""} 张 · 启动中 ${fmt(overview.known_starting_cards)} 张 · 排队 ${fmt(overview.known_queued_cards)} 张${overview.unknown_active_card_jobs ? `（${overview.unknown_active_card_jobs} 个任务缺规格，数字仅含已知部分）` : ""}`));
    if (snapshot.overview?.unknown_card_jobs) {
      const reasons = {browser_read_disabled_or_unavailable:"Chrome 网页读取尚未开启或暂不可用",platform_tab_closed:"请保持已登录的训练平台标签页打开",session_or_schema_unavailable:"网页登录已过期或平台数据格式变化",not_configured:"尚未连接网页规格观测"};
      container.append(node("p", "tj-warning", `${snapshot.overview.unknown_card_jobs} 个运行任务仍缺卡型/卡数：${reasons[snapshot.browser?.status] || "等待网页规格同步"}。未知部分未计入总卡数。`));
    }
    const bill = snapshot.platform?.card_hours;
    if (bill?.personal) {
      const p = bill.personal;
      const time = value => number(value) ? new Date(value).toLocaleString("zh-CN", { hour12: false, timeZone: bill.timezone }) : "—";
      container.append(node("p", "tj-accounting-note tj-muted", `账单周期 ${time(p.current_period_start_at)} 至 ${time(p.current_period_end_at)}（${bill.timezone || "平台时区"}） · 不包含项目共享额度的完整消费 · ${snapshot.overview?.utilization_jobs || 0} 个运行任务有近 3 分钟指标`));
    }
    const capacity = snapshot.platform?.capacity;
    const details = node("details", "tj-capacity");
    const known = (capacity?.rows || []).filter(r => number(r.total));
    const pools = known.filter(r => r.kind === "training").map(r => `${r.model.replace(/^NVIDIA /, "")} 总 ${fmt(r.total)} · 启动 ${fmt(r.starting)} · 在用 ${fmt(r.running)} · 剩余 ${fmt(r.free)}`);
    details.append(node("summary", "", pools.length ? `平台训练池：${pools.join(" · ")}（展开）` : "平台总卡 / 空卡 · 展开资源池"));
    if (!known.length) details.append(node("p", "tj-muted", "暂无平台容量观测"));
    else {
      const table = node("table", "tj-table"), head = node("tr", "");
      for (const text of ["卡型 / 资源区", "用途", "总卡", "启动中", "已在用", "剩余", "已分配合计", "观测时间"]) head.append(node("th", "", text));
      const thead = node("thead", ""); thead.append(head); table.append(thead);
      const body = node("tbody", "");
      for (const r of known) { const row = node("tr", ""); for (const text of [`${r.model} · ${r.vendor} / ${r.zone}`, r.kind === "training" ? "训练" : "开发机", fmt(r.total), fmt(r.starting), fmt(r.running), fmt(r.free), fmt(r.allocated), number(r.sampled_at) ? new Date(r.sampled_at).toLocaleTimeString("zh-CN", {hour12:false}) : "—"]) row.append(node("td", "", text)); body.append(row); }
      table.append(body); details.append(table);
    }
    details.append(node("p", "tj-muted", `“—”表示平台未提供启动中或在用明细；已分配合计为总卡减剩余，包含启动和其他占用。仅展示当前账号可见的规格候选池；开发机和训练池可能重叠，各行不能相加。${capacity?.error_status ? "本次读取失败，保留上次观测。" : "剩余是容量观测，不保证可立即调度。"}`));
    container.append(details);
  };
  globalThis.MocopTrainingOverview = Object.freeze({ stats, render });
})();
