(() => {
  "use strict";
  const numeric = value => typeof value === "number" && Number.isFinite(value);
  const size = bytes => numeric(bytes) ? bytes >= 1024 ** 4 ? `${(bytes / 1024 ** 4).toFixed(2)} TiB` : `${(bytes / 1024 ** 3).toFixed(1)} GiB` : "—";
  const delta = bytes => numeric(bytes) ? `${bytes >= 0 ? "+" : "−"}${size(Math.abs(bytes))}` : "首个样本";
  const node = (tag, cls, text) => { const e = document.createElement(tag); e.className = cls; if (text != null) e.textContent = text; return e; };
  const date = text => text ? new Date(text).toLocaleString("zh-CN", {hour12:false}) : "等待首次扫描";
  const status = { collecting: "采集中", ok: "已更新", error: "连接异常" };
  const render = (snapshot, container) => {
    container.replaceChildren();
    const rows = [...(snapshot.servers || [])].sort((a,b) => (a.priority || 0) - (b.priority || 0));
    if (!rows.length) { container.append(node("p", "tj-muted", "等待首次个人目录扫描。")); return; }
    const grid = node("div", "sa-grid"), others = node("div", "sa-grid");
    for (const server of rows) {
      const card = node("article", "sa-card"); card.dataset.host = server.host;
      const title = node("div", "sa-card-title"); title.append(node("strong", "", server.label || server.host), node("small", server.stale || server.status === "error" ? "tj-warning" : "tj-muted", `${status[server.status] || "等待扫描"}${server.stale ? " · 数据待更新" : ""}`)); card.append(title);
      card.append(node("p", "sa-total", server.knownBytes === 0 && !server.complete ? "监控目录：尚无完整统计" : `监控目录 ${size(server.knownBytes)}${server.complete ? "" : "（部分已统计）"}`));
      for (const fs of server.filesystems || []) {
        const line = node("div", `sa-filesystem ${fs.pressure || ""}`);
        if (fs.capacityVirtual) {
          line.append(node("strong", "", fs.mountPoint), node("small", "tj-muted", "对象存储挂载 · 系统报告虚拟容量，不代表真实额度或剩余；目录按文件逻辑大小统计"));
          card.append(line); continue;
        }
        line.append(node("strong", "", `${fs.mountPoint} · 剩余 ${size(fs.availableBytes)}`), node("small", "tj-muted", `${fs.kind === "network" ? "共享 / 云盘" : "本地盘"} · 已用 ${fs.percentUsed ?? "—"}% · 可用变化 ${delta(fs.changeAvailableBytes)}`));
        const meter = node("meter", ""); meter.min=0; meter.max=100; meter.value=fs.percentUsed || 0; meter.setAttribute("aria-label", `${fs.mountPoint} 磁盘已用比例`); line.append(meter);
        if (numeric(fs.daysUntilFull)) line.append(node("small", "tj-warning", `按已观测增长速度约 ${fs.daysUntilFull.toFixed(1)} 天耗尽，仅供参考`));
        line.append(node("small", "tj-muted", fs.capacitySampledAt ? `容量实时观测 ${date(fs.capacitySampledAt)}` : "容量来自定期快照"));
        card.append(line);
      }
      const detail = node("details", "sa-directories"); detail.append(node("summary", "", "完整目录路径、占用与增长"));
      detail.open = server.priority <= -90;
      for (const root of server.directories || []) {
        const block = node("div", "sa-root"); block.append(node("strong", "", `${root.path} · ${size(root.bytes)}`), node("small", root.status === "ok" ? "tj-muted" : "tj-warning", root.status === "ok" ? `较上次 ${delta(root.changeBytes)}` : ({timeout:"扫描超时，已完成子目录仍显示，根目录未计入合计",partial:"部分挂载扫描未完成，未计入合计",unreadable:"权限不足或扫描失败",missing:"目录不存在",linked:"入口是软链接，未重复扫描",skipped:"达到扫描时限",inventory_error:"容量读取失败"})[root.status] || "等待采样"));
        if (root.bytes == null && (root.children || []).some(c => numeric(c.bytes))) block.append(node("small", "tj-muted", `已完成子目录至少 ${size(root.children.reduce((sum,c)=>sum+(numeric(c.bytes)?c.bytes:0),0))}，未完成部分待后续扫描`));
        for (const child of root.children || []) block.append(node("div", "sa-child", `${child.path}  ${size(child.bytes)}${child.bytes == null ? " · 等待完整扫描" : ""}`));
        detail.append(block);
      }
      const history = (server.history || []).filter(p => p.complete && numeric(p.knownBytes));
      card.append(node("small", "tj-muted", `30 天趋势：${history.length} 次完整观测${history.length > 1 ? ` · 我的目录累计变化 ${delta(history.at(-1).knownBytes-history[0].knownBytes)}` : " · 第二次扫描后显示增长趋势"}`));
      if (history.length > 1) {
        const trend = document.createElementNS("http://www.w3.org/2000/svg", "svg"); trend.setAttribute("viewBox", "0 0 280 40"); trend.setAttribute("class", "sa-trend"); trend.setAttribute("role", "img"); trend.setAttribute("aria-label", "个人目录占用趋势");
        const low = Math.min(...history.map(p=>p.knownBytes)), high = Math.max(...history.map(p=>p.knownBytes));
        const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline"); line.setAttribute("fill","none"); line.setAttribute("stroke","#64d8b0"); line.setAttribute("stroke-width","2"); line.setAttribute("points",history.map((p,i)=>`${4+i/(history.length-1)*272},${high===low ? 20 : 36-(p.knownBytes-low)/(high-low)*32}`).join(" ")); trend.append(line); card.append(trend);
      }
      card.append(detail, node("small", "tj-muted", `最近成功 ${date(server.sampledAt)} · 下次 ${date(server.nextAttemptAt)} · 共享盘可用空间不可相加`));
      if (server.message) card.append(node("p", "tj-warning", server.message));
      (server.priority <= -30 ? grid : others).append(card);
    }
    container.append(grid);
    if (others.childElementCount) {const more=node("details","sa-more");more.append(node("summary","",`其他 ${others.childElementCount} 台服务器的存储`),others);container.append(more);}
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
  globalThis.MocopStorageAnalysis = Object.freeze({size, delta, render, create});
})();
