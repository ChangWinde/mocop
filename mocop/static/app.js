"use strict";

const PREFERENCE_STORAGE_KEY = "mocop.preferences.v1";
const VISUAL_ASSET_DATABASE = "mocop.visual-assets.v1";
const VISUAL_ASSET_STORE = "assets";
const BACKGROUND_ASSET_KEY = "background";
const MAX_BACKGROUND_BYTES = 8 * 1024 * 1024;
const MAX_BACKGROUND_DIMENSION = 8192;
const MAX_BACKGROUND_PIXELS = 32_000_000;
const BACKGROUND_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/avif"]);
const DEFAULT_PREFERENCES = Object.freeze({
  serverSort: "custom",
  serverOrder: [],
  gpuSort: "host",
  heatMetric: "utilization",
  theme: "midnight",
  density: "comfortable",
  backgroundVisibility: 38,
  serverFilter: "all",
  showTemperature: true,
  showPower: true,
});
const SERVER_SORT_VALUES = new Set(["custom", "group", "host", "status", "gpu", "cpu"]);
const GPU_SORT_VALUES = new Set(["host", "utilization", "memory", "temperature", "power"]);
const HEAT_METRIC_VALUES = new Set(["utilization", "memory", "temperature"]);
const THEME_VALUES = new Set(["midnight", "graphite", "aurora", "glass", "terminal"]);
const DENSITY_VALUES = new Set(["comfortable", "compact"]);
const SERVER_FILTER_VALUES = new Set(["all", "issues", "busy", "available", "stale"]);
const CAPACITY_HOST_BLOCKERS = new Set(["connectivity", "gpu_availability", "gpu_count"]);
const CAPACITY_GPU_BLOCKERS = new Set([
  "gpu_ecc",
  "gpu_memory_repair",
  "gpu_slowdown",
  "gpu_temperature",
]);

function safeBackgroundVisibility(value) {
  return Number.isInteger(value) && value >= 15 && value <= 70
    ? value : DEFAULT_PREFERENCES.backgroundVisibility;
}

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
      theme: THEME_VALUES.has(stored.theme)
        ? stored.theme : DEFAULT_PREFERENCES.theme,
      density: DENSITY_VALUES.has(stored.density)
        ? stored.density : DEFAULT_PREFERENCES.density,
      backgroundVisibility: safeBackgroundVisibility(stored.backgroundVisibility),
      serverFilter: SERVER_FILTER_VALUES.has(stored.serverFilter)
        ? stored.serverFilter : DEFAULT_PREFERENCES.serverFilter,
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
document.documentElement.dataset.theme = preferences.theme;
document.documentElement.dataset.density = preferences.density;
document.documentElement.style.setProperty(
  "--custom-background-opacity",
  String(preferences.backgroundVisibility / 100),
);

const view = {
  snapshot: null,
  selectedHost: "all",
  serverFilter: preferences.serverFilter,
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
  fleetGroupCache: new Map(),
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
  capacityRequest: { gpuCount: 1, minVramGiB: 24, model: "any" },
  capacityModelSignature: "",
  inventory: null,
  inventoryLoading: false,
  inventoryMessage: "",
  inventoryMessageKind: "",
  inventoryPendingHost: null,
  inventoryConfirmHost: null,
  inventoryConfirmTimer: null,
  maintenanceEditingHost: null,
  maintenancePendingHost: null,
  groupEditingHost: null,
  groupPendingHost: null,
  collectorSettingsDirty: false,
  collectorSettingsSaving: false,
  backgroundObjectUrl: null,
  backgroundRequestId: 0,
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
  capacityToggle: $("#capacity-toggle"),
  capacityDialog: $("#capacity-dialog"),
  capacityForm: $("#capacity-form"),
  capacityGpuCount: $("#capacity-gpu-count"),
  capacityVram: $("#capacity-vram"),
  capacityModel: $("#capacity-model"),
  capacityRule: $("#capacity-rule"),
  capacitySummary: $("#capacity-summary"),
  capacityUpdated: $("#capacity-updated"),
  capacityResults: $("#capacity-results"),
  serverSort: $("#server-sort"),
  defaultServerFilter: $("#default-server-filter"),
  interfaceDensity: $("#interface-density"),
  backgroundImageInput: $("#background-image-input"),
  backgroundImageStatus: $("#background-image-status"),
  backgroundVisibility: $("#background-visibility"),
  backgroundVisibilityValue: $("#background-visibility-value"),
  removeBackgroundImage: $("#remove-background-image"),
  settingsGpuSort: $("#settings-gpu-sort"),
  settingsHeatMetric: $("#settings-heat-metric"),
  showTemperature: $("#show-temperature"),
  showPower: $("#show-power"),
  resetPreferences: $("#reset-preferences"),
  collectorSettingsForm: $("#collector-settings-form"),
  settingsPollInterval: $("#settings-poll-interval"),
  settingsProbeTimeout: $("#settings-probe-timeout"),
  settingsMaxWorkers: $("#settings-max-workers"),
  saveCollectorSettings: $("#save-collector-settings"),
  collectorSettingsStatus: $("#collector-settings-status"),
  inventoryRefresh: $("#inventory-refresh"),
  inventoryStatus: $("#inventory-status"),
  configuredHostCount: $("#configured-host-count"),
  configuredHostList: $("#configured-host-list"),
  availableHostCount: $("#available-host-count"),
  availableHostList: $("#available-host-list"),
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
const themeChoiceButtons = [...document.querySelectorAll("[data-theme-choice]")];

function create(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

// Binary presentation assets stay out of both synchronous preferences and the service.
function openVisualAssetDatabase() {
  return new Promise((resolve, reject) => {
    if (!("indexedDB" in window)) {
      reject(new Error("IndexedDB unavailable"));
      return;
    }
    const request = indexedDB.open(VISUAL_ASSET_DATABASE, 1);
    let settled = false;
    const finish = (callback, value) => {
      if (settled) {
        if (value && typeof value.close === "function") value.close();
        return;
      }
      settled = true;
      callback(value);
    };
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(VISUAL_ASSET_STORE)) {
        request.result.createObjectStore(VISUAL_ASSET_STORE);
      }
    };
    request.onsuccess = () => finish(resolve, request.result);
    request.onerror = () => finish(
      reject,
      request.error || new Error("Unable to open browser storage"),
    );
    request.onblocked = () => finish(reject, new Error("Browser storage is blocked"));
  });
}

async function transactVisualAsset(mode, operation) {
  const database = await openVisualAssetDatabase();
  return new Promise((resolve, reject) => {
    let result;
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      database.close();
      callback(value);
    };
    let transaction;
    let request;
    try {
      transaction = database.transaction(VISUAL_ASSET_STORE, mode);
      request = operation(transaction.objectStore(VISUAL_ASSET_STORE));
    } catch (error) {
      finish(reject, error);
      return;
    }
    request.onsuccess = () => { result = request.result; };
    transaction.oncomplete = () => finish(resolve, result);
    transaction.onerror = () => finish(
      reject,
      transaction.error || request.error || new Error("Browser storage transaction failed"),
    );
    transaction.onabort = transaction.onerror;
  });
}

function readStoredBackground() {
  return transactVisualAsset("readonly", (store) => store.get(BACKGROUND_ASSET_KEY));
}

function writeStoredBackground(blob) {
  return transactVisualAsset("readwrite", (store) => store.put(blob, BACKGROUND_ASSET_KEY));
}

function deleteStoredBackground() {
  return transactVisualAsset("readwrite", (store) => store.delete(BACKGROUND_ASSET_KEY));
}

function decodeImageSize(blob) {
  if (typeof createImageBitmap === "function") {
    return createImageBitmap(blob).then((bitmap) => {
      const dimensions = { width: bitmap.width, height: bitmap.height };
      bitmap.close();
      return dimensions;
    });
  }
  return new Promise((resolve, reject) => {
    const objectUrl = URL.createObjectURL(blob);
    const image = new Image();
    const finish = (callback, value) => {
      URL.revokeObjectURL(objectUrl);
      callback(value);
    };
    image.onload = () => finish(resolve, {
      width: image.naturalWidth,
      height: image.naturalHeight,
    });
    image.onerror = () => finish(reject, new Error("无法解析图片内容"));
    image.src = objectUrl;
  });
}

function asciiAt(bytes, offset, value) {
  if (offset < 0 || offset + value.length > bytes.length) return false;
  for (let index = 0; index < value.length; index += 1) {
    if (bytes[offset + index] !== value.charCodeAt(index)) return false;
  }
  return true;
}

function uint32BigEndian(bytes, offset) {
  if (offset + 4 > bytes.length) return null;
  return (
    bytes[offset] * 0x1000000
    + bytes[offset + 1] * 0x10000
    + bytes[offset + 2] * 0x100
    + bytes[offset + 3]
  );
}

function uint32LittleEndian(bytes, offset) {
  if (offset + 4 > bytes.length) return null;
  return (
    bytes[offset]
    + bytes[offset + 1] * 0x100
    + bytes[offset + 2] * 0x10000
    + bytes[offset + 3] * 0x1000000
  );
}

async function isAnimatedImage(blob) {
  // Container markers are checked before decode so a selected background cannot animate.
  const bytes = new Uint8Array(await blob.arrayBuffer());
  if (blob.type === "image/jpeg") {
    if (bytes[0] !== 0xff || bytes[1] !== 0xd8 || bytes[2] !== 0xff) {
      throw new Error("图片内容与文件格式不匹配");
    }
    return false;
  }
  if (blob.type === "image/png") {
    if (
      bytes[0] !== 0x89
      || !asciiAt(bytes, 1, "PNG")
      || bytes[4] !== 0x0d
      || bytes[5] !== 0x0a
      || bytes[6] !== 0x1a
      || bytes[7] !== 0x0a
    ) {
      throw new Error("图片内容与文件格式不匹配");
    }
    for (let offset = 8; offset + 12 <= bytes.length;) {
      const length = uint32BigEndian(bytes, offset);
      if (length == null || length > bytes.length - offset - 12) return false;
      if (asciiAt(bytes, offset + 4, "acTL")) return true;
      if (asciiAt(bytes, offset + 4, "IEND")) return false;
      offset += length + 12;
    }
    return false;
  }
  if (blob.type === "image/webp") {
    if (!asciiAt(bytes, 0, "RIFF") || !asciiAt(bytes, 8, "WEBP")) {
      throw new Error("图片内容与文件格式不匹配");
    }
    for (let offset = 12; offset + 8 <= bytes.length;) {
      const length = uint32LittleEndian(bytes, offset + 4);
      if (length == null || length > bytes.length - offset - 8) return false;
      if (asciiAt(bytes, offset, "ANIM") || asciiAt(bytes, offset, "ANMF")) return true;
      offset += 8 + length + (length % 2);
    }
    return false;
  }
  if (blob.type === "image/avif") {
    const boxLength = uint32BigEndian(bytes, 0);
    if (
      boxLength == null
      || (boxLength > 1 && boxLength > bytes.length)
      || !asciiAt(bytes, 4, "ftyp")
    ) {
      throw new Error("图片内容与文件格式不匹配");
    }
    const headerLimit = Math.min(
      bytes.length,
      boxLength === 0 ? bytes.length : boxLength === 1 ? 256 : boxLength,
    );
    let hasAvifBrand = false;
    let animated = false;
    for (let offset = 8; offset + 4 <= headerLimit; offset += 1) {
      hasAvifBrand ||= asciiAt(bytes, offset, "avif") || asciiAt(bytes, offset, "avis");
      animated ||= asciiAt(bytes, offset, "avis");
    }
    if (!hasAvifBrand) throw new Error("图片内容与文件格式不匹配");
    return animated;
  }
  return false;
}

async function validateBackgroundBlob(blob) {
  if (!(blob instanceof Blob) || !BACKGROUND_TYPES.has(blob.type)) {
    throw new Error("仅支持 PNG、JPEG、WebP 或 AVIF 图片");
  }
  if (blob.size <= 0 || blob.size > MAX_BACKGROUND_BYTES) {
    throw new Error("图片大小必须在 8 MiB 以内");
  }
  if (await isAnimatedImage(blob)) {
    throw new Error("不支持动态图片，请选择静态背景");
  }
  let dimensions;
  try {
    dimensions = await decodeImageSize(blob);
  } catch (_error) {
    throw new Error("图片已损坏或当前浏览器不支持该格式");
  }
  if (
    dimensions.width <= 0
    || dimensions.height <= 0
    || dimensions.width > MAX_BACKGROUND_DIMENSION
    || dimensions.height > MAX_BACKGROUND_DIMENSION
    || dimensions.width * dimensions.height > MAX_BACKGROUND_PIXELS
  ) {
    throw new Error("图片尺寸不能超过 8192 像素或 32 百万像素");
  }
  return dimensions;
}

function setBackgroundStatus(message, kind = "") {
  elements.backgroundImageStatus.textContent = message;
  elements.backgroundImageStatus.className = `background-status${kind ? ` ${kind}` : ""}`;
}

function clearRenderedBackground() {
  if (view.backgroundObjectUrl) URL.revokeObjectURL(view.backgroundObjectUrl);
  view.backgroundObjectUrl = null;
  document.documentElement.style.removeProperty("--custom-background-image");
  delete document.documentElement.dataset.background;
  elements.removeBackgroundImage.disabled = true;
}

function renderBackground(blob) {
  clearRenderedBackground();
  view.backgroundObjectUrl = URL.createObjectURL(blob);
  document.documentElement.style.setProperty(
    "--custom-background-image",
    `url("${view.backgroundObjectUrl}")`,
  );
  document.documentElement.dataset.background = "custom";
  elements.removeBackgroundImage.disabled = false;
}

async function loadStoredBackground() {
  try {
    const blob = await readStoredBackground();
    if (!(blob instanceof Blob)) return;
    await validateBackgroundBlob(blob);
    renderBackground(blob);
    setBackgroundStatus("已恢复当前浏览器保存的背景", "success");
  } catch (_error) {
    clearRenderedBackground();
    setBackgroundStatus("浏览器背景存储不可用；内置皮肤仍可正常使用", "error");
  }
}

async function selectBackgroundImage() {
  const file = elements.backgroundImageInput.files?.[0];
  elements.backgroundImageInput.value = "";
  if (!file) return;
  const requestId = ++view.backgroundRequestId;
  elements.backgroundImageInput.disabled = true;
  setBackgroundStatus("正在安全读取图片…");
  try {
    const dimensions = await validateBackgroundBlob(file);
    if (requestId !== view.backgroundRequestId) return;
    renderBackground(file);
    try {
      await writeStoredBackground(file);
      setBackgroundStatus(
        `已保存在当前浏览器 · ${dimensions.width} × ${dimensions.height}`,
        "success",
      );
    } catch (_error) {
      setBackgroundStatus("浏览器存储空间不足；背景仅在本次会话有效", "error");
    }
  } catch (error) {
    if (requestId === view.backgroundRequestId) {
      setBackgroundStatus(error instanceof Error ? error.message : "无法使用这张图片", "error");
    }
  } finally {
    if (requestId === view.backgroundRequestId) elements.backgroundImageInput.disabled = false;
  }
}

async function removeBackgroundImage() {
  elements.removeBackgroundImage.disabled = true;
  setBackgroundStatus("正在移除背景…");
  try {
    await deleteStoredBackground();
    view.backgroundRequestId += 1;
    clearRenderedBackground();
    setBackgroundStatus("背景已从当前浏览器移除", "success");
  } catch (_error) {
    elements.removeBackgroundImage.disabled = !view.backgroundObjectUrl;
    setBackgroundStatus("无法更新浏览器存储，背景未移除", "error");
  }
}

function savePreferences() {
  const value = {
    serverSort: view.serverSort,
    serverOrder: view.serverOrder,
    gpuSort: view.sort,
    heatMetric: view.heatMetric,
    theme: preferences.theme,
    density: preferences.density,
    backgroundVisibility: preferences.backgroundVisibility,
    serverFilter: view.serverFilter,
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
  elements.defaultServerFilter.value = view.serverFilter;
  elements.interfaceDensity.value = preferences.density;
  elements.backgroundVisibility.value = String(preferences.backgroundVisibility);
  elements.backgroundVisibilityValue.value = `${preferences.backgroundVisibility}%`;
  elements.gpuSort.value = view.sort;
  elements.settingsGpuSort.value = view.sort;
  elements.settingsHeatMetric.value = view.heatMetric;
  elements.showTemperature.checked = preferences.showTemperature;
  elements.showPower.checked = preferences.showPower;
  document.documentElement.dataset.theme = preferences.theme;
  document.documentElement.dataset.density = preferences.density;
  document.documentElement.style.setProperty(
    "--custom-background-opacity",
    String(preferences.backgroundVisibility / 100),
  );
  document.querySelectorAll(".fleet-filter").forEach((button) => {
    button.classList.toggle("active", button.dataset.serverFilter === view.serverFilter);
  });
  themeChoiceButtons.forEach((button) => {
    const selected = button.dataset.themeChoice === preferences.theme;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-checked", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  document.body.classList.toggle("hide-gpu-temperature", !preferences.showTemperature);
  document.body.classList.toggle("hide-gpu-power", !preferences.showPower);
}

function resetPreferences() {
  view.serverSort = DEFAULT_PREFERENCES.serverSort;
  view.serverOrder = [];
  view.sort = DEFAULT_PREFERENCES.gpuSort;
  view.heatMetric = DEFAULT_PREFERENCES.heatMetric;
  preferences.theme = DEFAULT_PREFERENCES.theme;
  preferences.density = DEFAULT_PREFERENCES.density;
  preferences.backgroundVisibility = DEFAULT_PREFERENCES.backgroundVisibility;
  view.serverFilter = DEFAULT_PREFERENCES.serverFilter;
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

function shortTime(timestamp) {
  const value = new Date(timestamp);
  if (!Number.isFinite(value.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(value);
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
    if (view.inventory?.collectorSettings) {
      view.inventory.collectorSettings.pollIntervalSeconds = settings.pollIntervalSeconds;
      syncCollectorSettings();
    }
    elements.refreshInterval.value = String(settings.pollIntervalSeconds);
    showRefreshFeedback("saved", `已保存为 ${format(settings.pollIntervalSeconds)} 秒`);
    renderSummary();
  } catch (_error) {
    syncRefreshControl();
    showRefreshFeedback("error", "采集频率调整失败，已恢复原设置");
  } finally {
    elements.refreshInterval.disabled = false;
  }
}

function normalizeCollectorSettings(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("Invalid collector settings response");
  }
  const pollIntervalSeconds = payload.pollIntervalSeconds;
  const probeTimeoutSeconds = payload.probeTimeoutSeconds;
  const maxWorkers = payload.maxWorkers;
  if (
    typeof pollIntervalSeconds !== "number"
    || !Number.isFinite(pollIntervalSeconds)
    || pollIntervalSeconds < 1
    || pollIntervalSeconds > 3600
    || typeof probeTimeoutSeconds !== "number"
    || !Number.isFinite(probeTimeoutSeconds)
    || probeTimeoutSeconds < 2
    || probeTimeoutSeconds > 300
    || !Number.isSafeInteger(maxWorkers)
    || maxWorkers < 1
    || maxWorkers > 64
  ) {
    throw new TypeError("Invalid collector settings response");
  }
  return { pollIntervalSeconds, probeTimeoutSeconds, maxWorkers };
}

function normalizeInventory(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("Invalid inventory response");
  }
  const configuredHosts = safeStoredHosts(payload.configuredHosts);
  const activeHosts = safeStoredHosts(payload.activeHosts);
  const availableHosts = safeStoredHosts(payload.availableHosts);
  const collectorSettings = normalizeCollectorSettings(payload.collectorSettings);
  const maintenanceWindows = normalizeMaintenanceWindows(
    payload.maintenanceWindows,
    configuredHosts,
  );
  const hostGroups = normalizeHostGroups(payload.hostGroups, configuredHosts);
  if (
    configuredHosts.length !== payload.configuredHosts?.length
    || activeHosts.length !== payload.activeHosts?.length
    || availableHosts.length !== payload.availableHosts?.length
    || (payload.localHost != null && !safeStoredHosts([payload.localHost]).length)
    || typeof payload.autoDiscover !== "boolean"
    || typeof payload.writable !== "boolean"
    || !Number.isSafeInteger(payload.ignoredCodeHostCount)
    || !Number.isSafeInteger(payload.excludedHostCount)
  ) {
    throw new TypeError("Invalid inventory response");
  }
  return {
    configuredHosts,
    activeHosts,
    availableHosts,
    localHost: payload.localHost,
    autoDiscover: payload.autoDiscover,
    writable: payload.writable,
    ignoredCodeHostCount: Math.max(0, payload.ignoredCodeHostCount),
    excludedHostCount: Math.max(0, payload.excludedHostCount),
    collectorSettings,
    maintenanceWindows,
    hostGroups,
  };
}

function normalizeHostGroups(payload, configuredHosts) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("Invalid host groups response");
  }
  const configured = new Set(configuredHosts);
  const groups = {};
  Object.entries(payload).forEach(([host, group]) => {
    if (
      !configured.has(host)
      || typeof group !== "string"
      || !group
      || group !== group.trim()
      || [...group].length > 48
      || /\p{C}/u.test(group)
    ) {
      throw new TypeError("Invalid host groups response");
    }
    groups[host] = group;
  });
  return groups;
}

function normalizeMaintenanceWindows(payload, configuredHosts) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("Invalid maintenance windows response");
  }
  const configured = new Set(configuredHosts);
  const windows = {};
  Object.entries(payload).forEach(([host, window]) => {
    if (
      !configured.has(host)
      || !window
      || typeof window !== "object"
      || Array.isArray(window)
      || Object.keys(window).sort().join(",") !== "reason,until"
      || typeof window.until !== "string"
      || !Number.isFinite(Date.parse(window.until))
      || typeof window.reason !== "string"
      || [...window.reason].length > 120
      || /\p{C}/u.test(window.reason)
    ) {
      throw new TypeError("Invalid maintenance windows response");
    }
    windows[host] = { until: window.until, reason: window.reason };
  });
  return windows;
}

function setCollectorSettingsStatus(kind, message) {
  elements.collectorSettingsStatus.className = kind;
  elements.collectorSettingsStatus.textContent = message;
}

function syncCollectorSettings() {
  const settings = view.inventory?.collectorSettings;
  const writable = Boolean(settings && view.inventory.writable);
  const fields = [
    elements.settingsPollInterval,
    elements.settingsProbeTimeout,
    elements.settingsMaxWorkers,
  ];
  fields.forEach((field) => {
    field.disabled = !writable || view.collectorSettingsSaving;
  });
  elements.saveCollectorSettings.disabled = (
    !writable || view.collectorSettingsSaving || !view.collectorSettingsDirty
  );
  if (settings && !view.collectorSettingsDirty) {
    elements.settingsPollInterval.value = String(settings.pollIntervalSeconds);
    elements.settingsProbeTimeout.value = String(settings.probeTimeoutSeconds);
    elements.settingsMaxWorkers.value = String(settings.maxWorkers);
  }
  if (!settings) {
    setCollectorSettingsStatus("", "等待读取本地配置");
  } else if (!view.inventory.writable) {
    setCollectorSettingsStatus("error", "当前配置不可由网页修改");
  } else if (view.collectorSettingsSaving) {
    setCollectorSettingsStatus("", "正在验证并写入配置…");
  } else if (!view.collectorSettingsDirty) {
    setCollectorSettingsStatus("success", "已与本地配置同步");
  }
}

function markCollectorSettingsDirty() {
  view.collectorSettingsDirty = true;
  setCollectorSettingsStatus("", "有尚未保存的采集策略");
  syncCollectorSettings();
}

async function saveCollectorSettings(event) {
  event.preventDefault();
  if (
    view.collectorSettingsSaving
    || !view.inventory?.writable
    || !elements.collectorSettingsForm.reportValidity()
  ) return;
  const settings = {
    pollIntervalSeconds: Number(elements.settingsPollInterval.value),
    probeTimeoutSeconds: Number(elements.settingsProbeTimeout.value),
    maxWorkers: Number(elements.settingsMaxWorkers.value),
  };
  if (!Number.isSafeInteger(settings.maxWorkers)) {
    setCollectorSettingsStatus("error", "并发探测数必须是整数");
    return;
  }
  view.collectorSettingsSaving = true;
  syncCollectorSettings();
  try {
    const response = await fetch("/api/settings/collector", {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Monitor-Request": "dashboard",
      },
      body: JSON.stringify(settings),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const persisted = normalizeCollectorSettings(payload.collectorSettings);
    if (!Number.isSafeInteger(payload.version) || typeof payload.startedAt !== "string") {
      throw new TypeError("Invalid collector settings response");
    }
    view.inventory.collectorSettings = persisted;
    view.collectorSettingsDirty = false;
    view.cadenceSnapshotFloor = {
      version: payload.version,
      startedAt: payload.startedAt,
    };
    if (view.snapshot?.startedAt === payload.startedAt) {
      view.snapshot.pollIntervalSeconds = persisted.pollIntervalSeconds;
      view.snapshot.collectionStaleAfterSeconds = payload.collectionStaleAfterSeconds;
    }
    elements.refreshInterval.value = String(persisted.pollIntervalSeconds);
    syncRefreshControl();
    renderSummary();
    setCollectorSettingsStatus("success", "已写入本地配置并立即生效");
  } catch (_error) {
    setCollectorSettingsStatus(
      "error",
      "保存失败，请检查数值范围、SSH 连接超时与配置权限",
    );
  } finally {
    view.collectorSettingsSaving = false;
    syncCollectorSettings();
  }
}

function inventoryEmpty(message) {
  return create("div", "inventory-empty", message);
}

function inventoryHostRow(host, action) {
  const row = create("div", "inventory-host");
  const identity = create("span", "inventory-host-name");
  identity.append(create("i", "status-dot online"), create("strong", "", host));
  if (action === "remove" && host === view.inventory.localHost) {
    identity.append(create("small", "", "本机"));
  }
  const group = view.inventory.hostGroups[host];
  if (group) identity.append(create("small", "host-group-badge", group));
  const maintenance = view.inventory.maintenanceWindows[host];
  if (maintenance) {
    const badge = create("small", "maintenance-badge", `维护至 ${shortTime(maintenance.until)}`);
    badge.title = maintenance.reason;
    identity.append(badge);
  }
  const actions = create("span", "inventory-host-actions");
  const button = create("button", "inventory-host-action");
  button.type = "button";
  button.disabled = !view.inventory.writable
    || view.inventoryPendingHost != null
    || view.maintenancePendingHost != null
    || view.groupPendingHost != null;
  if (action === "add") {
    button.setAttribute("aria-label", `添加节点 ${host}`);
    button.textContent = view.inventoryPendingHost === host ? "添加中" : "添加";
    button.addEventListener("click", () => changeInventory("add", host));
    actions.append(button);
  } else {
    if (view.inventory.configuredHosts.includes(host)) {
      const groupButton = create("button", "inventory-host-action group-action");
      groupButton.type = "button";
      groupButton.disabled = button.disabled;
      groupButton.setAttribute("aria-expanded", String(view.groupEditingHost === host));
      groupButton.textContent = group ? "调整分组" : "设置分组";
      groupButton.addEventListener("click", () => {
        view.groupEditingHost = view.groupEditingHost === host ? null : host;
        if (view.groupEditingHost) view.maintenanceEditingHost = null;
        renderInventory();
      });
      actions.append(groupButton);
      const maintenanceButton = create("button", "inventory-host-action maintenance-action");
      maintenanceButton.type = "button";
      maintenanceButton.disabled = button.disabled;
      maintenanceButton.setAttribute("aria-expanded", String(view.maintenanceEditingHost === host));
      maintenanceButton.textContent = maintenance ? "调整维护" : "设为维护";
      maintenanceButton.addEventListener("click", () => {
        view.maintenanceEditingHost = view.maintenanceEditingHost === host ? null : host;
        if (view.maintenanceEditingHost) view.groupEditingHost = null;
        renderInventory();
      });
      actions.append(maintenanceButton);
    }
    const confirming = view.inventoryConfirmHost === host;
    button.classList.toggle("confirm", confirming);
    button.setAttribute("aria-label", confirming ? `确认移除节点 ${host}` : `移除节点 ${host}`);
    button.textContent = view.inventoryPendingHost === host
      ? "移除中"
      : confirming ? "再次点击确认" : "移除";
    button.addEventListener("click", () => requestInventoryRemoval(host));
    actions.append(button);
  }
  row.append(identity, actions);
  if (action === "remove" && view.maintenanceEditingHost === host) {
    row.append(maintenanceEditor(host, maintenance));
  }
  if (action === "remove" && view.groupEditingHost === host) {
    row.append(hostGroupEditor(host, group));
  }
  return row;
}

function hostGroupEditor(host, currentGroup) {
  const form = create("form", "host-group-editor");
  const group = document.createElement("input");
  group.type = "text";
  group.maxLength = 48;
  group.placeholder = "例如：训练集群 / 推理集群";
  group.value = currentGroup || "";
  group.setAttribute("aria-label", `${host} 所属分组`);
  const knownGroups = [...new Set(Object.values(view.inventory.hostGroups))]
    .sort((first, second) => first.localeCompare(second));
  if (knownGroups.length) {
    const suggestions = document.createElement("datalist");
    suggestions.id = `host-groups-${host}`;
    knownGroups.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      suggestions.append(option);
    });
    group.setAttribute("list", suggestions.id);
    form.append(group, suggestions);
  } else {
    form.append(group);
  }
  const save = create("button", "primary-action", currentGroup ? "保存分组" : "加入分组");
  save.type = "submit";
  form.append(save);
  if (currentGroup) {
    const clear = create("button", "inline-action danger-action", "移出分组");
    clear.type = "button";
    clear.addEventListener("click", () => changeHostGroup(host, ""));
    form.append(clear);
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!form.reportValidity() || !group.value.trim()) return;
    changeHostGroup(host, group.value);
  });
  return form;
}

function maintenanceEditor(host, maintenance) {
  const form = create("form", "maintenance-editor");
  const reason = document.createElement("input");
  reason.type = "text";
  reason.required = true;
  reason.maxLength = 120;
  reason.placeholder = "维护原因，例如：驱动升级";
  reason.value = maintenance?.reason || "";
  reason.setAttribute("aria-label", `${host} 维护原因`);
  const duration = document.createElement("select");
  duration.setAttribute("aria-label", `${host} 维护时长`);
  [[3600, "1 小时"], [14400, "4 小时"], [86400, "24 小时"], [604800, "7 天"]]
    .forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = label;
      if (value === 14400) option.selected = true;
      duration.append(option);
    });
  const save = create("button", "primary-action", maintenance ? "延长维护" : "开始维护");
  save.type = "submit";
  form.append(reason, duration, save);
  if (maintenance) {
    const clear = create("button", "inline-action danger-action", "立即结束");
    clear.type = "button";
    clear.addEventListener("click", () => changeMaintenance(host, 0, ""));
    form.append(clear);
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    changeMaintenance(host, Number(duration.value), reason.value);
  });
  return form;
}

function renderInventory() {
  syncCollectorSettings();
  elements.inventoryRefresh.disabled = view.inventoryLoading
    || view.inventoryPendingHost != null
    || view.maintenancePendingHost != null
    || view.groupPendingHost != null;
  elements.inventoryRefresh.textContent = view.inventoryLoading ? "扫描中" : "重新扫描";
  if (!view.inventory) {
    elements.configuredHostCount.textContent = "0";
    elements.availableHostCount.textContent = "0";
    elements.configuredHostList.replaceChildren(inventoryEmpty("等待扫描结果"));
    elements.availableHostList.replaceChildren(inventoryEmpty("等待扫描结果"));
    elements.inventoryStatus.className = `inventory-status ${view.inventoryMessageKind}`.trim();
    elements.inventoryStatus.textContent = view.inventoryMessage
      || (view.inventoryLoading ? "正在解析 OpenSSH 配置别名…" : "尚未扫描 SSH 配置");
    return;
  }

  const inventory = view.inventory;
  elements.configuredHostCount.textContent = inventory.activeHosts.length;
  elements.availableHostCount.textContent = inventory.availableHosts.length;
  elements.configuredHostList.replaceChildren(...(
    inventory.activeHosts.length
      ? inventory.activeHosts.map((host) => inventoryHostRow(host, "remove"))
      : [inventoryEmpty("当前配置中没有监控节点")]
  ));
  elements.availableHostList.replaceChildren(...(
    inventory.availableHosts.length
      ? inventory.availableHosts.map((host) => inventoryHostRow(host, "add"))
      : [inventoryEmpty("没有新的可添加节点")]
  ));
  const ignored = inventory.ignoredCodeHostCount + inventory.excludedHostCount;
  let message = `正在监控 ${inventory.activeHosts.length} 个节点，可添加 ${inventory.availableHosts.length} 个`;
  if (ignored) message += `，已按规则忽略 ${ignored} 个别名`;
  if (inventory.autoDiscover) message += "；自动发现已开启";
  if (!inventory.writable) message = "当前配置不可由网页修改，请先运行 mocop init 创建用户配置";
  elements.inventoryStatus.className = `inventory-status ${view.inventoryMessageKind}`.trim();
  elements.inventoryStatus.textContent = view.inventoryMessage || message;
}

async function refreshInventory() {
  if (
    view.inventoryLoading
    || view.inventoryPendingHost != null
    || view.maintenancePendingHost != null
    || view.groupPendingHost != null
  ) return;
  view.inventoryConfirmHost = null;
  if (view.inventoryConfirmTimer != null) clearTimeout(view.inventoryConfirmTimer);
  view.inventoryConfirmTimer = null;
  view.inventoryLoading = true;
  view.inventoryMessage = "";
  view.inventoryMessageKind = "";
  renderInventory();
  try {
    const response = await fetch("/api/inventory", {
      cache: "no-store",
      headers: { "X-Monitor-Request": "dashboard" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    view.inventory = normalizeInventory(await response.json());
  } catch (_error) {
    view.inventory = null;
    view.inventoryMessage = "扫描失败，请检查 SSH 配置与 Mocop 服务权限";
    view.inventoryMessageKind = "error";
  } finally {
    view.inventoryLoading = false;
    renderInventory();
  }
}

async function changeHostGroup(host, group) {
  if (view.groupPendingHost != null) return;
  view.groupPendingHost = host;
  view.inventoryMessage = group.trim()
    ? `正在更新 ${host} 的分组…` : `正在将 ${host} 移出分组…`;
  view.inventoryMessageKind = "";
  renderInventory();
  try {
    const response = await fetch("/api/settings/host-group", {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Monitor-Request": "dashboard",
      },
      body: JSON.stringify({ host, group }),
    });
    if (response.status === 409) throw new RangeError("stale inventory");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    view.inventory = normalizeInventory(await response.json());
    view.groupEditingHost = null;
    view.inventoryMessage = group.trim()
      ? `${host} 已加入 ${group.trim()}` : `${host} 已移出分组`;
    view.inventoryMessageKind = "success";
    await fetchSnapshot();
  } catch (error) {
    view.inventoryMessage = error instanceof RangeError
      ? "节点清单已变化，请重新扫描后再试"
      : "分组保存失败，请检查配置权限与分组名称";
    view.inventoryMessageKind = "error";
  } finally {
    view.groupPendingHost = null;
    renderInventory();
  }
}

async function changeMaintenance(host, durationSeconds, reason) {
  if (view.maintenancePendingHost != null) return;
  view.maintenancePendingHost = host;
  view.inventoryMessage = durationSeconds
    ? `正在为 ${host} 保存维护窗口…`
    : `正在结束 ${host} 的维护窗口…`;
  view.inventoryMessageKind = "";
  renderInventory();
  try {
    const response = await fetch("/api/settings/maintenance", {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Monitor-Request": "dashboard",
      },
      body: JSON.stringify({ host, durationSeconds, reason }),
    });
    if (response.status === 409) throw new RangeError("stale inventory");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    view.inventory = normalizeInventory(await response.json());
    view.maintenanceEditingHost = null;
    view.inventoryMessage = durationSeconds
      ? `${host} 已进入维护；采集继续，活动问题暂不计入待处理`
      : `${host} 已结束维护，活动问题重新进入待处理`;
    view.inventoryMessageKind = "success";
    await fetchSnapshot();
    syncIncidents();
  } catch (error) {
    view.inventoryMessage = error instanceof RangeError
      ? "节点清单已变化，请重新扫描后再试"
      : "维护设置保存失败，请检查本地配置权限";
    view.inventoryMessageKind = "error";
  } finally {
    view.maintenancePendingHost = null;
    renderInventory();
  }
}

async function changeInventory(action, host) {
  if (view.inventoryPendingHost != null) return;
  view.inventoryPendingHost = host;
  view.inventoryConfirmHost = null;
  view.inventoryMessage = action === "add" ? `正在添加 ${host}…` : `正在移除 ${host}…`;
  view.inventoryMessageKind = "";
  renderInventory();
  try {
    const response = await fetch("/api/settings/hosts", {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Monitor-Request": "dashboard",
      },
      body: JSON.stringify({ action, host }),
    });
    if (response.status === 409) {
      throw new RangeError("stale inventory");
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    view.inventory = normalizeInventory(await response.json());
    view.inventoryMessage = action === "add"
      ? `${host} 已加入监控，正在等待首轮数据`
      : `${host} 已从监控配置移除`;
    view.inventoryMessageKind = "success";
    if (action === "remove" && view.selectedHost === host) selectHost("all");
    await fetchSnapshot();
  } catch (error) {
    view.inventoryMessage = error instanceof RangeError
      ? "节点清单已变化，已重新扫描，请再试一次"
      : "节点配置更新失败，请重新扫描并检查服务权限";
    view.inventoryMessageKind = "error";
    if (error instanceof RangeError) {
      view.inventoryPendingHost = null;
      await refreshInventory();
      view.inventoryMessage = "节点清单已变化，已重新扫描，请再试一次";
      view.inventoryMessageKind = "error";
      renderInventory();
      return;
    }
  } finally {
    view.inventoryPendingHost = null;
    renderInventory();
  }
}

function requestInventoryRemoval(host) {
  if (view.inventoryConfirmHost === host) {
    if (view.inventoryConfirmTimer != null) clearTimeout(view.inventoryConfirmTimer);
    view.inventoryConfirmTimer = null;
    changeInventory("remove", host);
    return;
  }
  view.inventoryConfirmHost = host;
  if (view.inventoryConfirmTimer != null) clearTimeout(view.inventoryConfirmTimer);
  view.inventoryConfirmTimer = setTimeout(() => {
    view.inventoryConfirmHost = null;
    view.inventoryConfirmTimer = null;
    renderInventory();
  }, 4000);
  renderInventory();
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
  const states = {
    online: ["在线", "online"],
    unreachable: ["SSH 不可达", "issue"],
    no_nvidia_smi: ["无 nvidia-smi", "issue"],
    error: ["采集错误", "issue"],
    pending: ["等待探测", "pending"],
  };
  const current = server.stale
    ? ["数据陈旧", "stale"]
    : states[server.status] || ["未知", "issue"];
  return server.maintenance ? [`维护中 · ${current[0]}`, "maintenance"] : current;
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
    .filter((condition) => condition.host === server.host && !condition.silenced)
    .map((condition) => ({
      id: condition.conditionKey,
      kind: condition.category,
      severity: condition.severity,
      priority: condition.category === "connectivity"
        ? 3 : condition.severity === "critical" ? 2 : 1,
      message: condition.category === "connectivity" && server.status === "online"
        ? "SSH 已恢复，等待稳定确认"
        : incidentConditionMessage(condition),
      device: String(condition.resource || ""),
      usage: condition.value == null ? -1 : numeric(condition.value, -1),
      sharedKey: condition.groupKey || null,
    }));
}

function capacityConditions(host) {
  if (!Array.isArray(view.incidents?.active)) return [];
  return view.incidents.active.filter(
    (condition) => condition.host === host && !condition.silenced,
  );
}

function gpuHasCapacityBlocker(gpu, conditions) {
  const identity = String(gpu.uuid || gpu.index);
  const resourcePrefix = `GPU ${gpu.index}`;
  return conditions.some((condition) => {
    if (!CAPACITY_GPU_BLOCKERS.has(condition.category)) return false;
    const key = String(condition.conditionKey || "");
    const resource = String(condition.resource || "");
    return key.endsWith(`:${identity}`)
      || resource === resourcePrefix
      || resource.startsWith(`${resourcePrefix} `);
  });
}

function capacityMatches() {
  const request = view.capacityRequest;
  const minimumFreeMiB = request.minVramGiB * 1024;
  const busyThreshold = limits().gpu_busy_pct;
  const temperatureThreshold = limits().gpu_temperature_warning_c;
  const candidates = [];
  let excludedMaintenance = 0;
  let excludedHealth = 0;

  view.snapshot.servers.forEach((server) => {
    if (server.status !== "online" || server.stale) return;
    if (server.maintenance) {
      excludedMaintenance += 1;
      return;
    }
    const conditions = capacityConditions(server.host);
    if (conditions.some((condition) => CAPACITY_HOST_BLOCKERS.has(condition.category))) {
      excludedHealth += 1;
      return;
    }
    const groups = new Map();
    server.gpus.forEach((gpu) => {
      const model = gpu.name || "Unknown NVIDIA GPU";
      if (request.model !== "any" && model !== request.model) return;
      const group = groups.get(model) || [];
      group.push(gpu);
      groups.set(model, group);
    });
    groups.forEach((gpus, model) => {
      const available = gpus.filter((gpu) => {
        const utilization = optionalMetric(gpu, "utilization_gpu_pct");
        const freeMemory = optionalMetric(gpu, "memory_free_mib");
        const temperature = optionalMetric(gpu, "temperature_c");
        return Number.isFinite(utilization)
          && utilization < busyThreshold
          && Number.isFinite(freeMemory)
          && freeMemory >= minimumFreeMiB
          && (!Number.isFinite(temperature) || temperature < temperatureThreshold)
          && !gpuHasCapacityBlocker(gpu, conditions);
      });
      const freeValues = available.map((gpu) => numeric(gpu.memory_free_mib));
      const utilizationValues = available.map((gpu) => numeric(gpu.utilization_gpu_pct));
      candidates.push({
        host: server.host,
        model,
        total: gpus.length,
        available,
        satisfies: available.length >= request.gpuCount,
        deficit: Math.max(0, request.gpuCount - available.length),
        minimumFreeMiB: freeValues.length ? Math.min(...freeValues) : 0,
        averageUtilization: utilizationValues.length
          ? utilizationValues.reduce((sum, value) => sum + value, 0) / utilizationValues.length
          : 101,
        cpuUsage: optionalMetric(server.system || {}, "cpu_usage_pct"),
      });
    });
  });
  candidates.sort((first, second) => (
    Number(second.satisfies) - Number(first.satisfies)
    || first.deficit - second.deficit
    || second.available.length - first.available.length
    || second.minimumFreeMiB - first.minimumFreeMiB
    || first.averageUtilization - second.averageUtilization
    || first.host.localeCompare(second.host)
  ));
  return { candidates, excludedMaintenance, excludedHealth };
}

function syncCapacityModels() {
  const models = [...new Set(
    view.snapshot.servers.flatMap((server) =>
      server.gpus.map((gpu) => gpu.name || "Unknown NVIDIA GPU")),
  )].sort((first, second) => first.localeCompare(second));
  const signature = JSON.stringify(models);
  if (signature === view.capacityModelSignature) return;
  view.capacityModelSignature = signature;
  const selected = models.includes(view.capacityRequest.model)
    ? view.capacityRequest.model : "any";
  const options = [create("option", "", "不限型号")];
  options[0].value = "any";
  models.forEach((model) => {
    const option = create("option", "", model);
    option.value = model;
    options.push(option);
  });
  elements.capacityModel.replaceChildren(...options);
  elements.capacityModel.value = selected;
  view.capacityRequest.model = selected;
}

function capacityCandidateCard(candidate) {
  const card = create(
    "article",
    `capacity-candidate ${candidate.satisfies ? "match" : "near"}`,
  );
  const heading = create("div", "capacity-candidate-heading");
  const identity = create("span", "capacity-candidate-identity");
  identity.append(
    create("strong", "", candidate.host),
    create("small", "", candidate.model),
  );
  heading.append(
    identity,
    create(
      "em",
      "",
      candidate.satisfies ? "满足需求" : `还差 ${candidate.deficit} 张`,
    ),
  );
  const metrics = create("div", "capacity-candidate-metrics");
  metrics.append(
    create("span", "", `可用 ${candidate.available.length} / ${candidate.total}`),
    create(
      "span",
      "",
      candidate.available.length
        ? `最低空闲 ${memory(candidate.minimumFreeMiB)}` : "没有符合条件的 GPU",
    ),
    create(
      "span",
      "",
      candidate.averageUtilization <= 100
        ? `平均负载 ${format(candidate.averageUtilization, 1)}%` : "负载未知",
    ),
    create(
      "span",
      "",
      Number.isFinite(candidate.cpuUsage)
        ? `CPU ${format(candidate.cpuUsage, 1)}%` : "CPU 采样中",
    ),
  );
  const devices = create("div", "capacity-devices");
  candidate.available.slice(0, 12).forEach((gpu) => {
    devices.append(
      create("span", "", `GPU ${gpu.index} · ${memory(gpu.memory_free_mib)} 空闲`),
    );
  });
  if (candidate.available.length > 12) {
    devices.append(create("span", "", `+${candidate.available.length - 12}`));
  }
  const locate = create("button", "inline-action", "查看节点");
  locate.type = "button";
  locate.addEventListener("click", () => {
    elements.capacityDialog.close();
    selectHost(candidate.host);
  });
  card.append(heading, metrics, devices, locate);
  return card;
}

function renderCapacityMatcher() {
  if (!elements.capacityDialog.open || !view.snapshot) return;
  syncCapacityModels();
  const request = view.capacityRequest;
  const result = capacityMatches();
  const exact = result.candidates.filter((candidate) => candidate.satisfies).length;
  elements.capacityRule.textContent = `空闲 = GPU 负载低于 ${format(limits().gpu_busy_pct)}% · 单卡可用显存至少 ${format(request.minVramGiB)} GiB · 无 GPU 硬件告警`;
  elements.capacityUpdated.textContent = age(view.snapshot.lastPollCompletedAt);
  elements.capacitySummary.textContent = exact
    ? `${exact} 个节点 / 型号组合满足 ${request.gpuCount} 张 GPU`
    : `暂无组合满足 ${request.gpuCount} 张 GPU，显示最接近结果`;
  const visible = exact
    ? result.candidates.filter((candidate) => candidate.satisfies).slice(0, 12)
    : result.candidates.slice(0, 8);
  if (!visible.length) {
    const detail = [
      result.excludedMaintenance ? `${result.excludedMaintenance} 台维护中` : "",
      result.excludedHealth ? `${result.excludedHealth} 台存在资产或连接告警` : "",
    ].filter(Boolean).join("，");
    elements.capacityResults.replaceChildren(
      create("div", "capacity-empty", detail || "当前没有可用于匹配的在线 GPU 节点"),
    );
    return;
  }
  elements.capacityResults.replaceChildren(...visible.map(capacityCandidateCard));
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
    const item = create(
      "button",
      `incident-item ${stateClass}${event.silenced ? " silenced" : ""}`,
    );
    item.type = "button";
    item.title = `${event.host}：${incidentDescription(event)}`;
    const body = create("span", "incident-body");
    const title = create("span", "incident-title");
    title.append(
      create("strong", "", event.host),
      create("em", "", incidentStateLabel(event.state)),
    );
    if (event.silenced) {
      const badge = create("em", "incident-silenced", "维护静默");
      badge.title = event.maintenanceReason || "维护窗口内暂不处理";
      title.append(badge);
    }
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
    if (view.serverSort === "group") {
      const firstGroup = hostGroupName(a);
      const secondGroup = hostGroupName(b);
      if (!firstGroup && secondGroup) return 1;
      if (firstGroup && !secondGroup) return -1;
      return firstGroup.localeCompare(secondGroup)
        || numeric(order.get(a.host), Number.MAX_SAFE_INTEGER)
        - numeric(order.get(b.host), Number.MAX_SAFE_INTEGER);
    }
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

function hostGroupName(server) {
  return typeof server.group === "string" && server.group.trim()
    ? server.group.trim() : "";
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
  const actionableIssues = numeric(
    snapshot.stats.actionableIssueServers,
    snapshot.stats.issueServers,
  );
  const actionableCritical = numeric(
    snapshot.stats.actionableCriticalIncidents,
    snapshot.stats.criticalIncidents,
  );
  const maintenanceServers = numeric(snapshot.stats.maintenanceServers);
  const serverCritical = actionableCritical > 0
    || (actionableIssues > 0
      && snapshot.stats.servers > 0
      && snapshot.stats.onlineServers === 0);
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
  elements.serverHealth.textContent = actionableCritical
    ? "严重" : actionableIssues ? "需关注" : maintenanceServers ? "维护中" : "健康";
  elements.serverHealth.classList.toggle(
    "warning",
    actionableIssues > 0 && !serverCritical,
  );
  elements.serverHealth.classList.toggle("critical", serverCritical);
  elements.serverCard.classList.toggle("is-warning", actionableIssues > 0 && !serverCritical);
  elements.serverCard.classList.toggle("is-critical", serverCritical);
  elements.serverBar.style.width = `${clamp(onlineRatio)}%`;
  elements.serverDetail.textContent = actionableIssues
    ? `${actionableIssues} 台需关注 · ${numeric(snapshot.stats.actionableIncidents, snapshot.stats.activeIncidents)} 个待处理问题`
    : maintenanceServers && snapshot.stats.activeIncidents
      ? `${maintenanceServers} 台维护中 · ${snapshot.stats.activeIncidents} 个活动问题已静默`
      : maintenanceServers
        ? `${maintenanceServers} 台处于计划维护窗口`
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
  const group = hostGroupName(server);
  if (group) identity.append(create("span", "server-group-badge", group));
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
    server.maintenance?.until,
    server.maintenance?.reason,
    hostGroupName(server),
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

function fleetGroupHeader(group, servers) {
  const label = group || "未分组";
  const gpuCount = servers.reduce((sum, server) => sum + server.gpus.length, 0);
  const signature = `${label}:${servers.length}:${gpuCount}`;
  const cacheKey = group || "\u0000";
  const cached = view.fleetGroupCache.get(cacheKey);
  if (cached?.signature === signature) return cached.node;
  const node = create("div", "fleet-group-heading");
  node.setAttribute("role", "presentation");
  node.append(
    create("strong", "", label),
    create("span", "", `${servers.length} 节点 · ${gpuCount} GPU`),
  );
  view.fleetGroupCache.set(cacheKey, { signature, node });
  return node;
}

function renderServers() {
  if (!view.snapshot) return;
  const servers = orderedServers(focusedServers(view.snapshot.servers));
  elements.serverCount.textContent = view.serverFilter === "all"
    ? String(view.snapshot.stats.servers)
    : `${servers.length}/${view.snapshot.stats.servers}`;
  const gpuCount = servers.reduce((sum, server) => sum + server.gpus.length, 0);
  const desired = [fleetAllItem(SERVER_FILTER_LABELS[view.serverFilter], gpuCount)];
  const grouped = view.serverSort === "group" && servers.some(hostGroupName);
  const visibleGroupKeys = new Set();
  if (grouped) {
    let start = 0;
    while (start < servers.length) {
      const group = hostGroupName(servers[start]);
      let end = start + 1;
      while (end < servers.length && hostGroupName(servers[end]) === group) end += 1;
      const members = servers.slice(start, end);
      desired.push(fleetGroupHeader(group, members), ...members.map(cachedServerItem));
      visibleGroupKeys.add(group || "\u0000");
      start = end;
    }
  } else {
    desired.push(...servers.map(cachedServerItem));
  }
  if (!servers.length) {
    view.fleetEmptyNode ||= create("div", "fleet-empty", "当前筛选没有匹配节点");
    desired.push(view.fleetEmptyNode);
  }
  const knownHosts = new Set(view.snapshot.servers.map((server) => server.host));
  [...view.serverItemCache.keys()].forEach((host) => {
    if (!knownHosts.has(host)) view.serverItemCache.delete(host);
  });
  [...view.fleetGroupCache.keys()].forEach((group) => {
    if (!visibleGroupKeys.has(group)) view.fleetGroupCache.delete(group);
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
  if (elements.capacityDialog.open) elements.capacityDialog.close();
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
  renderCapacityMatcher();
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
    renderCapacityMatcher();
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
    view.serverFilter = button.dataset.serverFilter;
    syncPreferenceControls();
    savePreferences();
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
  if (elements.capacityDialog.open) elements.capacityDialog.close();
  elements.settingsDialog.showModal();
  refreshInventory();
});

elements.capacityToggle.addEventListener("click", () => {
  if (!view.snapshot) return;
  if (elements.settingsDialog.open) elements.settingsDialog.close();
  if (elements.gpuDetailDialog.open) elements.gpuDetailDialog.close();
  elements.capacityGpuCount.value = String(view.capacityRequest.gpuCount);
  elements.capacityVram.value = String(view.capacityRequest.minVramGiB);
  syncCapacityModels();
  elements.capacityModel.value = view.capacityRequest.model;
  elements.capacityDialog.showModal();
  renderCapacityMatcher();
});

elements.capacityForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!elements.capacityForm.reportValidity()) return;
  const gpuCount = Number(elements.capacityGpuCount.value);
  const minVramGiB = Number(elements.capacityVram.value);
  if (
    !Number.isSafeInteger(gpuCount)
    || gpuCount < 1
    || gpuCount > 256
    || !Number.isInteger(minVramGiB)
    || minVramGiB < 0
    || minVramGiB > 512
  ) return;
  view.capacityRequest = {
    gpuCount,
    minVramGiB,
    model: elements.capacityModel.value,
  };
  renderCapacityMatcher();
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

elements.defaultServerFilter.addEventListener("change", () => {
  if (!SERVER_FILTER_VALUES.has(elements.defaultServerFilter.value)) return;
  view.serverFilter = elements.defaultServerFilter.value;
  syncPreferenceControls();
  savePreferences();
  if (view.selectedHost !== "all") selectHost("all");
  else render();
});

elements.interfaceDensity.addEventListener("change", () => {
  if (!DENSITY_VALUES.has(elements.interfaceDensity.value)) return;
  preferences.density = elements.interfaceDensity.value;
  syncPreferenceControls();
  savePreferences();
});

elements.backgroundVisibility.addEventListener("input", () => {
  preferences.backgroundVisibility = safeBackgroundVisibility(
    Number(elements.backgroundVisibility.value),
  );
  syncPreferenceControls();
  savePreferences();
});

elements.backgroundImageInput.addEventListener("change", selectBackgroundImage);
elements.removeBackgroundImage.addEventListener("click", removeBackgroundImage);

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

themeChoiceButtons.forEach((button, index) => {
  button.addEventListener("click", () => {
    const theme = button.dataset.themeChoice;
    if (!THEME_VALUES.has(theme)) return;
    preferences.theme = theme;
    syncPreferenceControls();
    savePreferences();
  });
  button.addEventListener("keydown", (event) => {
    const direction = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    let targetIndex = direction == null ? null : index + direction;
    if (event.key === "Home") targetIndex = 0;
    if (event.key === "End") targetIndex = themeChoiceButtons.length - 1;
    if (targetIndex == null) return;
    event.preventDefault();
    const target = themeChoiceButtons[
      (targetIndex + themeChoiceButtons.length) % themeChoiceButtons.length
    ];
    target.focus();
    target.click();
  });
});

elements.resetPreferences.addEventListener("click", resetPreferences);
elements.inventoryRefresh.addEventListener("click", refreshInventory);
elements.collectorSettingsForm.addEventListener("submit", saveCollectorSettings);
[
  elements.settingsPollInterval,
  elements.settingsProbeTimeout,
  elements.settingsMaxWorkers,
].forEach((field) => field.addEventListener("input", markCollectorSettingsDirty));

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
renderInventory();
loadStoredBackground();
fetchSnapshot();
connect();
