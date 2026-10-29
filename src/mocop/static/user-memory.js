// Daily RAM snapshots. Only the server schedules remote collection.
(() => {
  "use strict";
  const gib = (kib) => `${(kib / 1048576).toFixed(2)} GiB`;
  const compactMemory = (kib) => kib >= 1073741824 ? `${(kib / 1073741824).toFixed(2)} TiB` : gib(kib);
  const date = (value) => value ? new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "等待采样";
  const STATUS = { pending: "待采样", collecting: "采样中", ok: "已更新", error: "连接异常" };
  const total = (server) => (server.users || []).reduce((sum, user) => sum + user.rssKiB, 0);
  function create({ button, dialog, close, content, request, delay = setTimeout, cancel = clearTimeout }) {
    let timer = null, pending = false, selected = null, snapshot = null, mode = "rss", selectedMount = null;
    const doc = content.ownerDocument;
    function element(tag, text = "", className = "") {
      const node = doc.createElement(tag);
      node.textContent = text;
      node.className = className;
      return node;
    }
    function meter(value, className = "") {
      const node = element("meter", "", `um-meter ${className}`);
      node.min = 0; node.max = 100; node.value = Math.max(0, Math.min(value, 100));
      node.ariaLabel = "用户 RSS 占物理内存比例";
      return node;
    }
    function render(data) {
      if (!data || !["rss", "disk"].includes(data.metric) || !Array.isArray(data.servers)) throw new TypeError("invalid memory response");
      snapshot = data;
      const disk = data.metric === "disk";
      if (disk) data = { ...data, servers: data.servers.map((server) => ({ ...server, users: (server.directories || []).map((d, uid) => ({ uid, user: d.path.split("/").pop(), path: d.path, mountPoint: d.mountPoint, rssKiB: d.sizeKiB || 0, known: d.sizeKiB !== null, processes: ({ ok: "已统计", unreadable: "权限不足 / 读取失败", timeout: "扫描超时", skipped: "待下次扫描" })[d.status] })), totalKiB: (server.directories || []).reduce((n, d) => n + (d.sizeKiB || 0), 0) })) };
      const titleNode = doc.getElementById?.("user-memory-title"), noteNode = doc.getElementById?.("um-metric-note");
      if (titleNode) titleNode.textContent = disk ? "目录空间" : "用户内存";
      if (noteNode) noteNode.textContent = disk ? "磁盘空间 · 按一级目录统计" : "系统 RAM · 包含 CPU 任务";
      if (!data.servers.length) { content.replaceChildren(element("p", "等待服务器清单和首次采样。", "um-empty")); return; }
      if (!data.servers.some((s) => s.host === selected)) selected = (data.servers.find((s) => s.status === "ok") || data.servers[0]).host;
      const healthy = data.servers.filter((s) => s.status === "ok");
      const stats = element("div", "", "um-stats");
      for (const [label, value, note] of [
        [disk ? "挂载已更新" : "已采样服务器", `${disk ? data.servers.filter((s) => s.inventoryAt && s.status !== "error").length : healthy.length} / ${data.servers.length}`, disk ? "磁盘容量已读取，目录陆续采样" : "异常节点的数据不计入概览"],
        [disk ? "已统计目录空间" : "用户 RSS 总量", disk && !healthy.length ? "采样中" : compactMemory(healthy.reduce((sum, s) => sum + total(s), 0)), "来自最近一次成功采样"],
        [disk ? "目录记录" : "用户记录", String(healthy.reduce((sum, s) => sum + s.users.length, 0)), disk ? "按服务器与目录分别统计" : "按服务器与 UID 分别统计"],
      ]) {
        const stat = element("div", "", "um-stat");
        stat.append(element("span", label), element("strong", value), element("small", note)); stats.append(stat);
      }
      const workspace = element("div", "", "um-workspace");
      const sidebar = element("nav", "", "um-sidebar"); sidebar.ariaLabel = "选择内存采样服务器";
      sidebar.append(element("div", `服务器  /  ${data.servers.length}`, "um-nav-label"));
      for (const server of data.servers) {
        const nav = element("button", "", `user-memory-host um-server ${server.host === selected ? "is-selected" : ""}`);
        nav.type = "button"; nav.ariaPressed = String(server.host === selected);
        const title = element("span", "", "um-server-title");
        title.append(element("i", "", `um-dot ${server.status === "ok" ? "is-ok" : "is-warning"}`), element("strong", server.host));
        const subtitle = server.sampledAt ? `${gib(total(server))} · ${server.users.length} ${disk ? "个目录" : "位用户"}` : disk && server.inventoryAt ? `${server.filesystems.length} 个挂载点 · 目录${server.status === "collecting" ? "扫描中" : "待扫描"}` : STATUS[server.status] || "等待采样";
        nav.append(title, element("span", subtitle, "um-server-subtitle"));
        nav.addEventListener("click", () => { selected = server.host; selectedMount = null; render(snapshot); }); sidebar.append(nav);
      }
      const server = data.servers.find((s) => s.host === selected);
      const detail = element("section", "", disk ? "um-detail um-disk-detail" : "um-detail");
      const heading = element("div", "", "um-detail-heading");
      const identity = element("div"); identity.append(element("span", disk ? "DIRECTORY STORAGE" : "SERVER MEMORY", "um-eyebrow"), element("h3", server.host));
      heading.append(identity, element("span", STATUS[server.status] || "状态未知", `um-status ${server.status === "ok" ? "is-ok" : "is-warning"}`));
      const description = disk && !server.sampledAt ? `已发现 ${(server.filesystems || []).length} 个挂载点 · 目录等待扫描` : disk ? `${server.users.filter((u) => u.known).length} / ${server.users.length} 个目录已统计 · ${compactMemory(total(server))}` : server.totalKiB ? `物理内存 ${gib(server.totalKiB)}  ·  ${server.users.length} 位用户有可见进程` : "等待连接成功后采集用户内存";
      detail.append(heading, element("p", description, "um-description"));
      if (server.message) detail.append(element("div", `${server.message}。${server.sampledAt ? "下方保留上次成功数据。" : "本次未能获取数据。"}`, "um-notice"));
      if (disk && ((server.notes || []).length || server.users.some((u) => !u.known))) detail.append(element("div", "部分目录未统计完成；未知占用不会计为 0。" + (server.notes || []).join("；"), "um-notice"));
      if (disk && server.filesystems?.length) {
        const mounts = element("div", "", "um-mounts"); mounts.ariaLabel = "磁盘与 NAS 挂载点";
        const all = element("button", "全部目录", "um-mount-all"); all.type = "button"; all.addEventListener("click", () => { selectedMount = null; render(snapshot); }); mounts.append(all);
        for (const fs of [...server.filesystems].sort((a, b) => Number(b.kind === "network") - Number(a.kind === "network"))) {
          const card = element("button", "", `um-mount ${selectedMount === fs.mountPoint ? "is-selected" : ""}`); card.type = "button"; card.ariaPressed = String(selectedMount === fs.mountPoint);
          const title = element("strong", fs.mountPoint); title.title = fs.aliases.join(" · ");
          const capacity = (value) => value === null ? "未知" : compactMemory(value);
          if (fs.capacityVirtual) card.append(title, element("span", "对象存储挂载 · 虚拟容量，不代表额度或剩余"));
          else card.append(element("small", `${fs.kind === "network" ? "NAS / 共享存储" : "本地存储"} · ${fs.fsType}`), title, element("span", `总计 ${capacity(fs.totalKiB)} · 已用 ${capacity(fs.usedKiB)}`), element("span", `可用 ${capacity(fs.availableKiB)} · ${fs.percentUsed === null ? "未知" : fs.percentUsed + "%"} 已用`));
          if (fs.aliases.length > 1) card.append(element("small", `同盘入口：${fs.aliases.slice(1).join("、")}`));
          card.addEventListener("click", () => { selectedMount = fs.mountPoint; render(snapshot); }); mounts.append(card);
        }
        detail.append(mounts, element("p", `挂载信息 ${date(server.inventoryAt)} · df 已用空间与已统计目录合计口径不同`, "um-description"));
      }
      const toolbar = element("div", "", "um-table-toolbar");
      toolbar.append(element("strong", disk ? selectedMount ? `${selectedMount} · 目录` : "目录占用排行" : "用户占用排行"), element("span", disk ? "按磁盘占用从高到低" : "按内存占用从高到低"));
      detail.append(toolbar);
      const scroller = element("div", "", "um-table-scroll");
      const table = element("table", "", "um-table");
      const head = element("tr");
      for (const label of [disk ? "目录" : "用户", disk ? "状态" : "进程", disk ? "磁盘占用" : "内存 RSS", disk ? "占已统计目录" : "占物理内存"]) head.append(element("th", label));
      const thead = element("thead"); thead.append(head);
      const body = element("tbody");
      const users = [...(server.users || [])].filter((u) => !disk || !selectedMount || u.mountPoint === selectedMount).sort((a, b) => b.rssKiB - a.rssKiB || a.uid - b.uid);
      users.forEach((user, index) => {
        const row = element("tr"), owner = element("td"), person = element("div", "", "um-person");
        const name = element("div", "", "um-person-name"), info = element("small", disk ? user.path : `UID ${user.uid}`);
        name.append(element("strong", disk ? user.path : user.user), info, element("span", `${user.processes}${disk ? "" : " 进程"}`, "um-mobile-processes"));
        person.append(element("span", String(index + 1).padStart(2, "0"), "um-rank"), name); owner.append(person);
        info.title = disk ? user.path : `UID ${user.uid}`;
        const share = (!disk || user.known) && server.totalKiB > 0 ? 100 * user.rssKiB / server.totalKiB : null;
        const fraction = element("td"), progress = element("div", "", "um-share");
        const gauge = meter(share || 0); if (disk) gauge.ariaLabel = "目录占已统计磁盘空间比例";
        progress.append(gauge, element("span", share === null ? "—" : `${share.toFixed(1)}%`)); fraction.append(progress);
        row.append(owner, element("td", String(user.processes), "um-numeric um-processes"), element("td", disk && !user.known ? "—" : gib(user.rssKiB), "um-numeric um-memory"), fraction); body.append(row);
      });
      table.append(thead, body);
      scroller.append(users.length ? table : element("p", disk ? "该范围暂无目录统计；采样完成后自动显示，权限不足或超时会标注。" : "暂无用户内存数据，采样成功后将在这里显示。", "um-empty"));
      detail.append(scroller);
      const footer = element("div", "", "um-sample-time");
      footer.append(element("span", `最近成功  ${date(server.sampledAt)}`), element("span", `下次采样  ${date(server.nextAttemptAt)}`)); detail.append(footer);
      workspace.append(sidebar, detail); content.replaceChildren(stats, workspace);
    }
    async function refresh() {
      if (pending || !dialog.open) return;
      pending = true;
      cancel(timer); const requestedMode = mode;
      try {
        const response = await request(requestedMode === "disk" ? "/api/directory-disk" : "/api/user-memory", { cache: "no-store" });
        if (!response.ok) throw new Error("request failed");
        const data = await response.json();
        if (requestedMode === mode) render(data);
      } catch (_error) {
        if (requestedMode === mode) content.replaceChildren(element("p", "数据读取失败，请重新打开面板重试。", "um-empty"));
      } finally {
        pending = false;
        if (dialog.open) { if (requestedMode !== mode) refresh(); else timer = delay(refresh, 60000); }
      }
    }
    function open(metric) {
      mode = metric; selectedMount = null; content.replaceChildren(element("p", "正在读取每日快照…", "um-empty")); dialog.showModal(); refresh();
    }
    button.addEventListener("click", () => open("rss"));
    doc.getElementById?.("directory-disk-open")?.addEventListener("click", () => open("disk"));
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => { cancel(timer); timer = null; });
    return Object.freeze({ refresh, render });
  }
  globalThis.MocopUserMemory = Object.freeze({ create });
})();
