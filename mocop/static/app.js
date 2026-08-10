"use strict";

const PREFERENCE_STORAGE_KEY = "mocop.preferences.v1";
const DEFAULT_PREFERENCES = Object.freeze({
  serverSort: "custom",
  serverOrder: [],
  gpuSort: "host",
  heatMetric: "utilization",
  showTemperature: true,
  showPower: true,
});
const SERVER_SORT_VALUES = new Set(["custom", "host", "status", "gpu", "cpu"]);
const GPU_SORT_VALUES = new Set(["host", "utilization", "memory", "temperature", "power"]);
const HEAT_METRIC_VALUES = new Set(["utilization", "memory", "temperature"]);

function safeStoredHosts(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter(
    (host) => typeof host === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$/.test(host),
  ))];
}

function loadPreferences() {
  try {
    const stored = JSON.parse(localStorage.getItem(PREFERENCE_STORAGE_KEY) || "null");
    if (!stored || typeof stored !== "object" || Array.isArray(stored)) {
      return { ...DEFAULT_PREFERENCES };
    }
    return {
      serverSort: SERVER_SORT_VALUES.has(stored.serverSort)
        ? stored.serverSort : DEFAULT_PREFERENCES.serverSort,
      serverOrder: safeStoredHosts(stored.serverOrder),
      gpuSort: GPU_SORT_VALUES.has(stored.gpuSort)
        ? stored.gpuSort : DEFAULT_PREFERENCES.gpuSort,
      heatMetric: HEAT_METRIC_VALUES.has(stored.heatMetric)
        ? stored.heatMetric : DEFAULT_PREFERENCES.heatMetric,
      showTemperature: typeof stored.showTemperature === "boolean"
        ? stored.showTemperature : DEFAULT_PREFERENCES.showTemperature,
      showPower: typeof stored.showPower === "boolean"
        ? stored.showPower : DEFAULT_PREFERENCES.showPower,
    };
  } catch (_error) {
    return { ...DEFAULT_PREFERENCES };
  }
}

const preferences = loadPreferences();

const view = {
  snapshot: null,
  selectedHost: "all",
  serverFilter: "all",
  attentionFilter: "all",
  filter: "all",
  sort: preferences.gpuSort,
  heatMetric: preferences.heatMetric,
  serverSort: preferences.serverSort,
  serverOrder: preferences.serverOrder,
  query: "",
  lastEventAt: 0,
  history: null,
  historyKey: "",
  historyRequest: 0,
  historyLoading: false,
  trendRenderKey: "",
  renderFrame: null,
  expandedHosts: new Set(),
  groupCache: new Map(),
  serverItemCache: new Map(),
  fleetAllCache: null,
  fleetEmptyNode: null,
  heatmapCache: new Map(),
  heatmapAxisCache: null,
  singleTableCache: null,
  incidents: null,
  attentionRenderKey: "",
  incidentRenderKey: "",
  incidentVersion: -1,
  incidentRequest: 0,
  incidentLoadingVersion: null,
  incidentExpanded: false,
  transportKind: "connecting",
  transportLabel: "连接中",
  refreshFeedbackTimer: null,
  cadenceSnapshotFloor: null,
  connectionErrorTimer: null,
  snapshotFetchInFlight: null,
  draggedHost: null,
  suppressServerClick: false,
  selectedGpu: null,
};

try {
  const initialHost = decodeURIComponent(window.location.hash.slice(1));
  if (/^[A-Za-z0-9._-]+$/.test(initialHost)) view.selectedHost = initialHost;
} catch (_error) {
  view.selectedHost = "all";
}

const $ = (selector) => document.querySelector(selector);
const SERVER_FILTER_LABELS = Object.freeze({
  all: "全部服务器",
  issues: "异常节点",
  busy: "计算中节点",
  available: "有空闲 GPU",
  stale: "陈旧节点",
});
const elements = {
  connection: $("#connection"),
  connectionText: $("#connection-text"),
  settingsToggle: $("#settings-toggle"),
  settingsDialog: $("#settings-dialog"),
  serverSort: $("#server-sort"),
  settingsGpuSort: $("#settings-gpu-sort"),
  settingsHeatMetric: $("#settings-heat-metric"),
  showTemperature: $("#show-temperature"),
  showPower: $("#show-power"),
  resetPreferences: $("#reset-preferences"),
  refreshInterval: $("#refresh-interval"),
  refreshFeedback: $("#refresh-feedback"),
  lastSync: $("#last-sync"),
  serverCard: $("#server-card"),
  serverBar: $("#server-bar"),
  totalGpus: $("#total-gpus"),
  activeGpus: $("#active-gpus"),
  gpuCard: $("#gpu-card"),
  gpuBar: $("#gpu-bar"),
  gpuMemoryCard: $("#gpu-memory-card"),
  gpuMemoryPercent: $("#gpu-memory-percent"),
  gpuMemoryUsed: $("#gpu-memory-used"),
  gpuMemoryTotal: $("#gpu-memory-total"),
  gpuMemoryBar: $("#gpu-memory-bar"),
  serverRatio: $("#server-ratio"),
  serverHealth: $("#server-health"),
  serverDetail: $("#server-detail"),
  averageCpu: $("#average-cpu"),
  cpuCard: $("#cpu-card"),
  cpuBar: $("#cpu-bar"),
  cpuHealth: $("#cpu-health"),
  cpuCores: $("#cpu-cores"),
  memoryPercent: $("#memory-percent"),
  memoryUsed: $("#memory-used"),
  memoryTotal: $("#memory-total"),
  memoryBar: $("#memory-bar"),
  memoryCard: $("#memory-card"),
  networkRx: $("#network-rx"),
  networkTx: $("#network-tx"),
  collectorError: $("#collector-error"),
  attentionPanel: $("#attention-panel"),
  attentionSummary: $("#attention-summary"),
  attentionList: $("#attention-list"),
  attentionStatus: $("#attention-panel .attention-heading > span"),
  incidentPanel: $("#incident-panel"),
  incidentSummary: $("#incident-summary"),
  incidentList: $("#incident-list"),
  incidentToggle: $("#toggle-incidents"),
  serverCount: $("#server-count"),
  serverList: $("#server-list"),
  inventoryTitle: $("#inventory-title"),
  visibleCount: $("#visible-count"),
  nodeFreshness: $("#node-freshness"),
  resourceGrid: $("#resource-grid"),
  gpuHeatmap: $("#gpu-heatmap"),
  heatmapGrid: $("#heatmap-grid"),
  diskList: $("#disk-list"),
  nodeNotice: $("#node-notice"),
  trendPanel: $("#trend-panel"),
  trendRange: $("#trend-range"),
  trendGrid: $("#trend-grid"),
  search: $("#search"),
  gpuSort: $("#gpu-sort"),
  exportCsv: $("#export-csv"),
  groupToggle: $("#toggle-groups"),
  gpuGroups: $("#gpu-groups"),
  emptyState: $("#empty-state"),
  pollInfo: $("#poll-info"),
  gpuDetailDialog: $("#gpu-detail-dialog"),
  gpuDetailHost: $("#gpu-detail-host"),
  gpuDetailTitle: $("#gpu-detail-title"),
  gpuDetailState: $("#gpu-detail-state"),
  gpuDetailMetrics: $("#gpu-detail-metrics"),
  gpuTaskCount: $("#gpu-task-count"),
  gpuTaskList: $("#gpu-task-list"),
};

function create(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function savePreferences() {
  const value = {
    serverSort: view.serverSort,
    serverOrder: view.serverOrder,
    gpuSort: view.sort,
    heatMetric: view.heatMetric,
    showTemperature: preferences.showTemperature,
    showPower: preferences.showPower,
  };
  try {
    localStorage.setItem(PREFERENCE_STORAGE_KEY, JSON.stringify(value));
  } catch (_error) {
    // Rendering must remain available when browser storage is disabled or full.
  }
}

function syncPreferenceControls() {
  elements.serverSort.value = view.serverSort;
  elements.gpuSort.value = view.sort;
  elements.settingsGpuSort.value = view.sort;
  elements.settingsHeatMetric.value = view.heatMetric;
  elements.showTemperature.checked = preferences.showTemperature;
  elements.showPower.checked = preferences.showPower;
  document.body.classList.toggle("hide-gpu-temperature", !preferences.showTemperature);
  document.body.classList.toggle("hide-gpu-power", !preferences.showPower);
}

function resetPreferences() {
  view.serverSort = DEFAULT_PREFERENCES.serverSort;
  view.serverOrder = [];
  view.sort = DEFAULT_PREFERENCES.gpuSort;
  view.heatMetric = DEFAULT_PREFERENCES.heatMetric;
  preferences.showTemperature = DEFAULT_PREFERENCES.showTemperature;
  preferences.showPower = DEFAULT_PREFERENCES.showPower;
  view.serverItemCache.clear();
  view.groupCache.clear();
  view.heatmapCache.clear();
  syncPreferenceControls();
  savePreferences();
  render();
}

function numeric(value, fallback = 0) {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function optionalMetric(point, key) {
  return point[key] == null ? NaN : numeric(point[key], NaN);
}

function combinedMetric(point, first, second) {
  if (point[first] == null && point[second] == null) return NaN;
  return numeric(point[first]) + numeric(point[second]);
}

function clamp(value) {
  return Math.min(100, Math.max(0, numeric(value)));
}

function format(value, digits = 0) {
  return numeric(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function memory(mib) {
  const amount = numeric(mib);
  if (amount >= 1024) return `${format(amount / 1024, 1)} GiB`;
  return `${format(amount)} MiB`;
}

function storage(mib) {
  const amount = numeric(mib);
  if (amount >= 1024 ** 2) return `${format(amount / 1024 ** 2, 1)} TiB`;
  return memory(amount);
}

function rate(bytesPerSecond) {
  const value = numeric(bytesPerSecond, NaN);
  if (!Number.isFinite(value)) return "—";
  if (value >= 1024 ** 3) return `${format(value / 1024 ** 3, 1)} GiB/s`;
  if (value >= 1024 ** 2) return `${format(value / 1024 ** 2, 1)} MiB/s`;
  if (value >= 1024) return `${format(value / 1024, 1)} KiB/s`;
  return `${format(value)} B/s`;
}

function duration(seconds) {
  const value = numeric(seconds);
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  if (days) return `${days} 天 ${hours} 小时`;
  return `${hours} 小时 ${Math.floor((value % 3600) / 60)} 分钟`;
}

function ratio(used, total) {
  return numeric(total) > 0 ? (numeric(used) / numeric(total)) * 100 : 0;
}

function age(timestamp) {
  if (!timestamp) return "等待数据";
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(timestamp)) / 1000));
  if (seconds < 3) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  return `${Math.floor(seconds / 60)} 分钟前`;
}

function retryCountdown(timestamp) {
  if (!timestamp) return "";
  const milliseconds = Date.parse(timestamp) - Date.now();
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "等待重试";
  const seconds = Math.max(1, Math.ceil(milliseconds / 1000));
  if (seconds < 60) return `${seconds} 秒后重试`;
  return `${Math.ceil(seconds / 60)} 分钟后重试`;
}

function refreshRelativeTimes() {
  document.querySelectorAll("[data-retry-at]").forEach((element) => {
    element.textContent = retryCountdown(element.dataset.retryAt);
  });
  document.querySelectorAll("[data-age-at]").forEach((element) => {
    element.textContent = age(element.dataset.ageAt);
  });
}

function collectionHealth() {
  if (!view.snapshot?.lastPollCompletedAt) {
    return { state: "waiting", elapsedSeconds: null, staleAfterSeconds: null };
  }
  const completedAt = Date.parse(view.snapshot.lastPollCompletedAt);
  if (!Number.isFinite(completedAt)) {
    return { state: "waiting", elapsedSeconds: null, staleAfterSeconds: null };
  }
  const elapsedSeconds = Math.max(
    0,
    (Date.now() - completedAt) / 1000,
  );
  const fallback = Math.max(15, numeric(view.snapshot.pollIntervalSeconds) * 3);
  const staleAfterSeconds = numeric(
    view.snapshot.collectionStaleAfterSeconds,
    fallback,
  );
  return {
    state: elapsedSeconds > staleAfterSeconds ? "delayed" : "fresh",
    elapsedSeconds,
    staleAfterSeconds,
  };
}

function renderConnectionStatus() {
  let kind = view.transportKind;
  let label = view.transportLabel;
  let title = "";
  if (view.transportKind === "live") {
    const health = collectionHealth();
    if (health.state === "waiting") {
      kind = "connecting";
      label = "等待采集";
      title = "实时通道已连接，正在等待首轮采集完成";
    } else if (health.state === "delayed") {
      kind = "delayed";
      label = "采集延迟";
      title = `实时通道已连接，但上轮采集完成于 ${age(view.snapshot.lastPollCompletedAt)}`;
    } else {
      title = `上轮采集完成于 ${age(view.snapshot.lastPollCompletedAt)}`;
    }
  }
  elements.connection.className = `connection ${kind}`;
  elements.connectionText.textContent = label;
  elements.connection.title = title;
}

function syncRefreshControl() {
  if (!view.snapshot) return;
  const seconds = numeric(view.snapshot.pollIntervalSeconds);
  const value = String(seconds);
  elements.refreshInterval.querySelectorAll("option[data-current]").forEach((option) => {
    if (option.value !== value) option.remove();
  });
  let option = [...elements.refreshInterval.options].find((item) => item.value === value);
  if (!option) {
    option = create("option", "", `${format(seconds, 1)} 秒（配置）`);
    option.value = value;
    option.dataset.current = "true";
    elements.refreshInterval.prepend(option);
  }
  elements.refreshInterval.value = value;
}

function acceptSnapshot(snapshot) {
  if (
    !snapshot
    || typeof snapshot !== "object"
    || !Number.isSafeInteger(snapshot.version)
    || typeof snapshot.startedAt !== "string"
  ) {
    throw new TypeError("Invalid snapshot envelope");
  }

  const floor = view.cadenceSnapshotFloor;
  if (floor && snapshot.startedAt !== floor.startedAt) {
    view.cadenceSnapshotFloor = null;
  } else if (floor && snapshot.version < floor.version) {
    return false;
  } else if (floor) {
    view.cadenceSnapshotFloor = null;
  }

  if (
    view.snapshot
    && snapshot.startedAt === view.snapshot.startedAt
    && snapshot.version < view.snapshot.version
  ) {
    return false;
  }
  view.snapshot = snapshot;
  return true;
}

function showRefreshFeedback(kind, message) {
  const control = elements.refreshInterval.closest(".refresh-control");
  control.classList.remove("pending", "saved", "error");
  if (kind) control.classList.add(kind);
  elements.refreshFeedback.textContent = message;
  if (view.refreshFeedbackTimer != null) clearTimeout(view.refreshFeedbackTimer);
  if (kind === "saved" || kind === "error") {
    view.refreshFeedbackTimer = setTimeout(() => {
      control.classList.remove(kind);
      view.refreshFeedbackTimer = null;
    }, 2400);
  }
}

async function updatePollInterval() {
  const requested = numeric(elements.refreshInterval.value, NaN);
  if (!Number.isFinite(requested)) {
    syncRefreshControl();
    return;
  }
  elements.refreshInterval.disabled = true;
  showRefreshFeedback("pending", `正在调整为 ${format(requested)} 秒`);
  try {
    const response = await fetch("/api/settings/poll-interval", {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Monitor-Request": "dashboard",
      },
      body: JSON.stringify({ pollIntervalSeconds: requested }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const settings = await response.json();
    if (
      !Number.isSafeInteger(settings.version)
      || typeof settings.startedAt !== "string"
    ) {
      throw new TypeError("Invalid settings response");
    }
    view.cadenceSnapshotFloor = {
      version: settings.version,
      startedAt: settings.startedAt,
    };
    if (view.snapshot?.startedAt === settings.startedAt) {
      view.snapshot.pollIntervalSeconds = settings.pollIntervalSeconds;
      view.snapshot.collectionStaleAfterSeconds = settings.collectionStaleAfterSeconds;
    }
    elements.refreshInterval.value = String(settings.pollIntervalSeconds);
    showRefreshFeedback("saved", `采集频率已调整为 ${format(settings.pollIntervalSeconds)} 秒`);
    renderSummary();
  } catch (_error) {
    syncRefreshControl();
    showRefreshFeedback("error", "采集频率调整失败，已恢复原设置");
  } finally {
    elements.refreshInterval.disabled = false;
  }
}

function limits() {
  return view.snapshot?.thresholds || {
    cpu_warning_pct: 85,
    memory_warning_pct: 90,
    swap_warning_pct: 50,
    disk_warning_pct: 85,
    gpu_temperature_warning_c: 80,
    gpu_busy_pct: 10,
  };
}

function serverStatus(server) {
  if (server.stale) return ["数据陈旧", "stale"];
  const states = {
    online: ["在线", "online"],
    unreachable: ["SSH 不可达", "issue"],
    no_nvidia_smi: ["无 nvidia-smi", "issue"],
    error: ["采集错误", "issue"],
    pending: ["等待探测", "pending"],
  };
  return states[server.status] || ["未知", "issue"];
}

function failureText(message) {
  const messages = {
    "SSH host key changed": "SSH 主机密钥发生变化",
    "SSH host key is not trusted": "SSH 主机密钥尚未信任",
    "SSH authentication failed": "SSH 身份认证失败",
    "SSH name resolution failed": "SSH 主机名解析失败",
    "SSH connection was refused": "SSH 连接被拒绝",
    "SSH connection timed out": "SSH 连接超时",
    "SSH network is unreachable": "SSH 网络不可达",
    "SSH connection failed": "SSH 连接失败",
    "SSH/resource collection timed out": "SSH / 资源采集超时",
    "Local resource collection timed out": "本机资源采集超时",
    "Local resource probe could not be started": "本机资源探针无法启动",
    "Local resource output was not recognized": "本机资源数据格式异常",
    "Local resource output exceeded the configured limit": "本机资源输出超过安全上限",
    "Remote resource output was not recognized": "远端资源数据格式异常",
    "Remote resource output exceeded the configured limit": "远端资源输出超过安全上限",
    "nvidia-smi is unavailable": "系统在线，但未安装 nvidia-smi",
    "nvidia-smi query failed": "系统在线，但 GPU 查询失败",
  };
  return messages[message] || message || "采集失败";
}

function allGpuRecords() {
  if (!view.snapshot) return [];
  const servers = view.selectedHost === "all"
    ? focusedServers(view.snapshot.servers).filter(
      (server) => server.status === "online" || (view.serverFilter === "stale" && server.stale),
    )
    : view.snapshot.servers.filter((server) => server.host === view.selectedHost);
  return servers.flatMap((server) =>
    server.gpus.map((gpu) => ({ server, gpu })),
  );
}

function serverMatchesFocus(server) {
  const threshold = limits().gpu_busy_pct;
  if (view.serverFilter === "issues") return Boolean(serverIssue(server));
  if (view.serverFilter === "busy") {
    return server.status === "online" && server.gpus.some(
      (gpu) => numeric(gpu.utilization_gpu_pct) >= threshold,
    );
  }
  if (view.serverFilter === "available") {
    return server.status === "online" && server.gpus.some(
      (gpu) => numeric(gpu.utilization_gpu_pct) < threshold,
    );
  }
  if (view.serverFilter === "stale") return Boolean(server.stale);
  return true;
}

function focusedServers(servers) {
  return servers.filter(serverMatchesFocus);
}

function selectHost(host) {
  if (view.selectedHost === host) return;
  view.selectedHost = host;
  const fragment = host === "all" ? window.location.pathname : `#${encodeURIComponent(host)}`;
  window.history.replaceState(null, "", fragment);
  view.history = null;
  view.historyKey = "";
  view.historyRequest += 1;
  view.historyLoading = host !== "all";
  view.trendRenderKey = "";
  render();
  syncHistory();
}

function incidentConditionMessage(condition) {
  const value = condition.value == null ? null : numeric(condition.value);
  const expected = condition.threshold == null ? null : numeric(condition.threshold);
  const resource = condition.resource || "资源";
  if (condition.category === "connectivity") return failureText(condition.detail);
  if (condition.category === "gpu_availability") return failureText(condition.detail);
  if (condition.category === "gpu_count") {
    return `GPU 数量 ${format(value)} / 预期 ${format(expected)}`;
  }
  if (condition.category === "gpu_processes") return `${resource} 数据不可用`;
  if (condition.category === "gpu_ecc") return `${resource} · ${format(value)} 个未纠正错误`;
  if (condition.category === "gpu_memory_repair") return `${resource} · 存在待处理显存修复`;
  if (condition.category === "gpu_slowdown") return `${resource} · 硬件降频已触发`;
  if (condition.category === "gpu_idle_memory") {
    return `${resource} ${format(value, 1)}% · 持续低负载`;
  }
  if (condition.category === "gpu_temperature") return `${resource} ${format(value, 1)}°C`;
  if (value != null) return `${resource} ${format(value, 1)}%`;
  return condition.detail || resource;
}

function serverConditions(server) {
  if (!Array.isArray(view.incidents?.active)) return [];
  return view.incidents.active
    .filter((condition) => condition.host === server.host)
    .map((condition) => ({
      id: condition.conditionKey,
      kind: condition.category,
      severity: condition.severity,
      priority: condition.category === "connectivity"
        ? 3 : condition.severity === "critical" ? 2 : 1,
      message: incidentConditionMessage(condition),
      device: String(condition.resource || ""),
      usage: condition.value == null ? -1 : numeric(condition.value, -1),
      sharedKey: condition.groupKey || null,
    }));
}

function conditionCategory(condition) {
  if (condition.kind === "connectivity") return "connection";
  if (condition.kind === "disk") return "storage";
  return "compute";
}

function issueFromConditions(server, conditions) {
  if (!conditions.length) return null;
  const disks = conditions
    .filter((condition) => condition.kind === "disk")
    .sort((a, b) => b.usage - a.usage);
  const messages = conditions
    .filter((condition) => condition.kind !== "disk")
    .map((condition) => condition.message);
  if (disks.length) {
    messages.unshift(`${disks[0].message}${disks.length > 1 ? ` +${disks.length - 1}` : ""}`);
  }
  const priority = Math.max(...conditions.map((condition) => condition.priority));
  return {
    server,
    hosts: [server.host],
    severity: conditions.some((condition) => condition.severity === "critical") ? "critical" : "warning",
    priority,
    messages,
    categories: [...new Set(conditions.map(conditionCategory))].sort(),
    sortName: server.host,
  };
}

function serverIssue(server) {
  return issueFromConditions(server, serverConditions(server));
}

function attentionIssues() {
  const conditionsByHost = new Map(
    view.snapshot.servers.map((server) => [server.host, serverConditions(server)]),
  );
  const sharedGroups = new Map();
  conditionsByHost.forEach((conditions, host) => {
    conditions.filter((condition) => condition.sharedKey).forEach((condition) => {
      const group = sharedGroups.get(condition.sharedKey) || [];
      group.push({ host, condition });
      sharedGroups.set(condition.sharedKey, group);
    });
  });

  const consumed = new Set();
  const issues = [];
  sharedGroups.forEach((occurrences) => {
    const byHost = new Map();
    occurrences.forEach((occurrence) => {
      const current = byHost.get(occurrence.host);
      if (!current || occurrence.condition.usage > current.condition.usage) {
        byHost.set(occurrence.host, occurrence);
      }
    });
    if (byHost.size < 2) return;
    occurrences.forEach(({ host, condition }) => consumed.add(`${host}|${condition.id}`));
    const unique = [...byHost.values()];
    const hottest = unique.reduce(
      (current, candidate) => candidate.condition.usage > current.condition.usage ? candidate : current,
    );
    const hosts = unique.map(({ host }) => host).sort((a, b) => a.localeCompare(b));
    issues.push({
      shared: true,
      hosts,
      severity: unique.some(({ condition }) => condition.severity === "critical") ? "critical" : "warning",
      priority: Math.max(...unique.map(({ condition }) => condition.priority)),
      messages: [`${hottest.condition.device} ${format(hottest.condition.usage)}% · 影响 ${hosts.length} 台`],
      categories: ["storage"],
      sortName: hottest.condition.device,
    });
  });
  view.snapshot.servers.forEach((server) => {
    const remaining = conditionsByHost.get(server.host).filter(
      (condition) => !consumed.has(`${server.host}|${condition.id}`),
    );
    const issue = issueFromConditions(server, remaining);
    if (issue) issues.push(issue);
  });
  return issues.sort((a, b) => {
    if (a.priority !== b.priority) return b.priority - a.priority;
    if (a.severity !== b.severity) return a.severity === "critical" ? -1 : 1;
    return a.sortName.localeCompare(b.sortName);
  });
}

function renderAttention() {
  if (view.selectedHost !== "all") {
    elements.attentionPanel.hidden = true;
    return;
  }
  const issues = attentionIssues();
  if (!issues.length) {
    view.attentionRenderKey = "empty";
    elements.attentionPanel.hidden = true;
    return;
  }
  const visibleIssues = view.attentionFilter === "all"
    ? issues
    : issues.filter((issue) => issue.categories.includes(view.attentionFilter));
  const renderKey = JSON.stringify({
    filter: view.attentionFilter,
    issues: issues.map((issue) => ({
      shared: Boolean(issue.shared),
      hosts: issue.hosts,
      severity: issue.severity,
      priority: issue.priority,
      messages: issue.messages,
      categories: issue.categories,
      sortName: issue.sortName,
    })),
  });
  if (renderKey === view.attentionRenderKey) {
    elements.attentionPanel.hidden = false;
    return;
  }
  view.attentionRenderKey = renderKey;
  const counts = {
    all: issues.length,
    connection: issues.filter((issue) => issue.categories.includes("connection")).length,
    storage: issues.filter((issue) => issue.categories.includes("storage")).length,
    compute: issues.filter((issue) => issue.categories.includes("compute")).length,
  };
  document.querySelectorAll(".attention-filter").forEach((button) => {
    const count = counts[button.dataset.attentionFilter];
    button.classList.toggle("active", button.dataset.attentionFilter === view.attentionFilter);
    button.disabled = count === 0;
    button.querySelector("span").textContent = count;
  });
  const critical = visibleIssues.filter((issue) => issue.severity === "critical").length;
  const affectedHosts = new Set(visibleIssues.flatMap((issue) => issue.hosts));
  elements.attentionSummary.textContent = view.attentionFilter === "all"
    ? `${affectedHosts.size} 台服务器 · ${issues.length} 个问题`
    : `${affectedHosts.size} 台服务器 · ${visibleIssues.length}/${issues.length} 个问题`;
  const fragment = document.createDocumentFragment();
  visibleIssues.forEach((issue) => {
    const item = create(issue.shared ? "article" : "button", `attention-item ${issue.severity}${issue.shared ? " shared" : ""}`);
    if (!issue.shared) item.type = "button";
    item.title = `${issue.shared ? "共享存储" : issue.server.host}：${issue.messages.join(" · ")}`;
    const heading = create("span", "attention-item-heading");
    heading.append(
      create("i"),
      create("strong", "", issue.shared ? "共享存储" : issue.server.host),
      create("em", "", issue.severity === "critical" ? "严重" : "警告"),
    );
    item.append(heading, create("span", "attention-message", issue.messages.join(" · ")));
    if (issue.shared) {
      const hosts = create("span", "attention-hosts");
      issue.hosts.forEach((host) => {
        const button = create("button", "attention-host", host);
        button.type = "button";
        button.addEventListener("click", () => selectHost(host));
        hosts.append(button);
      });
      item.append(hosts);
    } else {
      item.addEventListener("click", () => selectHost(issue.server.host));
    }
    fragment.append(item);
  });
  if (!visibleIssues.length) {
    fragment.append(create("div", "attention-empty", "当前类型没有活动问题"));
  }
  elements.attentionList.replaceChildren(fragment);
  elements.attentionStatus.textContent = critical
    ? `${critical} 个严重问题 · 点击可定位`
    : "点击问题可定位服务器";
  elements.attentionPanel.hidden = false;
}

function incidentStateLabel(state) {
  return {
    opened: "触发",
    resolved: "已恢复",
    escalated: "升级",
    deescalated: "已降级",
  }[state] || "变化";
}

function incidentDescription(event) {
  return event.state === "resolved"
    ? `${event.resource} 恢复正常`
    : incidentConditionMessage(event);
}

function renderIncidents() {
  if (!view.incidents) {
    elements.incidentPanel.hidden = true;
    return;
  }
  const events = view.incidents.events.filter(
    (event) => view.selectedHost === "all" || event.host === view.selectedHost,
  );
  if (!events.length) {
    view.incidentRenderKey = `${view.incidentVersion}:${view.selectedHost}:empty`;
    elements.incidentPanel.hidden = true;
    return;
  }
  const renderKey = `${view.incidentVersion}:${view.selectedHost}:${view.incidentExpanded}`;
  if (renderKey === view.incidentRenderKey) {
    elements.incidentPanel.hidden = false;
    return;
  }
  view.incidentRenderKey = renderKey;
  const visible = view.incidentExpanded ? events : events.slice(0, 6);
  elements.incidentSummary.textContent = view.selectedHost === "all"
    ? `${events.length} 条近期状态变化`
    : `${view.selectedHost} · ${events.length} 条近期变化`;
  elements.incidentToggle.hidden = events.length <= 6;
  elements.incidentToggle.textContent = view.incidentExpanded ? "收起" : "展开全部";
  const fragment = document.createDocumentFragment();
  visible.forEach((event) => {
    const stateClass = event.state === "resolved" || event.state === "deescalated"
      ? "resolved"
      : event.severity;
    const item = create("button", `incident-item ${stateClass}`);
    item.type = "button";
    item.title = `${event.host}：${incidentDescription(event)}`;
    const body = create("span", "incident-body");
    const title = create("span", "incident-title");
    title.append(
      create("strong", "", event.host),
      create("em", "", incidentStateLabel(event.state)),
    );
    body.append(title, create("span", "incident-message", incidentDescription(event)));
    const observedAge = create("span", "incident-time age-relative", age(event.observedAt));
    observedAge.dataset.ageAt = event.observedAt;
    item.append(
      create("i", "incident-dot"),
      body,
      observedAge,
    );
    item.addEventListener("click", () => selectHost(event.host));
    fragment.append(item);
  });
  elements.incidentList.replaceChildren(fragment);
  elements.incidentPanel.hidden = false;
}

function normalizeSelection() {
  if (view.selectedHost === "all" || !view.snapshot) return;
  if (view.snapshot.servers.some((server) => server.host === view.selectedHost)) return;
  view.selectedHost = "all";
  view.history = null;
  view.historyKey = "";
  window.history.replaceState(null, "", window.location.pathname);
}

function serverResources(server) {
  if (!server.system) return { warning: false, warningKind: null, warningUsage: 0, cpu: 0, memory: 0, swap: 0, disk: 0 };
  const threshold = limits();
  const cpu = numeric(server.system.cpu_usage_pct);
  const memoryPct = ratio(server.system.memory_used_mib, server.system.memory_total_mib);
  const swapPct = ratio(server.system.swap_used_mib, server.system.swap_total_mib);
  const diskPct = ratio(server.system.disk_used_mib, server.system.disk_total_mib);
  let warningKind = null;
  let warningUsage = 0;
  if (diskPct >= threshold.disk_warning_pct) [warningKind, warningUsage] = ["DISK", diskPct];
  else if (server.system.swap_total_mib > 0 && swapPct >= threshold.swap_warning_pct) [warningKind, warningUsage] = ["SWAP", swapPct];
  else if (memoryPct >= threshold.memory_warning_pct) [warningKind, warningUsage] = ["RAM", memoryPct];
  else if (cpu >= threshold.cpu_warning_pct) [warningKind, warningUsage] = ["CPU", cpu];
  return {
    warning: warningKind !== null,
    warningKind,
    warningUsage,
    cpu,
    memory: memoryPct,
    swap: swapPct,
    disk: diskPct,
  };
}

function setConnection(kind, label) {
  view.transportKind = kind;
  view.transportLabel = label;
  renderConnectionStatus();
}

function serverGpuUsage(server) {
  const values = server.gpus
    .map((gpu) => optionalMetric(gpu, "utilization_gpu_pct"))
    .filter((value) => Number.isFinite(value));
  if (!values.length) return NaN;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function serverUtilizationRow(label, value, kind, warning = false) {
  const row = create("div", `server-util-row ${kind}${warning ? " warning" : ""}`);
  const track = create("span", "server-util-track");
  const bar = create("i");
  bar.style.width = `${clamp(value)}%`;
  if (warning) bar.style.background = "var(--amber)";
  track.append(bar);
  row.append(
    create("span", "", label),
    track,
    create("strong", "", Number.isFinite(value) ? `${format(value)}%` : "—"),
  );
  return row;
}

function syncServerOrder(servers) {
  const hosts = servers.map((server) => server.host);
  const known = new Set(hosts);
  const order = view.serverOrder.filter((host) => known.has(host));
  const ordered = new Set(order);
  hosts.forEach((host) => {
    if (!ordered.has(host)) {
      order.push(host);
      ordered.add(host);
    }
  });
  view.serverOrder = order;
  return order;
}

function serverStatusRank(server) {
  if (server.status !== "online") return 0;
  if (serverResources(server).warning) return 1;
  return 2;
}

function orderedServers(servers) {
  const original = syncServerOrder(view.snapshot.servers);
  const order = new Map(original.map((host, index) => [host, index]));
  return servers.slice().sort((a, b) => {
    if (view.serverSort === "host") return a.host.localeCompare(b.host);
    if (view.serverSort === "status") {
      return serverStatusRank(a) - serverStatusRank(b)
        || a.host.localeCompare(b.host);
    }
    if (view.serverSort === "gpu") {
      return numeric(serverGpuUsage(b), -1) - numeric(serverGpuUsage(a), -1)
        || a.host.localeCompare(b.host);
    }
    if (view.serverSort === "cpu") {
      return numeric(b.system?.cpu_usage_pct, -1) - numeric(a.system?.cpu_usage_pct, -1)
        || a.host.localeCompare(b.host);
    }
    return numeric(order.get(a.host), Number.MAX_SAFE_INTEGER)
      - numeric(order.get(b.host), Number.MAX_SAFE_INTEGER);
  });
}

function reorderServer(source, target) {
  if (!source || !target || source === target || !view.snapshot) return;
  const order = syncServerOrder(view.snapshot.servers).filter((host) => host !== source);
  const targetIndex = order.indexOf(target);
  if (targetIndex < 0) return;
  order.splice(targetIndex, 0, source);
  view.serverOrder = order;
  view.serverSort = "custom";
  elements.serverSort.value = "custom";
  savePreferences();
  renderServers();
}

function enableServerDrag(item, host) {
  item.draggable = true;
  item.dataset.host = host;
  item.addEventListener("dragstart", (event) => {
    view.draggedHost = host;
    item.classList.add("dragging");
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", host);
    }
  });
  item.addEventListener("dragover", (event) => {
    if (!view.draggedHost || view.draggedHost === host) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    item.classList.add("drag-target");
  });
  item.addEventListener("dragleave", () => item.classList.remove("drag-target"));
  item.addEventListener("drop", (event) => {
    event.preventDefault();
    item.classList.remove("drag-target");
    view.suppressServerClick = true;
    reorderServer(
      view.draggedHost || event.dataTransfer?.getData("text/plain"),
      host,
    );
  });
  item.addEventListener("dragend", () => {
    item.classList.remove("dragging");
    elements.serverList.querySelectorAll(".drag-target").forEach(
      (node) => node.classList.remove("drag-target"),
    );
    view.draggedHost = null;
    setTimeout(() => { view.suppressServerClick = false; }, 0);
  });
}

function renderSummary() {
  const { snapshot } = view;
  if (!snapshot) return;
  const cpu = snapshot.stats.cpuAveragePct;
  const memoryPct = ratio(
    snapshot.stats.systemMemoryUsedMiB,
    snapshot.stats.systemMemoryTotalMiB,
  );
  const threshold = limits();
  const onlineRatio = ratio(snapshot.stats.onlineServers, snapshot.stats.servers);
  const gpuBusyRatio = ratio(snapshot.stats.busyGpus, snapshot.stats.gpus);
  const gpuMemoryPct = ratio(
    snapshot.stats.memoryUsedMiB,
    snapshot.stats.memoryTotalMiB,
  );
  const currentGpus = snapshot.servers
    .filter((server) => server.status === "online")
    .flatMap((server) => server.gpus);
  const hottestGpu = Math.max(
    -Infinity,
    ...currentGpus.map((gpu) => numeric(gpu.temperature_c, -Infinity)),
  );
  const serverCritical = snapshot.stats.servers > 0 && snapshot.stats.onlineServers === 0;
  const cpuCritical = cpu != null && cpu >= 95;
  const memoryCritical = memoryPct >= 95;
  const gpuCritical = hottestGpu >= threshold.gpu_temperature_warning_c + 5;

  elements.totalGpus.textContent = format(snapshot.stats.gpus);
  elements.activeGpus.textContent = format(snapshot.stats.busyGpus);
  elements.gpuMemoryPercent.textContent = `${format(gpuMemoryPct, 1)}%`;
  elements.gpuMemoryUsed.textContent = memory(snapshot.stats.memoryUsedMiB);
  elements.gpuMemoryTotal.textContent = `/ ${memory(snapshot.stats.memoryTotalMiB)}`;
  elements.gpuMemoryBar.style.width = `${clamp(gpuMemoryPct)}%`;
  elements.gpuMemoryCard.title = snapshot.stats.memoryTotalMiB > 0
    ? `已使用 ${memory(snapshot.stats.memoryUsedMiB)}，总计 ${memory(snapshot.stats.memoryTotalMiB)}`
    : "等待 GPU 显存样本";
  elements.serverRatio.textContent = `${snapshot.stats.onlineServers} / ${snapshot.stats.servers}`;
  elements.serverHealth.textContent = snapshot.stats.issueServers ? "需关注" : "健康";
  elements.serverHealth.classList.toggle("warning", snapshot.stats.issueServers > 0);
  elements.serverCard.classList.toggle("is-warning", snapshot.stats.issueServers > 0 && !serverCritical);
  elements.serverCard.classList.toggle("is-critical", serverCritical);
  elements.serverBar.style.width = `${clamp(onlineRatio)}%`;
  elements.serverDetail.textContent = snapshot.stats.issueServers
    ? `${snapshot.stats.issueServers} 台服务器异常`
    : "所有服务器运行正常";
  elements.averageCpu.textContent = cpu == null ? "—" : format(cpu, 1);
  elements.cpuHealth.textContent = cpu == null
    ? "采样中"
    : cpu >= threshold.cpu_warning_pct ? "高负载" : "正常";
  elements.cpuHealth.style.color = cpu != null && cpu >= threshold.cpu_warning_pct
    ? "var(--amber)"
    : "";
  elements.cpuCard.classList.toggle("is-warning", cpu != null && cpu >= threshold.cpu_warning_pct && !cpuCritical);
  elements.cpuCard.classList.toggle("is-critical", cpuCritical);
  elements.cpuBar.style.width = `${clamp(cpu)}%`;
  elements.cpuCores.textContent = format(snapshot.stats.cpuCores);
  elements.memoryPercent.textContent = `${format(memoryPct, 1)}%`;
  elements.memoryUsed.textContent = memory(snapshot.stats.systemMemoryUsedMiB);
  elements.memoryTotal.textContent = `/ ${memory(snapshot.stats.systemMemoryTotalMiB)}`;
  elements.memoryBar.style.width = `${clamp(memoryPct)}%`;
  elements.memoryCard.classList.toggle("is-warning", memoryPct >= threshold.memory_warning_pct && !memoryCritical);
  elements.memoryCard.classList.toggle("is-critical", memoryCritical);
  elements.gpuCard.classList.toggle("is-warning", hottestGpu >= threshold.gpu_temperature_warning_c && !gpuCritical);
  elements.gpuCard.classList.toggle("is-critical", gpuCritical);
  elements.gpuBar.style.width = `${clamp(gpuBusyRatio)}%`;
  elements.gpuCard.title = Number.isFinite(hottestGpu)
    ? `最高温度 ${format(hottestGpu, 1)}°C`
    : "等待 GPU 温度样本";
  elements.networkRx.textContent = rate(snapshot.stats.networkRxBps);
  elements.networkTx.textContent = rate(snapshot.stats.networkTxBps);
  elements.lastSync.textContent = age(snapshot.lastPollCompletedAt);
  elements.serverCount.textContent = snapshot.stats.servers;
  const cycleMilliseconds = snapshot.lastPollDurationMs;
  const cycleText = cycleMilliseconds == null
    ? "等待首轮完成"
    : `上轮 ${(numeric(cycleMilliseconds) / 1000).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} 秒`;
  const cycleSlow = cycleMilliseconds != null
    && numeric(cycleMilliseconds) > numeric(snapshot.pollIntervalSeconds) * 1000;
  const collectionDelayed = collectionHealth().state === "delayed";
  elements.pollInfo.textContent = `每 ${format(snapshot.pollIntervalSeconds)} 秒 · ${cycleText}${collectionDelayed ? " · 采集延迟" : ""} · v${snapshot.appVersion}`;
  elements.pollInfo.classList.toggle("warning", cycleSlow || collectionDelayed);
  elements.pollInfo.title = collectionDelayed
    ? "上轮采集完成时间已超过配置的新鲜度窗口"
    : cycleSlow ? "上一轮采集耗时超过目标轮询间隔" : "";
  elements.collectorError.hidden = !snapshot.collectorError;
  elements.collectorError.textContent = snapshot.collectorError || "";
  syncRefreshControl();
  renderConnectionStatus();
}

function serverItem(server, selectedHost) {
  const [label, stateClass] = serverStatus(server);
  const resources = serverResources(server);
  const item = create("button", `server-item${selectedHost === server.host ? " selected" : ""}`);
  item.type = "button";
  item.setAttribute("role", "option");
  item.setAttribute("aria-selected", String(selectedHost === server.host));
  item.title = [
    `${server.host} · ${label}`,
    server.lastSuccessAt ? `上次成功 ${age(server.lastSuccessAt)}` : "尚无成功样本",
    server.polling ? "正在重新探测" : server.nextRetryAt ? retryCountdown(server.nextRetryAt) : "",
  ].filter(Boolean).join(" · ");
  item.addEventListener("click", () => {
    if (view.suppressServerClick) return;
    selectHost(server.host);
  });
  enableServerDrag(item, server.host);

  const main = create("div", "server-main");
  const identity = create("div", "server-main");
  identity.append(create("i", `status-dot ${stateClass}`), create("span", "server-name", server.host));
  const gpuLabel = `${server.gpus.length} GPU${server.stale ? " · 历史" : ""}`;
  main.append(identity, create("span", "server-gpu-count", gpuLabel));

  const stats = create("div", "server-stats");
  const utilization = create("div", "server-utilization");
  const gpuUsage = serverGpuUsage(server);
  utilization.append(
    serverUtilizationRow(
      "GPU",
      gpuUsage,
      "gpu",
      Number.isFinite(gpuUsage) && gpuUsage >= 95,
    ),
    serverUtilizationRow(
      "CPU",
      optionalMetric(server.system || {}, "cpu_usage_pct"),
      "cpu",
      resources.cpu >= limits().cpu_warning_pct,
    ),
  );
  if (server.status === "online") {
    item.append(main, utilization);
    return item;
  } else {
    const detail = create("span", "issue-text");
    detail.append(failureText(server.message) || label);
    if (server.stale) {
      detail.append(" · ");
      const sampleAge = create("span", "age-relative", age(server.lastSuccessAt));
      sampleAge.dataset.ageAt = server.lastSuccessAt;
      detail.append(sampleAge);
    }
    stats.append(detail);
    if (server.polling) {
      stats.append(create("span", "polling-relative", "正在探测"));
    } else if (server.nextRetryAt) {
      const retry = create("span", "retry-relative", retryCountdown(server.nextRetryAt));
      retry.dataset.retryAt = server.nextRetryAt;
      stats.append(retry);
    } else if (server.consecutiveFailures > 1) {
      stats.append(create("span", "", `${server.consecutiveFailures} 次`));
    } else if (server.latencyMs != null) {
      stats.append(create("span", "", `${server.latencyMs} ms`));
    }
  }
  item.append(main, utilization, stats);
  return item;
}

function serverItemSignature(server) {
  const resources = serverResources(server);
  return JSON.stringify([
    server.status,
    server.stale,
    server.polling,
    server.latencyMs,
    server.message,
    server.lastSuccessAt,
    server.nextRetryAt,
    server.consecutiveFailures,
    server.gpus.length,
    serverGpuUsage(server),
    view.selectedHost === server.host,
    resources.warning,
    resources.warningKind,
    resources.warningUsage,
    resources.cpu,
    resources.memory,
  ]);
}

function cachedServerItem(server) {
  const signature = serverItemSignature(server);
  const cached = view.serverItemCache.get(server.host);
  if (cached?.signature === signature) return cached.node;
  const node = serverItem(server, view.selectedHost);
  view.serverItemCache.set(server.host, { signature, node });
  return node;
}

function fleetAllItem(label, gpuCount) {
  const signature = `${view.selectedHost}:${view.serverFilter}:${label}:${gpuCount}`;
  if (view.fleetAllCache?.signature === signature) return view.fleetAllCache.node;
  const item = create("button", `server-item${view.selectedHost === "all" ? " selected" : ""}`);
  item.type = "button";
  item.setAttribute("role", "option");
  item.setAttribute("aria-selected", String(view.selectedHost === "all"));
  item.addEventListener("click", () => selectHost("all"));
  const main = create("div", "server-main");
  const identity = create("div", "server-main");
  identity.append(
    create("i", "status-dot online"),
    create("span", "server-name", label),
  );
  main.append(identity, create("span", "server-gpu-count", `${gpuCount} GPU`));
  item.append(main);
  view.fleetAllCache = { signature, node: item };
  return item;
}

function renderServers() {
  if (!view.snapshot) return;
  const servers = orderedServers(focusedServers(view.snapshot.servers));
  elements.serverCount.textContent = view.serverFilter === "all"
    ? String(view.snapshot.stats.servers)
    : `${servers.length}/${view.snapshot.stats.servers}`;
  const gpuCount = servers.reduce((sum, server) => sum + server.gpus.length, 0);
  const desired = [
    fleetAllItem(SERVER_FILTER_LABELS[view.serverFilter], gpuCount),
    ...servers.map(cachedServerItem),
  ];
  if (!servers.length) {
    view.fleetEmptyNode ||= create("div", "fleet-empty", "当前筛选没有匹配节点");
    desired.push(view.fleetEmptyNode);
  }
  const knownHosts = new Set(view.snapshot.servers.map((server) => server.host));
  [...view.serverItemCache.keys()].forEach((host) => {
    if (!knownHosts.has(host)) view.serverItemCache.delete(host);
  });
  reconcileChildren(elements.serverList, desired);
}

function resourceTile(label, value, meta, usage = null, threshold = 101) {
  const tile = create("article", "resource-tile");
  const heading = create("div", "resource-tile-label");
  const indicator = create("i");
  if (usage != null && usage >= threshold) {
    indicator.className = usage >= 95 ? "critical" : "warning";
  }
  heading.append(create("span", "", label), indicator);
  tile.append(
    heading,
    create("strong", "resource-tile-value", value),
    create("span", "resource-tile-meta", meta),
  );
  if (usage != null) {
    const track = create("div", "mini-track");
    const bar = create("i");
    bar.style.width = `${clamp(usage)}%`;
    if (usage >= threshold) bar.style.background = usage >= 95 ? "var(--red)" : "var(--amber)";
    track.append(bar);
    tile.append(track);
  }
  return tile;
}

function renderResources() {
  const selectedServers = view.selectedHost === "all"
    ? focusedServers(view.snapshot.servers).filter((server) => server.status === "online" && server.system)
    : view.snapshot.servers.filter((server) => server.host === view.selectedHost && server.system);
  const systems = selectedServers.map((server) => server.system);
  if (!systems.length) {
    elements.resourceGrid.replaceChildren(
      ...["CPU", "内存", "Swap", "磁盘", "磁盘 I/O", "网络", "运行时间"].map((label) =>
        resourceTile(label, "—", "暂无有效样本")),
    );
    elements.diskList.hidden = true;
    return;
  }

  const cpuValues = systems
    .map((system) => system.cpu_usage_pct)
    .filter((value) => value != null);
  const cpu = cpuValues.length
    ? cpuValues.reduce((sum, value) => sum + numeric(value), 0) / cpuValues.length
    : null;
  const cores = systems.reduce((sum, system) => sum + numeric(system.cpu_cores), 0);
  const memoryTotal = systems.reduce((sum, system) => sum + numeric(system.memory_total_mib), 0);
  const memoryUsed = systems.reduce((sum, system) => sum + numeric(system.memory_used_mib), 0);
  const swapTotal = systems.reduce((sum, system) => sum + numeric(system.swap_total_mib), 0);
  const swapUsed = systems.reduce((sum, system) => sum + numeric(system.swap_used_mib), 0);
  const diskTotal = systems.reduce((sum, system) => sum + numeric(system.disk_total_mib), 0);
  const diskUsed = systems.reduce((sum, system) => sum + numeric(system.disk_used_mib), 0);
  const rx = systems.reduce((sum, system) => sum + numeric(system.network_rx_bps), 0);
  const tx = systems.reduce((sum, system) => sum + numeric(system.network_tx_bps), 0);
  const diskRead = systems.reduce((sum, system) => sum + numeric(system.disk_read_bps), 0);
  const diskWrite = systems.reduce((sum, system) => sum + numeric(system.disk_write_bps), 0);
  const diskIoAvailable = systems.some((system) => system.disk_read_bps != null || system.disk_write_bps != null);
  const networkAvailable = systems.some((system) => system.network_rx_bps != null || system.network_tx_bps != null);
  const load = systems.reduce((sum, system) => sum + numeric(system.load_1m), 0);
  const memoryPct = ratio(memoryUsed, memoryTotal);
  const swapPct = ratio(swapUsed, swapTotal);
  const diskPct = ratio(diskUsed, diskTotal);
  const threshold = limits();
  const selectedSystem = systems.length === 1 ? systems[0] : null;

  elements.resourceGrid.replaceChildren(
    resourceTile(
      "CPU",
      cpu == null ? "采样中" : `${format(cpu, 1)}%`,
      `${format(cores)} 核 · Load ${format(load, 2)}`,
      cpu,
      threshold.cpu_warning_pct,
    ),
    resourceTile(
      "内存",
      `${format(memoryPct, 1)}%`,
      `${memory(memoryUsed)} / ${memory(memoryTotal)}`,
      memoryPct,
      threshold.memory_warning_pct,
    ),
    resourceTile(
      "Swap",
      swapTotal ? `${format(swapPct, 1)}%` : "未配置",
      swapTotal ? `${memory(swapUsed)} / ${memory(swapTotal)}` : "无需交换空间",
      swapTotal ? swapPct : null,
      threshold.swap_warning_pct,
    ),
    resourceTile(
      "磁盘",
      `${format(diskPct, 1)}%`,
      `${storage(diskUsed)} / ${storage(diskTotal)}`,
      diskPct,
      threshold.disk_warning_pct,
    ),
    resourceTile(
      "磁盘 I/O",
      diskIoAvailable ? `R ${rate(diskRead)}` : "采样中",
      diskIoAvailable ? `W ${rate(diskWrite)}` : "等待相邻样本",
    ),
    resourceTile(
      "网络",
      networkAvailable ? `↓ ${rate(rx)}` : "采样中",
      networkAvailable ? `↑ ${rate(tx)}` : "等待相邻样本",
    ),
    resourceTile(
      "运行时间",
      selectedSystem ? duration(selectedSystem.uptime_seconds) : `${systems.length} 台在线`,
      selectedSystem
        ? `Load ${format(selectedSystem.load_1m, 2)} / ${format(selectedSystem.load_5m, 2)} / ${format(selectedSystem.load_15m, 2)}`
        : `总计 ${format(cores)} 个逻辑核心`,
    ),
  );
  renderDisks(selectedSystem);
}

function heatmapMetric(gpu) {
  if (view.heatMetric === "memory") {
    const value = numeric(gpu.memory_total_mib) > 0
      ? ratio(gpu.memory_used_mib, gpu.memory_total_mib)
      : NaN;
    return { value, level: value, label: Number.isFinite(value) ? `${format(value)}%` : "—" };
  }
  if (view.heatMetric === "temperature") {
    const value = gpu.temperature_c == null ? NaN : numeric(gpu.temperature_c, NaN);
    return {
      value,
      level: Number.isFinite(value) ? clamp(((value - 30) / 60) * 100) : 0,
      label: Number.isFinite(value) ? `${format(value)}°` : "—",
    };
  }
  const value = gpu.utilization_gpu_pct == null
    ? NaN
    : numeric(gpu.utilization_gpu_pct, NaN);
  return { value, level: value, label: Number.isFinite(value) ? `${format(value)}%` : "—" };
}

function heatmapAxis(columns) {
  if (view.heatmapAxisCache?.columns === columns) return view.heatmapAxisCache.node;
  const axis = create("div", "heatmap-axis");
  axis.style.setProperty("--heat-columns", columns);
  axis.append(create("span", "heatmap-axis-host", "节点 / GPU"));
  for (let index = 0; index < columns; index += 1) {
    axis.append(create("span", "", `#${index}`));
  }
  view.heatmapAxisCache = { columns, node: axis };
  return axis;
}

function heatmapTile(server, gpu) {
  const metric = heatmapMetric(gpu);
  const threshold = limits().gpu_temperature_warning_c;
  const temperatureClass = view.heatMetric === "temperature" && Number.isFinite(metric.value)
    ? metric.value >= threshold + 5 ? " critical" : metric.value >= threshold ? " warning" : ""
    : "";
  const unavailable = Number.isFinite(metric.value) ? "" : " unavailable";
  const tile = create(
    "button",
    `heatmap-cell ${view.heatMetric}${temperatureClass}${unavailable}`,
  );
  tile.type = "button";
  tile.style.setProperty("--heat-level", String(0.07 + (clamp(metric.level) / 100) * 0.63));
  tile.title = `${server.host} · GPU ${gpu.index} · ${gpu.name} · ${metric.label}`;
  tile.setAttribute("aria-label", tile.title);
  tile.append(
    create("small", "", `#${gpu.index}`),
    create("strong", "", metric.label),
  );
  tile.addEventListener("click", () => openGpuDetail(server, gpu));
  return tile;
}

function heatmapRow(server, columns) {
  const row = create("div", "heatmap-row");
  row.style.setProperty("--heat-columns", columns);
  const host = create("button", "heatmap-host", server.host);
  host.type = "button";
  host.title = `查看 ${server.host}`;
  host.addEventListener("click", () => selectHost(server.host));
  row.append(host);
  const byIndex = new Map(server.gpus.map((gpu) => [numeric(gpu.index), gpu]));
  for (let index = 0; index < columns; index += 1) {
    const gpu = byIndex.get(index);
    row.append(gpu ? heatmapTile(server, gpu) : create("span", "heatmap-cell placeholder"));
  }
  return row;
}

function cachedHeatmapRow(server, columns) {
  const signature = JSON.stringify({
    metric: view.heatMetric,
    columns,
    threshold: limits().gpu_temperature_warning_c,
    gpus: server.gpus.map((gpu) => ({
      index: gpu.index,
      name: gpu.name,
      utilization: gpu.utilization_gpu_pct,
      memoryUsed: gpu.memory_used_mib,
      memoryTotal: gpu.memory_total_mib,
      temperature: gpu.temperature_c,
    })),
  });
  const cached = view.heatmapCache.get(server.host);
  if (cached?.signature === signature) return cached.node;
  const node = heatmapRow(server, columns);
  view.heatmapCache.set(server.host, { signature, node });
  return node;
}

function renderHeatmap() {
  const servers = view.selectedHost === "all"
    ? focusedServers(view.snapshot.servers)
      .filter((server) => server.status === "online" && server.gpus.length)
      .sort((a, b) => a.host.localeCompare(b.host))
    : [];
  if (!servers.length) {
    elements.gpuHeatmap.hidden = true;
    return;
  }
  const columns = Math.max(
    1,
    ...servers.flatMap((server) => server.gpus.map((gpu) => numeric(gpu.index) + 1)),
  );
  const activeHosts = new Set(servers.map((server) => server.host));
  [...view.heatmapCache.keys()].forEach((host) => {
    if (!activeHosts.has(host)) view.heatmapCache.delete(host);
  });
  document.querySelectorAll(".heatmap-mode").forEach((button) => {
    button.classList.toggle("active", button.dataset.heatMetric === view.heatMetric);
  });
  reconcileChildren(elements.heatmapGrid, [
    heatmapAxis(columns),
    ...servers.map((server) => cachedHeatmapRow(server, columns)),
  ]);
  elements.gpuHeatmap.dataset.metric = view.heatMetric;
  elements.gpuHeatmap.hidden = false;
}

function renderNodeNotice() {
  const selected = view.selectedHost === "all"
    ? null
    : view.snapshot.servers.find((server) => server.host === view.selectedHost);
  if (!selected) {
    elements.nodeNotice.hidden = true;
    elements.nodeFreshness.hidden = true;
    return;
  }
  elements.nodeFreshness.className = "freshness-badge";
  elements.nodeFreshness.replaceChildren();
  if (selected.status === "online") {
    elements.nodeFreshness.append(selected.polling ? "采集中 · " : "实时 · ");
    const freshAge = create("span", "age-relative", age(selected.lastAttemptAt));
    freshAge.dataset.ageAt = selected.lastAttemptAt || "";
    elements.nodeFreshness.append(freshAge);
  } else if (selected.polling) {
    elements.nodeFreshness.classList.add("warning");
    elements.nodeFreshness.textContent = "重新探测中";
  } else if (selected.stale) {
    elements.nodeFreshness.classList.add("warning");
    elements.nodeFreshness.append("历史 · ");
    const staleAge = create("span", "age-relative", age(selected.lastSuccessAt));
    staleAge.dataset.ageAt = selected.lastSuccessAt;
    elements.nodeFreshness.append(staleAge);
  } else {
    elements.nodeFreshness.classList.add("warning");
    elements.nodeFreshness.textContent = "等待有效数据";
  }
  elements.nodeFreshness.hidden = false;
  elements.nodeNotice.className = "node-notice";
  elements.nodeNotice.replaceChildren();
  if (selected.stale) {
    elements.nodeNotice.classList.add("warning");
    elements.nodeNotice.append(`${failureText(selected.message)}。当前展示 `);
    const sampleAge = create("span", "age-relative", age(selected.lastSuccessAt));
    sampleAge.dataset.ageAt = selected.lastSuccessAt;
    elements.nodeNotice.append(
      sampleAge,
      ` 的最后成功数据，已连续失败 ${selected.consecutiveFailures} 次；这些数据不会计入集群实时汇总。`,
    );
    if (selected.polling) {
      elements.nodeNotice.append(" 正在重新探测。");
    } else if (selected.nextRetryAt) {
      const retry = create("span", "retry-relative notice-retry", retryCountdown(selected.nextRetryAt));
      retry.dataset.retryAt = selected.nextRetryAt;
      elements.nodeNotice.append(" ", retry, "。");
    }
    elements.nodeNotice.hidden = false;
  } else if (selected.status !== "online") {
    elements.nodeNotice.classList.add("warning");
    elements.nodeNotice.append(`${failureText(selected.message)}。该节点还没有可展示的成功样本。`);
    if (selected.polling) {
      elements.nodeNotice.append(" 正在重新探测。");
    } else if (selected.nextRetryAt) {
      const retry = create("span", "retry-relative notice-retry", retryCountdown(selected.nextRetryAt));
      retry.dataset.retryAt = selected.nextRetryAt;
      elements.nodeNotice.append(" ", retry, "。");
    }
    elements.nodeNotice.hidden = false;
  } else if (selected.message) {
    elements.nodeNotice.classList.add("info");
    elements.nodeNotice.textContent = failureText(selected.message);
    elements.nodeNotice.hidden = false;
  } else {
    elements.nodeNotice.hidden = true;
  }
}

function historyDuration(points) {
  if (points.length < 2) return `${points.length} 个样本`;
  const elapsed = Math.max(0, Date.parse(points.at(-1).observedAt) - Date.parse(points[0].observedAt));
  const minutes = Math.max(1, Math.round(elapsed / 60000));
  return `${points.length} 个样本 · 最近 ${minutes} 分钟`;
}

function sparkline(points, accessor, color, maximum = null) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 220 54");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  const values = points.map(accessor);
  const finite = values.filter((value) => Number.isFinite(value));
  const ceiling = maximum ?? Math.max(1, ...finite);
  const coordinates = values.flatMap((value, index) => {
    if (!Number.isFinite(value)) return [];
    const x = values.length > 1 ? (index / (values.length - 1)) * 220 : 220;
    const y = 50 - Math.min(1, Math.max(0, value / ceiling)) * 44;
    return [`${x.toFixed(1)},${y.toFixed(1)}`];
  });
  const baseline = document.createElementNS(namespace, "line");
  baseline.setAttribute("x1", "0");
  baseline.setAttribute("x2", "220");
  baseline.setAttribute("y1", "50");
  baseline.setAttribute("y2", "50");
  baseline.setAttribute("class", "chart-baseline");
  const line = document.createElementNS(namespace, "polyline");
  line.setAttribute("points", coordinates.join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", color);
  line.setAttribute("stroke-width", "2");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(baseline, line);
  return svg;
}

function trendCard(label, points, accessor, formatter, color, maximum = null) {
  const card = create("article", "trend-card");
  const values = points.map(accessor).filter((value) => Number.isFinite(value));
  const current = values.at(-1);
  const top = create("div", "trend-card-top");
  top.append(
    create("span", "", label),
    create("strong", "", current == null ? "—" : formatter(current)),
  );
  card.append(top, sparkline(points, accessor, color, maximum));
  const peak = values.length ? Math.max(...values) : null;
  card.append(create("span", "trend-card-foot", peak == null ? "等待有效速率" : `峰值 ${formatter(peak)}`));
  return card;
}

function renderTrends() {
  if (view.selectedHost === "all") {
    elements.trendPanel.hidden = true;
    view.trendRenderKey = "all";
    return;
  }
  elements.trendPanel.hidden = false;
  const points = view.history?.points || [];
  const latestPoint = points.at(-1)?.observedAt || "none";
  const renderKey = `${view.selectedHost}:${view.historyLoading}:${points.length}:${latestPoint}`;
  if (renderKey === view.trendRenderKey) return;
  view.trendRenderKey = renderKey;
  if (view.historyLoading && !view.history) {
    elements.trendRange.textContent = "正在读取历史";
    elements.trendGrid.replaceChildren(create("div", "trend-empty", "正在收集和加载趋势样本…"));
    return;
  }
  elements.trendRange.textContent = historyDuration(points);
  if (!points.length) {
    elements.trendGrid.replaceChildren(create("div", "trend-empty", "首次成功采集后将显示趋势"));
    return;
  }
  elements.trendGrid.replaceChildren(
    trendCard("CPU", points, (point) => optionalMetric(point, "cpuUsagePct"), (value) => `${format(value, 1)}%`, "#6d8cff", 100),
    trendCard("内存", points, (point) => optionalMetric(point, "memoryUsagePct"), (value) => `${format(value, 1)}%`, "#5de0a0", 100),
    trendCard("GPU 平均负载", points, (point) => optionalMetric(point, "gpuUsagePct"), (value) => `${format(value, 1)}%`, "#b68cff", 100),
    trendCard("网络总速率", points, (point) => combinedMetric(point, "networkRxBps", "networkTxBps"), rate, "#53b8dc"),
    trendCard("磁盘总 I/O", points, (point) => combinedMetric(point, "diskReadBps", "diskWriteBps"), rate, "#f5b95f"),
  );
}

async function syncHistory() {
  if (!view.snapshot || view.selectedHost === "all") return;
  const server = view.snapshot.servers.find((item) => item.host === view.selectedHost);
  if (!server) return;
  const key = `${server.host}:${server.lastSuccessAt || "pending"}`;
  if (key === view.historyKey) return;
  view.historyKey = key;
  view.historyLoading = true;
  renderTrends();
  const request = ++view.historyRequest;
  try {
    const response = await fetch(`/api/history?host=${encodeURIComponent(server.host)}&limit=120`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const history = await response.json();
    if (request !== view.historyRequest || view.selectedHost !== history.host) return;
    view.history = history;
  } catch (_error) {
    if (request === view.historyRequest) view.history = { points: [] };
  } finally {
    if (request === view.historyRequest) {
      view.historyLoading = false;
      renderTrends();
    }
  }
}

function renderDisks(system) {
  if (!system || !system.disks?.length) {
    elements.diskList.hidden = true;
    elements.diskList.replaceChildren();
    return;
  }
  const threshold = limits().disk_warning_pct;
  const title = create("div", "disk-list-title");
  title.append(create("span", "", "磁盘挂载"), create("span", "", `${system.disks.length} 个文件系统`));
  const items = create("div", "disk-items");
  system.disks
    .slice()
    .sort((a, b) => numeric(b.used_pct) - numeric(a.used_pct))
    .forEach((disk) => {
      const severity = disk.used_pct >= 95 ? "critical" : disk.used_pct >= threshold ? "warning" : "";
      const item = create("article", `disk-item ${severity}`.trim());
      const head = create("div", "disk-item-head");
      head.append(create("strong", "", disk.mountpoint), create("span", "", `${format(disk.used_pct)}%`));
      const meta = create("div", "disk-item-meta");
      meta.append(create("span", "", disk.device), create("span", "", `${storage(disk.used_mib)} / ${storage(disk.total_mib)}`));
      const track = create("div", "mini-track");
      const bar = create("i");
      bar.style.width = `${clamp(disk.used_pct)}%`;
      if (severity) bar.style.background = severity === "critical" ? "var(--red)" : "var(--amber)";
      track.append(bar);
      item.append(head, meta, track);
      items.append(item);
    });
  elements.diskList.replaceChildren(title, items);
  elements.diskList.hidden = false;
}

function miniMetric(value, suffix, usage, className = "") {
  const wrapper = create("div", className);
  const label = create("div", "cell-value");
  label.append(create("span", "", value), create("span", "", suffix));
  const track = create("div", "mini-track");
  const bar = create("i");
  bar.style.width = `${clamp(usage)}%`;
  track.append(bar);
  wrapper.append(label, track);
  return wrapper;
}

function gpuState(gpu, server) {
  if (server.stale) return ["历史数据", "stale"];
  const identity = String(gpu.uuid || gpu.index);
  const activeConditions = Array.isArray(view.incidents?.active)
    ? view.incidents.active : [];
  const gpuConditions = activeConditions.filter(
    (condition) => condition.host === server.host
      && String(condition.conditionKey || "").endsWith(`:${identity}`),
  );
  if (gpuConditions.some((condition) => [
    "gpu_ecc", "gpu_memory_repair", "gpu_slowdown",
  ].includes(condition.category))) return ["硬件异常", "critical"];
  if (gpuConditions.some((condition) => condition.category === "gpu_memory")) {
    return ["显存压力", "hot"];
  }
  if (gpuConditions.some((condition) => condition.category === "gpu_idle_memory")) {
    return ["显存待释放", "hot"];
  }
  const utilization = numeric(gpu.utilization_gpu_pct);
  const temperature = numeric(gpu.temperature_c);
  const threshold = limits();
  if (temperature >= threshold.gpu_temperature_warning_c) return ["高温", "hot"];
  if (utilization >= threshold.gpu_busy_pct) return ["运行中", "busy"];
  return ["空闲", "idle"];
}

function gpuDetailMetric(label, value, title = "") {
  const metric = create("article", "gpu-detail-metric");
  const content = create("strong", "", value);
  if (title) content.title = title;
  metric.append(create("span", "", label), content);
  return metric;
}

function gpuProcessName(process) {
  const fullName = String(process.name || "unknown process");
  return fullName.replaceAll("\\", "/").split("/").at(-1) || fullName;
}

function gpuHealthSummary(health) {
  if (!health) return ["数据不可用", "本轮附加健康查询未返回数据"];
  const issues = [];
  if (numeric(health.ecc_uncorrected_volatile) > 0) {
    issues.push(`${format(health.ecc_uncorrected_volatile)} 个未纠正 ECC 错误`);
  }
  if (health.retired_pages_pending) issues.push("待退休显存页");
  if (health.remapped_rows_pending) issues.push("待重映射显存行");
  if (health.thermal_slowdown) issues.push("热降频");
  if (health.power_brake_slowdown) issues.push("功率制动降频");
  return issues.length ? ["需要关注", issues.join(" · ")] : ["正常", "未检测到硬件健康异常"];
}

function selectedGpuRecord() {
  if (!view.snapshot || !view.selectedGpu) return null;
  const server = view.snapshot.servers.find(
    (candidate) => candidate.host === view.selectedGpu.host,
  );
  const gpu = server?.gpus.find(
    (candidate) => String(candidate.uuid || candidate.index) === view.selectedGpu.key,
  );
  return server && gpu ? { server, gpu } : null;
}

function renderGpuDetail() {
  if (!elements.gpuDetailDialog.open) return;
  const record = selectedGpuRecord();
  if (!record) {
    elements.gpuDetailDialog.close();
    return;
  }
  const { server, gpu } = record;
  const [state] = gpuState(gpu, server);
  const memoryPct = ratio(gpu.memory_used_mib, gpu.memory_total_mib);
  const [healthState, healthDetail] = gpuHealthSummary(gpu.health);
  const processes = Array.isArray(gpu.processes) ? gpu.processes.slice() : [];
  processes.sort((a, b) => numeric(b.used_memory_mib, -1) - numeric(a.used_memory_mib, -1)
    || numeric(a.pid) - numeric(b.pid));

  elements.gpuDetailHost.textContent = `${server.host} · GPU ${gpu.index}`;
  elements.gpuDetailTitle.textContent = gpu.name || "Unknown NVIDIA GPU";
  elements.gpuDetailState.textContent = [
    state,
    server.stale ? "历史样本" : "实时样本",
    gpu.uuid || "No UUID",
    `Driver ${gpu.driver_version || "—"}`,
  ].join(" · ");
  elements.gpuDetailMetrics.replaceChildren(
    gpuDetailMetric("GPU 负载", `${format(gpu.utilization_gpu_pct)}%`),
    gpuDetailMetric("显存", `${memory(gpu.memory_used_mib)} / ${memory(gpu.memory_total_mib)}`),
    gpuDetailMetric("温度", gpu.temperature_c == null ? "—" : `${format(gpu.temperature_c)}°C`),
    gpuDetailMetric("功耗", gpu.power_draw_w == null ? "—" : `${format(gpu.power_draw_w)} W`),
    gpuDetailMetric("P-State", gpu.pstate || "—"),
    gpuDetailMetric("显存占用率", `${format(memoryPct, 1)}%`),
    gpuDetailMetric("硬件健康", healthState, healthDetail),
    gpuDetailMetric("MIG 模式", gpu.health?.mig_mode || "—"),
  );
  elements.gpuTaskCount.textContent = String(processes.length);
  if (gpu.processes_available === false) {
    elements.gpuTaskList.replaceChildren(
      create("div", "gpu-task-empty", "任务数据暂不可用；GPU 指标仍会继续刷新。"),
    );
    return;
  }
  if (!processes.length) {
    elements.gpuTaskList.replaceChildren(
      create("div", "gpu-task-empty", "当前没有活跃的 CUDA 计算进程。"),
    );
    return;
  }
  elements.gpuTaskList.replaceChildren(...processes.map((process) => {
    const item = create("article", "gpu-task");
    const name = create("strong", "gpu-task-name", gpuProcessName(process));
    name.title = process.name || "unknown process";
    const used = process.used_memory_mib == null ? "显存未知" : memory(process.used_memory_mib);
    const usage = ratio(process.used_memory_mib, gpu.memory_total_mib);
    const track = create("div", "mini-track");
    const bar = create("i");
    bar.style.width = `${clamp(usage)}%`;
    track.append(bar);
    const meta = create("div", "gpu-task-meta");
    meta.append(create("span", "", `PID ${process.pid}`), track);
    item.append(name, create("span", "gpu-task-memory", used), meta);
    return item;
  }));
}

function openGpuDetail(server, gpu) {
  view.selectedGpu = {
    host: server.host,
    key: String(gpu.uuid || gpu.index),
  };
  if (elements.settingsDialog.open) elements.settingsDialog.close();
  if (!elements.gpuDetailDialog.open) elements.gpuDetailDialog.showModal();
  renderGpuDetail();
}

function tableRow(record, grouped = false) {
  const { server, gpu } = record;
  const row = document.createElement("tr");
  const deviceCell = document.createElement("td");
  const device = create("div", "device-cell");
  device.append(create("span", "gpu-index", String(gpu.index)));
  const deviceText = create("div", "device-text");
  deviceText.append(create("strong", "", grouped ? `GPU ${gpu.index}` : server.host));
  deviceText.append(create("span", "", gpu.uuid ? gpu.uuid.slice(-12) : "No UUID"));
  device.append(deviceText);
  deviceCell.append(device);

  const modelCell = document.createElement("td");
  const model = create("div", "model-text");
  model.append(create("strong", "", gpu.name || "Unknown NVIDIA GPU"));
  model.append(create("span", "", `${gpu.pstate || "P?"} · Driver ${gpu.driver_version || "—"}`));
  modelCell.append(model);

  const utilCell = document.createElement("td");
  utilCell.append(miniMetric(`${format(gpu.utilization_gpu_pct)}%`, "GPU", gpu.utilization_gpu_pct));

  const memoryPct = ratio(gpu.memory_used_mib, gpu.memory_total_mib);
  const memoryCell = document.createElement("td");
  memoryCell.append(miniMetric(
    memory(gpu.memory_used_mib),
    memory(gpu.memory_total_mib),
    memoryPct,
    "memory-cell",
  ));

  const temperatureCell = document.createElement("td");
  temperatureCell.className = "gpu-col-temperature";
  const temperature = numeric(gpu.temperature_c, NaN);
  const warningTemperature = limits().gpu_temperature_warning_c;
  const temperatureClass = temperature >= warningTemperature + 5
    ? "critical"
    : temperature >= warningTemperature ? "hot" : "";
  temperatureCell.append(create(
    "span",
    `temperature ${temperatureClass}`.trim(),
    Number.isFinite(temperature) ? `${format(temperature)}°C` : "—",
  ));

  const powerCell = document.createElement("td");
  powerCell.className = "gpu-col-power";
  powerCell.append(create("span", "power", gpu.power_draw_w == null ? "—" : `${format(gpu.power_draw_w)} W`));

  const statusCell = document.createElement("td");
  const [status, statusClass] = gpuState(gpu, server);
  const pill = create("span", `state-pill ${statusClass}`);
  pill.append(create("i"), create("span", "", status));
  statusCell.append(pill);
  row.append(deviceCell, modelCell, utilCell, memoryCell, temperatureCell, powerCell, statusCell);
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.setAttribute("aria-label", `查看 ${server.host} GPU ${gpu.index} 的任务详情`);
  row.addEventListener("click", () => openGpuDetail(server, gpu));
  row.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openGpuDetail(server, gpu);
  });
  return row;
}

function filteredRecords() {
  const query = view.query.trim().toLocaleLowerCase("zh-CN");
  return allGpuRecords().filter(({ server, gpu }) => {
    if (view.selectedHost !== "all" && server.host !== view.selectedHost) return false;
    const utilization = numeric(gpu.utilization_gpu_pct);
    const temperature = numeric(gpu.temperature_c);
    const threshold = limits();
    if (view.filter === "busy" && utilization < threshold.gpu_busy_pct) return false;
    if (view.filter === "idle" && utilization >= threshold.gpu_busy_pct) return false;
    if (view.filter === "hot" && temperature < threshold.gpu_temperature_warning_c) return false;
    if (!query) return true;
    return `${server.host} ${gpu.name || ""} ${gpu.uuid || ""}`
      .toLocaleLowerCase("zh-CN")
      .includes(query);
  });
}

function gpuSortValue(gpu) {
  if (view.sort === "memory") return ratio(gpu.memory_used_mib, gpu.memory_total_mib);
  if (view.sort === "temperature") return numeric(gpu.temperature_c, -1);
  if (view.sort === "power") return numeric(gpu.power_draw_w, -1);
  return numeric(gpu.utilization_gpu_pct, -1);
}

function sortedRecords(records) {
  return records.slice().sort((a, b) => {
    if (view.sort === "host") return numeric(a.gpu.index) - numeric(b.gpu.index);
    const difference = gpuSortValue(b.gpu) - gpuSortValue(a.gpu);
    return difference || numeric(a.gpu.index) - numeric(b.gpu.index);
  });
}

function visibleOrderedRecords() {
  const records = filteredRecords();
  if (view.selectedHost !== "all") return sortedRecords(records);
  return groupedRecords(records).flatMap((group) => group.records);
}

function csvCell(value) {
  const raw = value == null ? "" : String(value);
  const safe = /^[\s\u0000-\u001F]*[=+\-@]/.test(raw) ? `'${raw}` : raw;
  return `"${safe.replaceAll('"', '""')}"`;
}

function buildCsv(records) {
  const columns = [
    ["主机", ({ server }) => server.host],
    ["GPU Index", ({ gpu }) => gpu.index],
    ["UUID", ({ gpu }) => gpu.uuid],
    ["型号", ({ gpu }) => gpu.name],
    ["驱动", ({ gpu }) => gpu.driver_version],
    ["P-State", ({ gpu }) => gpu.pstate],
    ["GPU 利用率 %", ({ gpu }) => gpu.utilization_gpu_pct],
    ["显存利用率 %", ({ gpu }) => ratio(gpu.memory_used_mib, gpu.memory_total_mib).toFixed(2)],
    ["显存已用 MiB", ({ gpu }) => gpu.memory_used_mib],
    ["显存总量 MiB", ({ gpu }) => gpu.memory_total_mib],
    ["温度 °C", ({ gpu }) => gpu.temperature_c],
    ["功耗 W", ({ gpu }) => gpu.power_draw_w],
    ["功耗上限 W", ({ gpu }) => gpu.power_limit_w],
    ["CPU 利用率 %", ({ server }) => server.system?.cpu_usage_pct],
    ["系统内存利用率 %", ({ server }) => ratio(server.system?.memory_used_mib, server.system?.memory_total_mib).toFixed(2)],
    ["GPU 状态", ({ server, gpu }) => gpuState(gpu, server)[0]],
    ["采样时间", ({ server }) => server.lastAttemptAt],
  ];
  const lines = [columns.map(([label]) => csvCell(label)).join(",")];
  records.forEach((record) => {
    lines.push(columns.map(([, getter]) => csvCell(getter(record))).join(","));
  });
  return `\uFEFF${lines.join("\r\n")}\r\n`;
}

function exportVisibleCsv() {
  const records = visibleOrderedRecords();
  if (!records.length) return;
  const blob = new Blob([buildCsv(records)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const now = new Date();
  const date = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
  anchor.href = url;
  anchor.download = `gpu-monitor-${view.selectedHost}-${date}.csv`;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function groupedRecords(records) {
  const groups = new Map();
  records.forEach((record) => {
    if (!groups.has(record.server.host)) groups.set(record.server.host, []);
    groups.get(record.server.host).push(record);
  });
  return [...groups.entries()]
    .map(([host, items]) => ({ host, server: items[0].server, records: sortedRecords(items) }))
    .sort((a, b) => {
      if (view.sort === "host") return a.host.localeCompare(b.host);
      const aScore = Math.max(...a.records.map((record) => gpuSortValue(record.gpu)));
      const bScore = Math.max(...b.records.map((record) => gpuSortValue(record.gpu)));
      return bScore - aScore || a.host.localeCompare(b.host);
    });
}

function gpuTable(records, grouped = false) {
  const table = create("table", "gpu-table gpu-table-body");
  const body = document.createElement("tbody");
  body.replaceChildren(...records.map((record) => tableRow(record, grouped)));
  table.append(body);
  return table;
}

function groupMetric(label, value) {
  const metric = create("span", "gpu-group-metric");
  metric.append(create("small", "", label), create("strong", "", value));
  return metric;
}

function gpuGroup(group) {
  const { server, records } = group;
  const details = create("details", "gpu-server-group");
  const focused = view.filter !== "all" || view.query.trim() !== "";
  details.open = focused || view.expandedHosts.has(server.host);
  details.dataset.host = server.host;
  const summary = create("summary", "gpu-group-summary");
  const identity = create("span", "gpu-group-identity");
  const name = create("span", "gpu-group-name");
  name.append(
    create("strong", "", server.host),
    create("small", "", `${records.length === server.gpus.length ? records.length : `${records.length} / ${server.gpus.length}`} 张 GPU`),
  );
  identity.append(create("i", "status-dot online"), name);

  const gpuUsageValues = server.gpus
    .map((gpu) => gpu.utilization_gpu_pct)
    .filter((value) => value != null);
  const gpuUsage = gpuUsageValues.length
    ? gpuUsageValues.reduce((sum, value) => sum + numeric(value), 0) / gpuUsageValues.length
    : 0;
  const gpuMemoryUsed = server.gpus.reduce((sum, gpu) => sum + numeric(gpu.memory_used_mib), 0);
  const gpuMemoryTotal = server.gpus.reduce((sum, gpu) => sum + numeric(gpu.memory_total_mib), 0);
  const resources = serverResources(server);
  const issue = serverIssue(server);
  const stateClass = issue ? ` ${issue.severity}` : "";
  const state = create(
    "span",
    `gpu-group-state${stateClass}`,
    issue?.severity === "critical" ? "严重" : issue ? "需关注" : "正常",
  );
  const chevron = create("i", "group-chevron");
  summary.append(
    identity,
    groupMetric("GPU 平均", `${format(gpuUsage, 1)}%`),
    groupMetric("显存", `${format(ratio(gpuMemoryUsed, gpuMemoryTotal), 1)}%`),
    groupMetric("CPU", server.system?.cpu_usage_pct == null ? "采样中" : `${format(resources.cpu, 1)}%`),
    groupMetric("RAM", `${format(resources.memory, 1)}%`),
    state,
    chevron,
  );
  details.append(summary, gpuTable(records, true));
  details.addEventListener("toggle", () => {
    if (focused) {
      updateGroupToggle();
      return;
    }
    if (details.open) view.expandedHosts.add(server.host);
    else view.expandedHosts.delete(server.host);
    updateGroupToggle();
  });
  return details;
}

function tableSignature(server, records) {
  const system = server.system;
  return JSON.stringify({
    host: server.host,
    incidentVersion: view.incidentVersion,
    filter: view.filter,
    query: view.query.trim(),
    sort: view.sort,
    status: server.status,
    stale: server.stale,
    system: system ? {
      cpu: system.cpu_usage_pct,
      memoryTotal: system.memory_total_mib,
      memoryUsed: system.memory_used_mib,
      swapTotal: system.swap_total_mib,
      swapUsed: system.swap_used_mib,
      diskTotal: system.disk_total_mib,
      diskUsed: system.disk_used_mib,
    } : null,
    gpus: server.gpus.map((gpu) => ({
      index: gpu.index,
      uuid: gpu.uuid,
      name: gpu.name,
      driver: gpu.driver_version,
      pstate: gpu.pstate,
      utilization: gpu.utilization_gpu_pct,
      memoryTotal: gpu.memory_total_mib,
      memoryUsed: gpu.memory_used_mib,
      temperature: gpu.temperature_c,
      power: gpu.power_draw_w,
    })),
    visibleGpuUuids: records.map((record) => record.gpu.uuid),
  });
}

function cachedGpuGroup(group) {
  const signature = tableSignature(group.server, group.records);
  const cached = view.groupCache.get(group.host);
  if (cached?.signature === signature) return cached.node;
  const node = gpuGroup(group);
  view.groupCache.set(group.host, { signature, node });
  return node;
}

function reconcileChildren(parent, desired) {
  desired.forEach((node, index) => {
    if (parent.children[index] !== node) {
      parent.insertBefore(node, parent.children[index] || null);
    }
  });
  while (parent.children.length > desired.length) {
    parent.lastElementChild.remove();
  }
}

function updateGroupToggle() {
  const groups = [...elements.gpuGroups.querySelectorAll("details.gpu-server-group")];
  elements.groupToggle.hidden = view.selectedHost !== "all" || groups.length === 0;
  if (!groups.length) return;
  elements.groupToggle.textContent = groups.every((group) => group.open)
    ? "全部收起"
    : "全部展开";
}

function renderTable() {
  const records = filteredRecords();
  const selected = view.selectedHost === "all"
    ? null
    : view.snapshot.servers.find((server) => server.host === view.selectedHost);
  elements.inventoryTitle.textContent = selected
    ? selected.host
    : view.serverFilter === "all" ? "全局资源" : SERVER_FILTER_LABELS[view.serverFilter];
  elements.visibleCount.textContent = records.length;
  elements.exportCsv.disabled = records.length === 0;
  elements.gpuGroups.classList.toggle("single", Boolean(selected));
  if (selected) {
    const ordered = sortedRecords(records);
    const signature = tableSignature(selected, ordered);
    if (view.singleTableCache?.signature !== signature) {
      view.singleTableCache = { signature, node: gpuTable(ordered) };
    }
    reconcileChildren(elements.gpuGroups, [view.singleTableCache.node]);
  } else {
    const groups = groupedRecords(records);
    const activeHosts = new Set(groups.map((group) => group.host));
    [...view.groupCache.keys()].forEach((host) => {
      if (!activeHosts.has(host)) view.groupCache.delete(host);
    });
    reconcileChildren(elements.gpuGroups, groups.map(cachedGpuGroup));
  }
  updateGroupToggle();
  elements.emptyState.hidden = records.length !== 0;
}

function render() {
  if (view.renderFrame != null) {
    cancelAnimationFrame(view.renderFrame);
    view.renderFrame = null;
  }
  if (!view.snapshot) return;
  renderSummary();
  renderAttention();
  renderIncidents();
  renderServers();
  renderNodeNotice();
  renderResources();
  renderHeatmap();
  renderTrends();
  renderTable();
  renderGpuDetail();
  refreshRelativeTimes();
}

function scheduleRender() {
  if (view.renderFrame != null) return;
  view.renderFrame = requestAnimationFrame(() => {
    view.renderFrame = null;
    render();
    syncHistory();
  });
}

async function fetchSnapshot() {
  if (view.snapshotFetchInFlight) return view.snapshotFetchInFlight;
  const request = (async () => {
    try {
      const response = await fetch("/api/snapshot", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const snapshot = await response.json();
      if (!acceptSnapshot(snapshot)) return true;
      view.lastEventAt = Date.now();
      normalizeSelection();
      render();
      syncHistory();
      syncIncidents();
      return true;
    } catch (_error) {
      if (!view.snapshot) setConnection("offline", "服务不可达");
      return false;
    }
  })();
  view.snapshotFetchInFlight = request;
  try {
    return await request;
  } finally {
    if (view.snapshotFetchInFlight === request) view.snapshotFetchInFlight = null;
  }
}

async function syncIncidents() {
  if (!view.snapshot) return;
  const targetVersion = numeric(view.snapshot.incidentVersion, 0);
  if (view.incidents && view.incidentVersion === targetVersion) return;
  if (view.incidentLoadingVersion === targetVersion) return;
  view.incidentLoadingVersion = targetVersion;
  const request = ++view.incidentRequest;
  try {
    const response = await fetch("/api/incidents?limit=50", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const incidents = await response.json();
    if (request !== view.incidentRequest) return;
    view.incidents = incidents;
    view.incidentVersion = numeric(incidents.version, 0);
    renderIncidents();
    renderAttention();
    renderServers();
    renderTable();
    renderGpuDetail();
  } catch (_error) {
    // Current telemetry remains usable if the optional transition feed is unavailable.
  } finally {
    if (request === view.incidentRequest) view.incidentLoadingVersion = null;
    if (
      request === view.incidentRequest
      && view.snapshot
      && view.incidentVersion !== numeric(view.snapshot.incidentVersion, 0)
    ) syncIncidents();
  }
}

function connect() {
  const events = new EventSource("/api/events");
  const markLive = () => {
    if (view.connectionErrorTimer != null) {
      clearTimeout(view.connectionErrorTimer);
      view.connectionErrorTimer = null;
    }
    setConnection("live", "实时连接");
  };
  events.addEventListener("open", markLive);
  events.addEventListener("snapshot", (event) => {
    try {
      if (!acceptSnapshot(JSON.parse(event.data))) return;
      view.lastEventAt = Date.now();
      markLive();
      normalizeSelection();
      scheduleRender();
      syncIncidents();
    } catch (_error) {
      setConnection("offline", "数据异常");
    }
  });
  events.addEventListener("error", () => {
    if (view.connectionErrorTimer != null) return;
    view.connectionErrorTimer = setTimeout(async () => {
      view.connectionErrorTimer = null;
      const reachable = await fetchSnapshot();
      if (events.readyState === EventSource.OPEN) {
        markLive();
      } else if (reachable) {
        setConnection("delayed", "轮询同步");
      } else {
        setConnection("offline", "服务不可达");
      }
    }, 1200);
  });
}

document.querySelectorAll(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    view.filter = button.dataset.filter;
    render();
  });
});

document.querySelectorAll(".fleet-filter").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".fleet-filter").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    view.serverFilter = button.dataset.serverFilter;
    if (view.selectedHost !== "all") selectHost("all");
    else render();
  });
});

document.querySelectorAll(".attention-filter").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.disabled) return;
    view.attentionFilter = button.dataset.attentionFilter;
    renderAttention();
  });
});

document.querySelectorAll(".heatmap-mode").forEach((button) => {
  button.addEventListener("click", () => {
    view.heatMetric = button.dataset.heatMetric;
    elements.settingsHeatMetric.value = view.heatMetric;
    savePreferences();
    renderHeatmap();
  });
});

elements.search.addEventListener("input", () => {
  view.query = elements.search.value;
  render();
});

document.addEventListener("keydown", (event) => {
  if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
  const tag = document.activeElement?.tagName;
  if (event.key === "/" && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") {
    event.preventDefault();
    elements.search.focus();
    return;
  }
  if (event.key !== "Escape" || document.activeElement !== elements.search) return;
  if (elements.search.value) {
    elements.search.value = "";
    view.query = "";
    render();
  } else {
    elements.search.blur();
  }
});

elements.gpuSort.addEventListener("change", () => {
  view.sort = elements.gpuSort.value;
  elements.settingsGpuSort.value = view.sort;
  savePreferences();
  render();
});

elements.settingsToggle.addEventListener("click", () => {
  syncPreferenceControls();
  if (elements.gpuDetailDialog.open) elements.gpuDetailDialog.close();
  elements.settingsDialog.showModal();
});

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => {
    document.getElementById(button.dataset.closeDialog)?.close();
  });
});

document.querySelectorAll("dialog.side-dialog").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

elements.gpuDetailDialog.addEventListener("close", () => {
  view.selectedGpu = null;
});

elements.serverSort.addEventListener("change", () => {
  view.serverSort = elements.serverSort.value;
  savePreferences();
  renderServers();
});

elements.settingsGpuSort.addEventListener("change", () => {
  view.sort = elements.settingsGpuSort.value;
  elements.gpuSort.value = view.sort;
  savePreferences();
  render();
});

elements.settingsHeatMetric.addEventListener("change", () => {
  view.heatMetric = elements.settingsHeatMetric.value;
  savePreferences();
  renderHeatmap();
});

elements.showTemperature.addEventListener("change", () => {
  preferences.showTemperature = elements.showTemperature.checked;
  syncPreferenceControls();
  savePreferences();
});

elements.showPower.addEventListener("change", () => {
  preferences.showPower = elements.showPower.checked;
  syncPreferenceControls();
  savePreferences();
});

elements.resetPreferences.addEventListener("click", resetPreferences);

elements.refreshInterval.addEventListener("change", updatePollInterval);

elements.exportCsv.addEventListener("click", exportVisibleCsv);

elements.incidentToggle.addEventListener("click", () => {
  view.incidentExpanded = !view.incidentExpanded;
  renderIncidents();
});

elements.groupToggle.addEventListener("click", () => {
  const groups = [...elements.gpuGroups.querySelectorAll("details.gpu-server-group")];
  const expand = groups.some((group) => !group.open);
  groups.forEach((group) => {
    group.open = expand;
    const host = group.dataset.host;
    if (expand) view.expandedHosts.add(host);
    else view.expandedHosts.delete(host);
  });
  updateGroupToggle();
});

setInterval(() => {
  if (view.snapshot) elements.lastSync.textContent = age(view.snapshot.lastPollCompletedAt);
  refreshRelativeTimes();
  renderConnectionStatus();
  const elapsed = Date.now() - view.lastEventAt;
  const fallbackAfter = Math.max(2000, numeric(view.snapshot?.pollIntervalSeconds, 5) * 1000);
  if (view.transportKind !== "live" && elapsed > fallbackAfter) fetchSnapshot();
  else if (elapsed > 15000) fetchSnapshot();
}, 1000);

syncPreferenceControls();
fetchSnapshot();
connect();
