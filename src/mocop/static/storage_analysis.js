(() => {
  "use strict";
  const numeric = value => typeof value === "number" && Number.isFinite(value);
  const size = bytes => numeric(bytes) ? bytes >= 1024 ** 4 ? `${(bytes / 1024 ** 4).toFixed(2)} TiB` : `${(bytes / 1024 ** 3).toFixed(1)} GiB` : "—";
  const delta = bytes => numeric(bytes) ? `${bytes >= 0 ? "+" : "−"}${size(Math.abs(bytes))}` : "首个样本";
  const node = (tag, cls, text) => { const e = document.createElement(tag); e.className = cls; if (text != null) e.textContent = text; return e; };
  const date = text => text ? new Date(text).toLocaleString("zh-CN", {hour12:false}) : "等待首次扫描";
  const within = (path, parent) => path === parent || path.startsWith(parent === "/" ? "/" : `${parent}/`);
  const usage = root => {
    if (root.status === "ok" && numeric(root.bytes)) return {text:size(root.bytes),note:"完整统计"};
    const known = (root.children || []).filter(c => numeric(c.bytes));
    const note = ({timeout:"扫描未完成",partial:"部分统计",unreadable:"权限不足 / 扫描失败",missing:"目录不存在",linked:"链接目录，未重复统计"})[root.status] || "等待统计";
    const observed = numeric(root.observedBytes) ? root.observedBytes : known.length ? known.reduce((sum,c) => sum+c.bytes,0) : null;
    return {text:numeric(observed) ? `≥ ${size(observed)}` : "待统计",note};
  };
  const budget = root => {
    if (!numeric(root.limitBytes)) return "";
    const limit = root.limitBytes / 1e9;
    const measured = root.status === "ok" ? root.bytes : numeric(root.observedBytes) ? root.observedBytes : (root.children || []).reduce((sum,child) => sum+(numeric(child.bytes) ? child.bytes : 0),0);
    return numeric(measured) && measured > root.limitBytes ? `超出 ${limit} GB 上限 · 至少超出 ${((measured-root.limitBytes)/1e9).toFixed(1)} GB` : `上限 ${limit} GB${root.status === "ok" && numeric(measured) ? " · 未超限" : " · 待完整统计"}`;
  };
  const directoryRows = server => {
    const filesystems = server.filesystems || [];
    return (server.directories || []).map(root => {
      const path = root.usagePath || root.path;
      const nested = filesystems.filter(fs => fs.kind !== "memory" && fs.mountPoint !== path && within(fs.mountPoint,path));
      const fs = filesystems.find(fs => fs.key && fs.key === root.filesystemKey) ||
        filesystems.filter(fs => within(root.path,fs.mountPoint)).sort((a,b) => b.mountPoint.length-a.mountPoint.length)[0];
      // A directory containing mounts has no single capacity. Never assign the OS disk's capacity to NAS.
      return {path:root.path,root,usage:usage(root),fs:nested.length ? null : fs,nested:nested.length};
    });
  };
  const serverCard = server => {
    const card = node("details", "sa-card"); card.dataset.host = server.host;
    const title = node("summary", "sa-card-title");
    title.append(node("strong", "", server.label || server.host),node("small", server.stale || server.status === "error" ? "tj-warning" : "tj-muted", `${server.status === "collecting" ? "采集中 · " : ""}目录统计 ${date(server.sampledAt)}${server.stale ? " · 待更新" : ""}`)); card.append(title);
    const table = node("table", "sa-table");
    const head = node("thead", ""), header = node("tr", "");
    for (const text of ["完整目录路径", "总容量", "剩余容量", "我占用的"]) header.append(node("th", "", text));
    head.append(header); table.append(head);
    const body = node("tbody", "");
    for (const row of directoryRows(server)) {
      const tr = node("tr", "");
      const pathCell = node("td", "sa-path", row.path);
      if (row.root.purpose) pathCell.append(node("small", "tj-muted", row.root.purpose));
      if (row.root.usagePath && row.root.usagePath !== row.path) pathCell.append(node("small", "tj-muted", `统计范围：${row.root.usagePath}`));
      tr.append(pathCell);
      const total = row.fs?.capacityVirtual ? "额度未知" : row.nested ? `${row.nested} 个挂载` : size(row.fs?.totalBytes);
      const free = row.fs?.capacityVirtual ? "额度未知" : row.nested ? "见扫描详情" : size(row.fs?.availableBytes);
      const totalCell = node("td", "sa-number", total), freeCell = node("td", `sa-number ${row.fs?.pressure === "critical" ? "tj-warning" : ""}`, free);
      const used = node("td", "sa-number"); used.append(node("strong", "", row.usage.text));
      if (row.usage.note !== "完整统计") used.append(node("small", "tj-muted", row.usage.note));
      const limit = budget(row.root);
      if (limit) used.append(node("small", "tj-warning", limit));
      const capacityNote = row.fs?.capacityVirtual ? "对象存储：平台未提供真实额度" : `所在盘 / 共享池容量，不能跨目录相加；容量观测 ${date(row.fs?.capacitySampledAt || server.sampledAt)}`;
      totalCell.title = freeCell.title = capacityNote;
      tr.append(totalCell,freeCell,used); body.append(tr);
    }
    table.append(body); card.append(table);
    const detail = node("details", "sa-directories"); detail.append(node("summary", "", "子目录占用与扫描详情"));
    for (const root of server.directories || []) {
      const block = node("div", "sa-root"); block.append(node("strong", "", root.path));
      block.append(node("small", "tj-muted", `我占用的 ${usage(root).text} · ${usage(root).note} · 较上次 ${delta(root.changeBytes)}`));
      for (const child of root.children || []) block.append(node("div", "sa-child", `${child.path}  ${numeric(child.bytes) ? size(child.bytes) : "待统计"}`));
      detail.append(block);
    }
    detail.append(node("p", "tj-muted", `下次目录扫描 ${date(server.nextAttemptAt)} · 容量每分钟刷新 · 目录每 6 小时统计`));
    for (const fs of server.filesystems || []) {
      const capacity = fs.capacityVirtual ? "对象存储额度未知" : `总 ${size(fs.totalBytes)} · 剩余 ${size(fs.availableBytes)}`;
      detail.append(node("p", "tj-muted", `${fs.mountPoint} · ${capacity}${fs.kind === "memory" ? " · 内存临时盘，不计入磁盘占用" : ""}`));
    }
    if (server.message) detail.append(node("p", "tj-warning", server.message));
    card.append(detail); return card;
  };
  const render = (snapshot, container) => {
    const expanded = new Set([...(container.querySelectorAll?.("details.sa-card[open]") || [])].map(card => card.dataset.host));
    container.replaceChildren();
    const rows = [...(snapshot.servers || [])].sort((a,b) => (a.priority || 0) - (b.priority || 0));
    if (!rows.length) { container.append(node("p", "tj-muted", "等待首次目录扫描。")); return; }
    const grid = node("div", "sa-grid"), others = node("div", "sa-grid");
    for (const server of rows) { const card = serverCard(server); card.open = expanded.has(server.host); (server.priority <= -30 ? grid : others).append(card); }
    container.append(grid);
    container.append(node("p", "sa-footnote tj-muted", "“我占用的”按指定目录统计；共享目录包含该路径内的可访问文件。总容量、剩余属于所在盘 / 共享池，不能跨目录相加；≥ 表示尚未扫完整。"));
    if (others.childElementCount) {const more=node("details","sa-more");more.append(node("summary","",`其他 ${others.childElementCount} 台服务器`),others);container.append(more);}
  };
  const create = ({ request }) => {
    let timer = null, pending = false, started = false;
    const container = document.querySelector("#storage-analysis-content");
    const load = async () => {
      if (pending || !started) return;
      clearTimeout(timer);
      if (document.hidden) {timer=setTimeout(load,60000);return;}
      pending = true;
      const controller = new AbortController(), timeout = setTimeout(() => controller.abort(),15000);
      try { const response = await request("/api/storage-analysis", {signal:controller.signal}); if (!response.ok) throw new Error("storage unavailable"); render(await response.json(), container); }
      catch { if (!container.childElementCount) container.append(node("p", "tj-warning", "存储数据暂不可用，请稍后刷新。")); }
      finally {clearTimeout(timeout);pending=false;timer=setTimeout(load,60000);}
    };
    document.querySelector("#storage-analysis-refresh").addEventListener("click", () => {clearTimeout(timer);load();});
    document.addEventListener("visibilitychange", () => {if (!document.hidden && started) load();});
    return {start:() => {started=true;load();}};
  };
  globalThis.MocopStorageAnalysis = Object.freeze({size, delta, usage, budget, directoryRows, render, create});
})();
