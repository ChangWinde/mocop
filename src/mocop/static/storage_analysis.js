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
    return {text:known.length ? `≥ ${size(known.reduce((sum,c) => sum+c.bytes,0))}` : "待统计",note};
  };
  const directoryRows = server => {
    const filesystems = server.filesystems || [];
    return (server.directories || []).flatMap(root => {
      const nested = filesystems.filter(fs => fs.mountPoint !== root.path && within(fs.mountPoint,root.path));
      const fs = filesystems.find(fs => fs.key && fs.key === root.filesystemKey) ||
        filesystems.filter(fs => within(root.path,fs.mountPoint)).sort((a,b) => b.mountPoint.length-a.mountPoint.length)[0];
      // A directory containing mounts has no single capacity. Never assign the OS disk's capacity to NAS.
      const rows = [{path:root.path,usage:usage(root),fs:nested.length ? null : fs,nested:nested.length}];
      for (const mount of nested) {
        const child = (root.children || []).find(c => c.path === mount.mountPoint);
        rows.push({path:mount.mountPoint,usage:usage(child || {status:"pending"}),fs:mount,nested:0});
      }
      return rows;
    });
  };
  const serverCard = server => {
    const card = node("article", "sa-card"); card.dataset.host = server.host;
    const title = node("div", "sa-card-title");
    title.append(node("strong", "", server.label || server.host),node("small", server.stale || server.status === "error" ? "tj-warning" : "tj-muted", `${server.status === "collecting" ? "采集中 · " : ""}目录统计 ${date(server.sampledAt)}${server.stale ? " · 待更新" : ""}`)); card.append(title);
    const table = node("table", "sa-table");
    const head = node("thead", ""), header = node("tr", "");
    for (const text of ["完整目录路径", "总容量", "剩余容量", "目录占用"]) header.append(node("th", "", text));
    head.append(header); table.append(head);
    const body = node("tbody", "");
    for (const row of directoryRows(server)) {
      const tr = node("tr", "");
      tr.append(node("td", "sa-path", row.path));
      const total = row.fs?.capacityVirtual ? "额度未知" : row.nested ? `${row.nested} 个挂载 ↓` : size(row.fs?.totalBytes);
      const free = row.fs?.capacityVirtual ? "额度未知" : row.nested ? "分别见下" : size(row.fs?.availableBytes);
      const totalCell = node("td", "sa-number", total), freeCell = node("td", `sa-number ${row.fs?.pressure === "critical" ? "tj-warning" : ""}`, free);
      const used = node("td", "sa-number"); used.append(node("strong", "", row.usage.text));
      if (row.usage.note !== "完整统计") used.append(node("small", "tj-muted", row.usage.note));
      const capacityNote = row.fs?.capacityVirtual ? "对象存储：平台未提供真实额度" : `所在盘 / 共享池容量，不能跨目录相加；容量观测 ${date(row.fs?.capacitySampledAt || server.sampledAt)}`;
      totalCell.title = freeCell.title = capacityNote;
      tr.append(totalCell,freeCell,used); body.append(tr);
    }
    table.append(body); card.append(table);
    const detail = node("details", "sa-directories"); detail.append(node("summary", "", "子目录占用与扫描详情"));
    for (const root of server.directories || []) {
      const block = node("div", "sa-root"); block.append(node("strong", "", root.path));
      block.append(node("small", "tj-muted", `目录占用 ${usage(root).text} · ${usage(root).note} · 较上次 ${delta(root.changeBytes)}`));
      for (const child of root.children || []) block.append(node("div", "sa-child", `${child.path}  ${numeric(child.bytes) ? size(child.bytes) : "待统计"}`));
      detail.append(block);
    }
    detail.append(node("p", "tj-muted", `下次目录扫描 ${date(server.nextAttemptAt)} · 容量每分钟刷新 · 目录每 6 小时统计`));
    if (server.message) detail.append(node("p", "tj-warning", server.message));
    card.append(detail); return card;
  };
  const render = (snapshot, container) => {
    container.replaceChildren();
    const rows = [...(snapshot.servers || [])].sort((a,b) => (a.priority || 0) - (b.priority || 0));
    if (!rows.length) { container.append(node("p", "tj-muted", "等待首次目录扫描。")); return; }
    const grid = node("div", "sa-grid"), others = node("div", "sa-grid");
    for (const server of rows) (server.priority <= -30 ? grid : others).append(serverCard(server));
    container.append(grid);
    container.append(node("p", "sa-footnote tj-muted", "总容量、剩余为目录所在盘 / 共享池的容量；目录占用仅统计指定路径，不能当作整盘已用。共享目录包含该目录内的可访问文件；≥ 表示尚未扫完整。"));
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
  globalThis.MocopStorageAnalysis = Object.freeze({size, delta, usage, directoryRows, render, create});
})();
