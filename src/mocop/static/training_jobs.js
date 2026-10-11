(() => {
  "use strict";
  const TERMINAL = new Set(["SUCCEEDED", "FAILED", "STOPPED", "CANCELED", "CANCELLED", "TERMINATED", "COMPLETED", "SUCCESS"]);
  const LABELS = { RUNNING: "运行中", PENDING: "排队中", QUEUED: "排队中", CREATED: "已创建", STARTING: "启动中", SUCCEEDED: "已完成", FAILED: "失败", STOPPED: "已停止", CANCELED: "已取消", CANCELLED: "已取消", STOPPING: "停止中", UNKNOWN: "未知" };
  const VENDORS = { baidu: "百度云", volcengine: "火山引擎", aliyun: "阿里云", ctyun: "天翼云", lanqi: "蓝耆" };
  const METRICS = { GpuUsage: "GPU 使用", GpuMemoryUsage: "显存使用", CpuUsage: "CPU 使用", MemoryUsage: "内存使用" };
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = text;
    return element;
  };
  const numeric = value => typeof value === "number" && Number.isFinite(value);
  const valueText = value => numeric(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 1 }) : "未知";
  const timeText = value => numeric(value) ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "未知";
  const tone = status => status === "FAILED" ? "failed" : status === "RUNNING" ? "running" : ["SUCCEEDED", "COMPLETED", "SUCCESS"].includes(status) ? "success" : "other";
  const badge = status => node("span", `tj-badge ${tone(status)}`, LABELS[status] || status || "未知");
  const latest = points => [...(points || [])].filter(p => numeric(p.ts)).sort((a, b) => a.ts - b.ts).at(-1);
  const errorText = status => ({ 401: "平台令牌已失效", 403: "平台读取权限不足", 404: "任务不存在或已不可访问", 409: "当前厂商不支持", 429: "读取频率受限，请稍后重试", 501: "当前厂商不提供此数据" })[status] || "暂时无法读取";

  const chart = points => {
    const canvas = node("canvas", "tj-sparkline");
    canvas.width = 480;
    canvas.height = 100;
    canvas.setAttribute("aria-label", "最近 30 分钟资源指标趋势，空缺表示无观测");
    const ordered = [...points].filter(p => numeric(p.ts)).sort((a, b) => a.ts - b.ts);
    const valid = ordered.filter(p => numeric(p.value));
    if (!valid.length) return canvas;
    const lo = Math.min(...valid.map(p => p.value));
    const hi = Math.max(...valid.map(p => p.value));
    const start = ordered[0].ts;
    const end = ordered.at(-1).ts;
    const context = canvas.getContext("2d");
    context.strokeStyle = "#64d8b0";
    context.lineWidth = 3;
    context.beginPath();
    let drawing = false;
    for (const point of ordered) {
      if (!numeric(point.value)) { drawing = false; continue; }
      const x = 8 + (point.ts - start) / (end - start || 1) * 464;
      const y = hi === lo ? 50 : 92 - (point.value - lo) / (hi - lo) * 84;
      if (drawing) context.lineTo(x, y); else context.moveTo(x, y);
      drawing = true;
    }
    context.stroke();
    canvas.title = `${timeText(start)} 至 ${timeText(end)}；范围 ${valueText(lo)}–${valueText(hi)}`;
    return canvas;
  };

  const create = ({ request }) => {
    const section = document.querySelector("#training-jobs");
    const cards = document.querySelector("#training-job-cards");
    const status = document.querySelector("#training-jobs-status");
    const summary = document.querySelector("#training-jobs-summary");
    const filter = document.querySelector("#training-jobs-filter");
    const search = document.querySelector("#training-jobs-search");
    const refresh = document.querySelector("#training-jobs-refresh");
    const dialog = document.querySelector("#training-job-dialog");
    const content = document.querySelector("#training-job-detail-content");
    const title = document.querySelector("#training-job-detail-title");
    const detailStatus = document.querySelector("#training-job-detail-status");
    const close = document.querySelector("#training-job-detail-close");
    const detailRefresh = document.querySelector("#training-job-detail-refresh");
    let snapshot = null;
    let started = false;
    let timer = null;
    let loading = false;
    let detailId = null;
    let detailGeneration = 0;
    let detailLoading = false;

    const get = async path => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 45_000);
      try {
        const response = await request(path, { signal: controller.signal });
        if (!response.ok) throw Object.assign(new Error("read failed"), { status: response.status });
        return await response.json();
      } finally { clearTimeout(timeout); }
    };

    const metricsPreview = job => {
      const block = node("div", "tj-metrics");
      if (job.metrics_status !== 200 || !job.metrics) {
        block.append(node("span", "tj-muted", job.metrics_status ? `资源指标：${errorText(job.metrics_status)}` : "资源指标：未采样"));
        return block;
      }
      const metrics = job.metrics;
      if (metrics.source === "provider") {
        const gpu = metrics.series.find(s => s.metric === "GpuUsage");
        const memory = metrics.series.find(s => s.metric === "GpuMemoryUsage" && s.replica_id === gpu?.replica_id);
        const point = latest(gpu?.points);
        block.append(node("span", "tj-metric", `GPU ${valueText(point?.value)}`));
        block.append(node("span", "tj-metric", `显存 ${valueText(latest(memory?.points)?.value)}`));
        block.append(node("small", "tj-muted", "厂商原生值"));
        if (gpu) {
          block.append(chart(gpu.points));
          block.append(node("small", "tj-muted tj-sample-age", `样本 ${timeText(point?.ts)}`));
          block.title = `副本：${gpu.replica_id || "未知"}`;
        }
      } else {
        const sample = [...(metrics.samples || [])].sort((a, b) => (a.ts || 0) - (b.ts || 0)).at(-1);
        block.append(node("span", "tj-metric", `CPU ${valueText(sample?.cpu_pct)}${numeric(sample?.cpu_pct) ? "%" : ""}`));
        block.append(node("span", "tj-metric", `GPU ${sample?.gpus?.length ?? "未知"} 张`));
        block.append(node("small", "tj-muted", metrics.collection_enabled ? "采样已开启" : "平台采样未开启"));
      }
      return block;
    };

    const render = () => {
      if (!snapshot) return;
      const jobs = snapshot.jobs || [];
      const active = jobs.filter(j => !TERMINAL.has(j.status));
      const failed = jobs.filter(j => j.status === "FAILED");
      summary.textContent = `${active.length} 个活跃 · ${snapshot.total} 条记录${snapshot.complete ? "" : "（列表未完整）"}`;
      status.textContent = snapshot.error_status ? `${errorText(snapshot.error_status)}${snapshot.sampled_at ? " · 保留上次观测" : ""}` : `更新 ${timeText(snapshot.sampled_at)} · 约 ${snapshot.poll_interval_seconds || 30} 秒采集`;
      status.classList.toggle("tj-warning", Boolean(snapshot.stale));
      if (snapshot.stale && !snapshot.error_status) status.textContent += " · 数据待更新";
      section.dataset.stale = String(Boolean(snapshot.stale));
      const terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      let visible = filter.value === "active" ? active : filter.value === "failed" ? failed : filter.value === "recent" ? jobs.slice(0, 20) : jobs;
      visible = visible.filter(j => terms.every(t => `${j.name} ${j.job_id} ${j.vendor} ${j.status}`.toLowerCase().includes(t)));
      cards.replaceChildren();
      if (!visible.length) {
        cards.append(node("p", "tj-empty", snapshot.stale && !snapshot.sampled_at ? "训练平台暂不可用，等待采集" : "没有符合筛选条件的任务"));
        return;
      }
      for (const job of visible) {
        const card = node("article", "tj-card");
        card.dataset.jobId = job.id;
        const heading = node("div", "tj-card-top");
        heading.append(node("span", "tj-vendor", VENDORS[job.vendor] || job.vendor), badge(job.status));
        const name = node("h3", "tj-name", job.name || job.job_id);
        name.title = job.name || job.job_id;
        const meta = node("div", "tj-muted", `${job.execution_class === "low_priority" ? "低优先级" : job.execution_class === "standard" ? "标准任务" : job.execution_class || "未知队列"} · ${job.replicas?.total != null ? `${job.replicas.total} 个副本` : "副本数未知"}`);
        card.append(heading, name, meta);
        if (!TERMINAL.has(job.status)) card.append(metricsPreview(job));
        const observed = numeric(job.updated_at) && Date.now() - job.updated_at > 120_000 && !TERMINAL.has(job.status);
        const age = node("small", observed ? "tj-warning" : "tj-muted", `平台观测 ${timeText(job.updated_at)}${observed ? " · 可能滞后" : ""}`);
        const button = node("button", "tj-detail-button", "详情与日志");
        button.type = "button";
        button.addEventListener("click", () => openDetail(job));
        card.append(age, button);
        cards.append(card);
      }
    };

    const renderDetail = data => {
      const job = data.job;
      content.replaceChildren();
      const meta = node("div", "tj-detail-meta");
      meta.append(badge(job.status), node("span", "tj-muted", `${VENDORS[job.vendor] || job.vendor} · ${job.execution_class} · 创建 ${timeText(job.created_at)}`));
      content.append(meta, node("p", "tj-muted", `任务 ID ${job.job_id} · 平台观测 ${timeText(job.updated_at)}`));
      content.append(node("h3", "tj-detail-heading", "副本状态"));
      if (data.replicas_status !== 200) content.append(node("p", "tj-muted", errorText(data.replicas_status)));
      else {
        const replicas = data.replicas;
        content.append(node("p", "tj-muted", `共 ${replicas.total ?? "未知"} 个副本 · 展示 ${replicas.replicas.length} 个${replicas.complete ? "" : "（诊断未完整）"}`));
        for (const r of replicas.replicas) {
          const row = node("div", "tj-replica");
          row.append(node("strong", "", r.name || r.instance_id), node("span", "tj-muted", `${r.status ?? "状态未知"} · 退出码 ${r.exit_code ?? "未知"}`));
          if (r.secondary_status && r.secondary_status !== r.status) row.append(node("span", "tj-muted", r.secondary_status));
          for (const message of [r.failure_message, r.diagnosis_message]) if (message) row.append(node("p", "tj-warning", message));
          content.append(row);
        }
        if (!replicas.replicas.length) content.append(node("p", "tj-muted", "暂无副本观测；不代表训练已完成"));
      }
      content.append(node("h3", "tj-detail-heading", "资源指标 · 最近 30 分钟"));
      if (data.metrics_status !== 200) content.append(node("p", "tj-muted", errorText(data.metrics_status)));
      else if (data.metrics.source === "provider") {
        content.append(node("p", "tj-muted", "下列数值使用厂商原生单位。缺失样本显示未知；不将空缺视为零。"));
        const grid = node("div", "tj-detail-metrics");
        for (const s of data.metrics.series) {
          const cell = node("div", "tj-metric-cell");
          const point = latest(s.points);
          cell.append(node("small", "tj-muted", s.replica_id), node("span", "", METRICS[s.metric] || s.metric), node("strong", "tj-metric-value", valueText(point?.value)), chart(s.points), node("small", "tj-muted", `样本 ${timeText(point?.ts)}`));
          grid.append(cell);
        }
        if (!data.metrics.series.length) grid.append(node("p", "tj-muted", "暂无资源指标"));
        content.append(grid);
      } else {
        content.append(metricsPreview({ metrics: data.metrics, metrics_status: 200 }));
        const sample = [...(data.metrics.samples || [])].sort((a, b) => (a.ts || 0) - (b.ts || 0)).at(-1);
        for (const gpu of sample?.gpus || []) content.append(node("p", "tj-muted", `GPU ${gpu.index ?? "未知"} · 利用率 ${valueText(gpu.util_pct)}${numeric(gpu.util_pct) ? "%" : ""} · 显存 ${valueText(gpu.mem_used_mb)} / ${valueText(gpu.mem_total_mb)} MB`));
      }
      content.append(node("h3", "tj-detail-heading", "任务日志 · 最多 100 行"));
      if (data.logs_status !== 200) content.append(node("p", "tj-muted", errorText(data.logs_status)));
      else {
        const logs = data.logs;
        content.append(node("p", "tj-muted", logs.pending ? "日志尚未就绪" : `本次读取 ${logs.lines.length} 行${logs.truncated ? " · 输出已截断" : ""}，保留平台返回顺序；不代表完整历史。`));
        const text = logs.lines.map(line => `${line.source ? `[${line.source}] ` : ""}${line.body || ""}`).join("\n");
        content.append(node("pre", "tj-logs", text || "暂无日志"));
      }
    };

    const loadDetail = async () => {
      if (!detailId || detailLoading) return;
      const id = detailId;
      const generation = detailGeneration;
      detailLoading = true;
      detailRefresh.disabled = true;
      detailStatus.textContent = "正在读取副本、指标和日志…";
      try {
        const data = await get(`/api/training-job-detail?id=${encodeURIComponent(id)}`);
        if (generation !== detailGeneration || id !== detailId) return;
        renderDetail(data);
        detailStatus.textContent = `读取 ${timeText(data.fetched_at)}`;
      } catch (error) {
        if (generation !== detailGeneration || id !== detailId) return;
        detailStatus.textContent = errorText(error.status);
        if ([401, 403, 404].includes(error.status)) content.replaceChildren(node("p", "tj-warning", errorText(error.status)));
      } finally {
        detailLoading = false;
        detailRefresh.disabled = false;
        if (dialog.open && generation !== detailGeneration) loadDetail();
      }
    };

    const openDetail = job => {
      detailId = job.id;
      detailGeneration += 1;
      title.textContent = job.name || job.job_id;
      content.replaceChildren(node("p", "tj-muted", "正在读取…"));
      if (!dialog.open) dialog.showModal();
      loadDetail();
    };

    const load = async () => {
      if (!started || loading || document.hidden) return;
      loading = true;
      refresh.disabled = true;
      try { snapshot = await get("/api/training-jobs"); section.hidden = !snapshot.enabled; if (snapshot.enabled) render(); }
      catch (error) {
        status.textContent = `训练任务监控：${errorText(error.status)}`;
        status.classList.add("tj-warning");
        section.dataset.stale = "true";
        if ([401, 403].includes(error.status)) {
          snapshot = null;
          cards.replaceChildren(node("p", "tj-empty", "请先登录监管页面"));
          if (dialog.open) dialog.close();
        }
      } finally { loading = false; refresh.disabled = false; }
    };
    refresh.addEventListener("click", load);
    search.addEventListener("input", render);
    filter.addEventListener("change", render);
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => { detailId = null; detailGeneration += 1; content.replaceChildren(); });
    detailRefresh.addEventListener("click", loadDetail);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
    const start = () => {
      if (started) return;
      started = true;
      timer = setInterval(() => { load(); if (dialog.open && !document.hidden) loadDetail(); }, 30_000);
      load();
    };
    window.addEventListener("pagehide", () => { clearInterval(timer); timer = null; });
    window.addEventListener("pageshow", event => {
      if (event.persisted && started && timer === null) {
        timer = setInterval(() => { load(); if (dialog.open && !document.hidden) loadDetail(); }, 30_000);
        load();
      }
    });
    return Object.freeze({ start, refresh: load });
  };
  globalThis.MocopTrainingJobs = Object.freeze({ create, latest, valueText, timeText, errorText });
})();
