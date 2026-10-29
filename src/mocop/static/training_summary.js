(() => {
  "use strict";
  const TERMINAL = new Set(["SUCCEEDED", "FAILED", "STOPPED", "CANCELED", "CANCELLED", "TERMINATED", "COMPLETED", "SUCCESS"]);
  const WAITING = new Set(["PENDING", "QUEUED", "CREATED", "STARTING"]);
  const numeric = value => typeof value === "number" && Number.isFinite(value);
  const timestamp = value => numeric(value) && value > 0 && Number.isFinite(new Date(value).getTime());
  const latest = samples => [...(samples || [])].filter(p => numeric(p.ts)).sort((a, b) => a.ts - b.ts).at(-1);
  const gpuSummary = job => {
    if (Number.isInteger(job.gpu_count) && job.gpu_count >= 0) return { text: `${job.gpu_count} 张`, note: `${job.gpu_model || "平台规格"}${job.gpu_count_source === "browser" ? " · 网页观测" : ""}` };
    const sample = job.metrics_status === 200 && job.metrics?.source === "stored" ? latest(job.metrics.samples) : null;
    return sample?.gpus?.length ? { text: `${sample.gpus.length} 张`, note: "观测 / 副本" } : { text: "未提供", note: job.gpu_model || "规格未知" };
  };
  const duration = value => {
    const seconds = Math.floor(value);
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor(seconds / 3600) % 24;
    const minutes = Math.floor(seconds / 60) % 60;
    return `${days ? `${days}天 ` : ""}${days || hours ? `${hours}小时 ` : ""}${days || hours || minutes ? `${minutes}分` : `${seconds}秒`}`;
  };
  const elapsed = (start, end) => !timestamp(start) || !timestamp(end) || end < start ? "未知" : duration((end - start) / 1000);
  const runtimeSummary = (job, now = Date.now()) => {
    const terminal = TERMINAL.has(job.status);
    const end = terminal ? job.ended_at : now;
    if (WAITING.has(job.status)) return { text: elapsed(job.created_at, now), note: "排队等待" };
    if (numeric(job.runtime_seconds) && job.runtime_seconds >= 0) return { text: duration(job.runtime_seconds), note: terminal ? "运行时长" : "已运行" };
    let start = job.started_at;
    let note = terminal ? "运行时长" : "已运行";
    const replicas = job.replicas;
    if (!timestamp(start) && job.replicas_status === 200 && replicas?.complete && replicas.total > 0 && replicas.replicas?.length === replicas.total && replicas.replicas.every(r => timestamp(r.started_at))) {
      start = Math.min(...replicas.replicas.map(r => r.started_at));
      note = "副本开跑起";
    }
    if (!timestamp(start)) { start = job.created_at; note = terminal ? "提交至结束" : "提交至今"; }
    return { text: elapsed(start, end), note };
  };
  const submittedText = value => {
    if (!timestamp(value)) return "未知";
    const date = new Date(value);
    const pad = value => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  const cell = (className, text, note) => {
    const td = element("td", className);
    td.append(element("span", "tj-cell-value", text));
    if (note) td.append(element("small", "tj-muted", note));
    return td;
  };
  const metricSummary = (job, valueText) => {
    const metrics = job.metrics;
    const raw = [];
    if (metrics?.source === "provider") {
      for (const series of metrics.series || []) if (series.metric === "GpuUsage") { const p = latest(series.points); if (p) raw.push({ ...p, label: series.replica_id }); }
    } else {
      const sample = latest(metrics?.samples);
      for (const gpu of sample?.gpus || []) raw.push({ ts: sample.ts, value: gpu.util_pct, label: `GPU ${gpu.index}` });
    }
    const valid = raw.filter(p => numeric(p.value) && p.value >= 0 && p.value <= 100 && Date.now() - p.ts <= 180000 && p.ts <= Date.now()+60000);
    if (!valid.length) return { text: "—", note: raw.length ? "样本已过期" : "等待利用率采样", title: "缺失或过期数据不计为零" };
    const mean = valid.reduce((n,p) => n+p.value,0)/valid.length;
    return { text: `${valueText(mean)}%`, note: `${metrics.source === "provider" ? "副本均值" : "逐卡均值"} · ${valid.length}/${raw.length} 观测`, title: raw.map(p => `${p.label}: ${valueText(p.value)}% · ${new Date(p.ts).toLocaleString("zh-CN", {hour12:false})}`).join("\n") };
  };
  const table = (jobs, { vendors, badge, openDetail, valueText, timeText }) => {
    const table = element("table", "tj-table");
    const header = element("thead", "");
    const labels = element("tr", "");
    for (const text of ["任务 / 平台", "状态", "GPU 卡数", "提交时间", "时长", "GPU 利用率", "操作"]) {
      const th = element("th", "", text); th.scope = "col"; labels.append(th);
    }
    header.append(labels);
    const body = element("tbody", "");
    for (const job of jobs) {
      const row = element("tr", "tj-card");
      row.dataset.jobId = job.id;
      const name = cell("tj-task", job.name || job.job_id, `${vendors[job.vendor] || job.vendor} · ${job.execution_class === "low_priority" ? "低优先级" : job.execution_class === "standard" ? "标准" : job.execution_class || "未知队列"} · ${job.replicas?.total != null ? `${job.replicas.total} 个副本` : "副本未知"}`);
      name.firstChild.className = "tj-name";
      name.title = `${job.name || job.job_id}\n${job.job_id}\n平台观测 ${timeText(job.updated_at)}`;
      const status = element("td", "tj-state"); status.append(badge(job.status));
      if (!TERMINAL.has(job.status) && timestamp(job.updated_at) && Date.now() - job.updated_at > 120_000) status.append(element("small", "tj-warning", "观测滞后"));
      const gpu = gpuSummary(job);
      const duration = runtimeSummary(job);
      const metrics = metricSummary(job, valueText);
      const submitted = cell("tj-submitted", submittedText(job.created_at)); submitted.title = timeText(job.created_at);
      const action = element("td", "tj-action");
      const button = element("button", "tj-detail-button", "详情"); button.type = "button";
      button.setAttribute("aria-label", `查看 ${job.name || job.job_id} 的详情与日志`);
      button.addEventListener("click", () => openDetail(job)); action.append(button);
      const metricCell = cell("tj-observation", metrics.text, metrics.note); metricCell.title = metrics.title;
      row.append(name, status, cell("tj-gpu", gpu.text, gpu.note), submitted, cell("tj-runtime", duration.text, duration.note), metricCell, action);
      body.append(row);
    }
    table.append(header, body);
    return table;
  };
  globalThis.MocopTrainingSummary = Object.freeze({ gpuSummary, runtimeSummary, submittedText, metricSummary, table });
})();
