"use strict";

const {
  age, appendStreamChunk, clamp, combinedMetric, duration, durationSince, format,
  memory, numeric, optionalMetric, rate, ratio, retryCountdown, shortTime, storage,
} = globalThis.MocopFormat.create();

const PREFERENCE_STORAGE_KEY = "mocop.preferences.v1";
const VISUAL_ASSET_DATABASE = "mocop.visual-assets.v1";
const VISUAL_ASSET_STORE = "assets";
const BACKGROUND_ASSET_KEY = "background";
const MAX_BACKGROUND_BYTES = 8 * 1024 * 1024;
const MAX_BACKGROUND_SOURCE_BYTES = 32 * 1024 * 1024;
const MAX_BACKGROUND_DIMENSION = 8192;
const MAX_BACKGROUND_PIXELS = 32_000_000;
const MAX_COMPRESSED_BACKGROUND_DIMENSION = 4096;
const MAX_COMPRESSED_BACKGROUND_PIXELS = 12_000_000;
const MAX_GPU_DETAIL_PROCESSES = 100;
const MAX_PROGRAM_SEARCH_RESULTS = 200;
const MAX_SEARCH_QUERY_LENGTH = 120;
const MAX_HEATMAP_COLUMNS = 8;
const GPU_PROCESS_FRESHNESS_WARNING_MS = 90_000;
const gpuProcessSummaryCache = new WeakMap();
const BACKGROUND_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/avif"]);
const DEFAULT_PREFERENCES = Object.freeze({
  serverSort: "custom",
  serverOrder: [],
  gpuSort: "host",
  gpuTaskSort: "memory",
  heatMetric: "utilization",
  visualStyle: "precision",
  accent: "cobalt",
  density: "comfortable",
  backgroundVisibility: 38,
  serverFilter: "all",
  showTemperature: true,
  showPower: true,
});
const SERVER_SORT_VALUES = new Set(["custom", "group", "host", "status", "gpu", "cpu"]);
const GPU_SORT_VALUES = new Set([
  "host", "utilization", "memory", "temperature", "power", "processes",
]);
const GPU_TASK_SORT_VALUES = new Set(["memory", "duration", "name"]);
const HEAT_METRIC_VALUES = new Set(["utilization", "memory", "temperature"]);
const VISUAL_STYLE_VALUES = new Set([
  "precision", "glass", "terminal", "ledger", "blueprint", "studio",
]);
const ACCENT_VALUES = new Set(["cobalt", "cyan", "violet", "emerald", "amber", "rose"]);
const DENSITY_VALUES = new Set(["comfortable", "compact"]);
const SERVER_FILTER_VALUES = new Set(["all", "issues", "busy", "available", "stale"]);
const TOPOLOGY_TRANSPORT_VALUES = new Set(["ssh", "frp-stcp", "frp-xtcp", "vpn"]);
const TOPOLOGY_TRANSPORT_LABELS = Object.freeze({
  ssh: "SSH",
  "frp-stcp": "FRP · STCP",
  "frp-xtcp": "FRP · XTCP",
  vpn: "VPN",
});

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
      gpuTaskSort: GPU_TASK_SORT_VALUES.has(stored.gpuTaskSort)
        ? stored.gpuTaskSort : DEFAULT_PREFERENCES.gpuTaskSort,
      heatMetric: HEAT_METRIC_VALUES.has(stored.heatMetric)
        ? stored.heatMetric : DEFAULT_PREFERENCES.heatMetric,
      visualStyle: VISUAL_STYLE_VALUES.has(stored.visualStyle)
        ? stored.visualStyle : DEFAULT_PREFERENCES.visualStyle,
      accent: ACCENT_VALUES.has(stored.accent)
        ? stored.accent : DEFAULT_PREFERENCES.accent,
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
document.documentElement.dataset.style = preferences.visualStyle;
document.documentElement.dataset.accent = preferences.accent;
document.documentElement.dataset.density = preferences.density;
document.documentElement.style.setProperty(
  "--custom-background-opacity",
  String(preferences.backgroundVisibility / 100),
);

// Every dashboard-originated request must carry the viewer marker: the
// service treats marked reads as "someone is watching" and keeps process
// sampling on the attended cadence even when SSE falls back to polling.
// The managed-service capability is delivered in the URL fragment, which is
// never sent by HTTP. Keep it in tab-scoped session storage so an intentional
// reload (including the managed restart workflow) remains usable, while never
// turning it into an ambient cookie or persistent cross-session credential.
const dashboardAuthentication = globalThis.MocopDashboardAuth.create(window);
// Shadowing the script-scope fetch keeps the viewer marker and explicit
// capability uniform for all call sites.
const nativeFetch = window.fetch.bind(window);
const fetch = (url, options = {}) => nativeFetch(url, {
  cache: "no-store",
  ...options,
  headers: {
    "X-Monitor-Request": "dashboard",
    ...(options.headers || {}),
    ...(dashboardAuthentication.token
      ? { Authorization: `Bearer ${dashboardAuthentication.token}` } : {}),
  },
});
// Every write route takes one JSON object and nothing else.
const postJson = (url, body) => fetch(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const WORKLOAD_KIND_LABELS = {
  process: "进程",
  slurm: "Slurm",
  kubernetes: "Kubernetes",
  docker: "Docker",
  podman: "Podman",
};

// compareProcessSearchRecords and processSearchRank are not called by the
// dashboard itself: the opt-in browser benchmark (tests/browser_smoke.mjs with
// MOCOP_PROGRAM_SEARCH_BENCHMARK=1) evaluates them in this page to cross-check
// the bounded heap against a full sort.
const {
  compareProcessSearchRecords,
  gpuRecordMatchesSearch,
  normalizedSearchTerms,
  processMatchesSearch,
  processMemoryRank,
  processSearchRank,
  searchProcessRecords,
} = globalThis.MocopProcessSearch.create({
  maxResults: MAX_PROGRAM_SEARCH_RESULTS,
  maxQueryLength: MAX_SEARCH_QUERY_LENGTH,
  workloadLabels: WORKLOAD_KIND_LABELS,
  processName: gpuProcessName,
  numeric,
});

const capacityMatch = globalThis.MocopCapacityMatch.create();
const gpuTasks = globalThis.MocopGpuTasks.create();
const capacityWatch = globalThis.MocopCapacityWatch.create({ storage: localStorage });

const view = {
  dashboardStarted: false,
  connectStarted: false,
  snapshotFailureStreak: 0,
  snapshot: null,
  topology: null,
  topologyLoading: false,
  topologyError: "",
  topologyRevision: 0,
  topologyRenderedRevision: -1,
  topologyNodeRefs: new Map(),
  topologyMappedHosts: new Set(),
  topologyUnmappedKey: "",
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
  historyError: false,
  historyFetchKey: null,
  historyRetryTimer: null,
  historyRetryKey: "",
  historyRetryDelayMs: 0,
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
  selectedPanelKey: "",
  incidents: null,
  incidentsByHost: new Map(),
  attentionRenderKey: "",
  incidentRenderKey: "",
  incidentVersion: -1,
  incidentRequest: 0,
  incidentLoadingVersion: null,
  incidentRetryTimer: null,
  incidentRetryVersion: null,
  incidentRetryDelayMs: 0,
  incidentSyncFailed: false,
  incidentExpanded: false,
  transportKind: "connecting",
  transportLabel: "连接中",
  refreshFeedbackTimer: null,
  cadenceSnapshotFloor: null,
  snapshotFetchInFlight: null,
  draggedHost: null,
  suppressServerClick: false,
  selectedGpu: null,
  gpuHistory: null,
  gpuHistoryKey: "",
  gpuHistoryRenderKey: "",
  gpuHistoryFetchKey: null,
  gpuHistoryRequest: 0,
  gpuHistoryLoading: false,
  gpuHistoryError: false,
  gpuHistoryRetryTimer: null,
  gpuHistoryRetryKey: "",
  gpuHistoryRetryDelayMs: 0,
  gpuTaskQuery: "",
  gpuTaskIdentityFilter: "all",
  gpuTaskFeedbackTimer: null,
  selectedProcessKey: "",
  ownersUsage: null,
  ownersUsageHours: 24,
  ownersUsageLoading: false,
  ownersUsageError: "",
  ownersUsageRequest: 0,
  gpuTaskRowCache: new Map(),
  programSearchRowCache: new Map(),
  selectedIncident: null,
  incidentActionPending: false,
  manualProbePending: false,
  notificationTestPending: false,
  capacityRequest: { gpuCount: 1, minVramGiB: 24, model: "any" },
  capacityModelSignature: "",
  capacityWatch: capacityWatch.loadWatch(),
  capacityWatchSatisfied: 0,
  capacityWatchBannerDismissed: false,
  baseDocumentTitle: document.title,
  inventory: null,
  inventoryLoading: false,
  inventoryMessage: "",
  inventoryMessageKind: "",
  inventoryPendingHost: null,
  inventoryConfirmHost: null,
  inventoryConfirmTimer: null,
  maintenanceEditingHost: null,
  maintenancePendingHost: null,
  maintenanceDraft: null,
  // Host whose settings row should be scrolled into view and focused once
  // the inventory list finishes loading (incident detail -> maintenance).
  maintenanceFocusHost: null,
  groupEditingHost: null,
  groupPendingHost: null,
  collectorSettingsDirty: false,
  collectorSettingsSaving: false,
  serviceRestartSupported: false,
  serviceRestartLoading: false,
  serviceRestarting: false,
  backgroundObjectUrl: null,
  backgroundRequestId: 0,
  backgroundStorageTail: Promise.resolve(),
  authenticationFailed: false,
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
  authenticationDialog: $("#authentication-dialog"),
  authenticationForm: $("#authentication-form"),
  authenticationToken: $("#authentication-token"),
  authenticationSubmit: $("#authentication-submit"),
  authenticationStatus: $("#authentication-status"),
  settingsToggle: $("#settings-toggle"),
  settingsDialog: $("#settings-dialog"),
  topologyToggle: $("#topology-toggle"),
  topologyDialog: $("#topology-dialog"),
  topologyNodeCount: $("#topology-node-count"),
  topologyFrpCount: $("#topology-frp-count"),
  topologyLiveSummary: $("#topology-live-summary"),
  topologyStatus: $("#topology-status"),
  topologyTree: $("#topology-tree"),
  topologyUnmapped: $("#topology-unmapped"),
  topologyUnmappedList: $("#topology-unmapped-list"),
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
  capacityWatchToggle: $("#capacity-watch-toggle"),
  capacityWatchStatus: $("#capacity-watch-status"),
  capacityWatchBanner: $("#capacity-watch-banner"),
  capacityWatchBannerText: $("#capacity-watch-banner-text"),
  capacityWatchBannerOpen: $("#capacity-watch-banner-open"),
  capacityWatchBannerDismiss: $("#capacity-watch-banner-dismiss"),
  capacityWatchBannerStop: $("#capacity-watch-banner-stop"),
  ownersToggle: $("#owners-toggle"),
  ownersDialog: $("#owners-dialog"),
  ownersSummary: $("#owners-summary"),
  ownersUpdated: $("#owners-updated"),
  ownersResults: $("#owners-results"),
  ownersUsageSummary: $("#owners-usage-summary"),
  ownersUsageHours: $("#owners-usage-hours"),
  ownersUsageResults: $("#owners-usage-results"),
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
  persistenceStatus: $("#persistence-status"),
  notificationStatus: $("#notification-status"),
  notificationEndpoints: $("#notification-endpoints"),
  notificationTest: $("#test-notifications"),
  notificationTestStatus: $("#notification-test-status"),
  exportDiagnostics: $("#export-diagnostics"),
  restartService: $("#restart-service"),
  restartConfirmDialog: $("#restart-confirm-dialog"),
  confirmRestartService: $("#confirm-restart-service"),
  serviceRestartStatus: $("#service-restart-status"),
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
  serverOrderStatus: $("#server-order-status"),
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
  probeNow: $("#probe-now"),
  groupToggle: $("#toggle-groups"),
  gpuGroups: $("#gpu-groups"),
  emptyState: $("#empty-state"),
  programSearchPanel: $("#program-search-panel"),
  programSearchScope: $("#program-search-scope"),
  programSearchCount: $("#program-search-count"),
  programSearchSummary: $("#program-search-summary"),
  programSearchResults: $("#program-search-results"),
  pollInfo: $("#poll-info"),
  gpuDetailDialog: $("#gpu-detail-dialog"),
  gpuDetailHost: $("#gpu-detail-host"),
  gpuDetailTitle: $("#gpu-detail-title"),
  gpuDetailSsh: $("#gpu-detail-ssh"),
  gpuDetailState: $("#gpu-detail-state"),
  gpuDetailMetrics: $("#gpu-detail-metrics"),
  gpuHistoryRange: $("#gpu-history-range"),
  gpuHistoryGrid: $("#gpu-history-grid"),
  gpuProcessTimeline: $("#gpu-process-timeline"),
  gpuTaskCount: $("#gpu-task-count"),
  gpuTaskInsights: $("#gpu-task-insights"),
  gpuTaskFeedback: $("#gpu-task-feedback"),
  gpuTaskOverview: $("#gpu-task-overview"),
  gpuTaskMemoryTotal: $("#gpu-task-memory-total"),
  gpuTaskMemoryBar: $("#gpu-task-memory-bar"),
  gpuTaskNote: $("#gpu-task-note"),
  gpuTaskHeadingTools: $("#gpu-task-heading-tools"),
  gpuTaskSearch: $("#gpu-task-search"),
  gpuTaskList: $("#gpu-task-list"),
  incidentDetailDialog: $("#incident-detail-dialog"),
  incidentDetailHost: $("#incident-detail-host"),
  incidentDetailTitle: $("#incident-detail-title"),
  incidentDetailStatus: $("#incident-detail-status"),
  incidentDetailSummary: $("#incident-detail-summary"),
  incidentEvidence: $("#incident-evidence"),
  incidentNextSteps: $("#incident-next-steps"),
  incidentOpenGpu: $("#incident-open-gpu"),
  incidentActionDuration: $("#incident-action-duration"),
  incidentActionReason: $("#incident-action-reason"),
  acknowledgeIncident: $("#acknowledge-incident"),
  silenceIncident: $("#silence-incident"),
  clearIncidentAction: $("#clear-incident-action"),
  incidentOpenMaintenance: $("#incident-open-maintenance"),
  incidentActionFeedback: $("#incident-action-feedback"),
};

// The six feature dialogs are mutually exclusive modals; the authentication
// prompt is deliberately not in this set so nothing can dismiss it.
const FEATURE_DIALOGS = [
  elements.settingsDialog,
  elements.topologyDialog,
  elements.gpuDetailDialog,
  elements.capacityDialog,
  elements.ownersDialog,
  elements.incidentDetailDialog,
];

function openExclusiveDialog(dialog) {
  FEATURE_DIALOGS.forEach((other) => {
    if (other !== dialog && other.open) other.close();
  });
  if (!dialog.open) dialog.showModal();
}
const styleChoiceButtons = [...document.querySelectorAll("[data-style-choice]")];
const accentChoiceButtons = [...document.querySelectorAll("[data-accent-choice]")];

function create(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

// Binary presentation assets stay out of both synchronous preferences and the service.
function openVisualAssetDatabase() {
  return new Promise((resolve, reject) => {
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

async function decodeImage(blob) {
  const bitmap = await createImageBitmap(blob);
  return {
    source: bitmap,
    width: bitmap.width,
    height: bitmap.height,
    release: () => bitmap.close(),
  };
}

async function decodeImageSize(blob) {
  const decoded = await decodeImage(blob);
  try {
    return { width: decoded.width, height: decoded.height };
  } finally {
    decoded.release();
  }
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

async function validateBackgroundBlob(blob, maxBytes = MAX_BACKGROUND_BYTES) {
  if (!(blob instanceof Blob) || !BACKGROUND_TYPES.has(blob.type)) {
    throw new Error("仅支持 PNG、JPEG、WebP 或 AVIF 图片");
  }
  if (blob.size <= 0) {
    throw new Error("图片内容不能为空");
  }
  if (blob.size > maxBytes) {
    const limit = maxBytes === MAX_BACKGROUND_BYTES ? 8 : 32;
    throw new Error(`图片大小不能超过 ${limit} MiB`);
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

function canvasToWebp(canvas, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!(blob instanceof Blob) || blob.size <= 0 || blob.type !== "image/webp") {
        reject(new Error("当前浏览器不支持 WebP 图片压缩"));
        return;
      }
      resolve(blob);
    }, "image/webp", quality);
  });
}

async function encodeCanvasWithinLimit(canvas) {
  const highQuality = await canvasToWebp(canvas, 0.9);
  if (highQuality.size <= MAX_BACKGROUND_BYTES) {
    return { blob: highQuality, smallestSize: highQuality.size };
  }

  const lowQuality = await canvasToWebp(canvas, 0.5);
  if (lowQuality.size > MAX_BACKGROUND_BYTES) {
    return { blob: null, smallestSize: lowQuality.size };
  }

  let best = lowQuality;
  let lowerQuality = 0.5;
  let upperQuality = 0.9;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const quality = (lowerQuality + upperQuality) / 2;
    const candidate = await canvasToWebp(canvas, quality);
    if (candidate.size <= MAX_BACKGROUND_BYTES) {
      best = candidate;
      lowerQuality = quality;
    } else {
      upperQuality = quality;
    }
  }
  return { blob: best, smallestSize: best.size };
}

async function compressBackgroundBlob(blob, dimensions) {
  const decoded = await decodeImage(blob);
  const canvas = document.createElement("canvas");
  const initialScale = Math.min(
    1,
    MAX_COMPRESSED_BACKGROUND_DIMENSION / dimensions.width,
    MAX_COMPRESSED_BACKGROUND_DIMENSION / dimensions.height,
    Math.sqrt(MAX_COMPRESSED_BACKGROUND_PIXELS / (dimensions.width * dimensions.height)),
  );
  let width = Math.max(1, Math.floor(dimensions.width * initialScale));
  let height = Math.max(1, Math.floor(dimensions.height * initialScale));

  try {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("当前浏览器无法处理这张图片");
      context.imageSmoothingEnabled = true;
      context.imageSmoothingQuality = "high";
      context.drawImage(decoded.source, 0, 0, width, height);

      const encoded = await encodeCanvasWithinLimit(canvas);
      if (encoded.blob) {
        return encoded.blob;
      }

      const shrink = Math.min(
        0.82,
        Math.sqrt(MAX_BACKGROUND_BYTES / encoded.smallestSize) * 0.92,
      );
      const nextWidth = Math.max(1, Math.floor(width * shrink));
      const nextHeight = Math.max(1, Math.floor(height * shrink));
      if (nextWidth === width && nextHeight === height) break;
      width = nextWidth;
      height = nextHeight;
    }
  } finally {
    decoded.release();
    canvas.width = 1;
    canvas.height = 1;
  }
  throw new Error("无法在安全限制内压缩这张图片");
}

async function prepareBackgroundBlob(blob) {
  const dimensions = await validateBackgroundBlob(blob, MAX_BACKGROUND_SOURCE_BYTES);
  if (blob.size <= MAX_BACKGROUND_BYTES) {
    return { blob, dimensions, compressed: false };
  }
  const compressed = await compressBackgroundBlob(blob, dimensions);
  const validatedDimensions = await validateBackgroundBlob(compressed);
  return {
    blob: compressed,
    dimensions: validatedDimensions,
    compressed: true,
  };
}

function backgroundSize(blob) {
  if (blob.size < 0.1 * 1024 * 1024) {
    return `${Math.max(1, Math.ceil(blob.size / 1024))} KiB`;
  }
  return `${(blob.size / (1024 * 1024)).toFixed(1)} MiB`;
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
  elements.removeBackgroundImage.disabled = true;
  setBackgroundStatus(
    file.size > MAX_BACKGROUND_BYTES ? "正在浏览器本地优化图片…" : "正在安全读取图片…",
  );
  try {
    const prepared = await prepareBackgroundBlob(file);
    if (requestId !== view.backgroundRequestId) return;
    try {
      await runBackgroundStorage(() => writeStoredBackground(prepared.blob));
      if (requestId !== view.backgroundRequestId) return;
      renderBackground(prepared.blob);
      const prefix = prepared.compressed
        ? `已压缩至 ${backgroundSize(prepared.blob)} 并保存在当前浏览器`
        : "已保存在当前浏览器";
      setBackgroundStatus(
        `${prefix} · ${prepared.dimensions.width} × ${prepared.dimensions.height}`,
        "success",
      );
    } catch (_error) {
      if (requestId !== view.backgroundRequestId) return;
      renderBackground(prepared.blob);
      const prefix = prepared.compressed
        ? `已压缩至 ${backgroundSize(prepared.blob)}；`
        : "";
      setBackgroundStatus(`${prefix}浏览器存储空间不足；背景仅在本次会话有效`, "error");
    }
  } catch (error) {
    if (requestId === view.backgroundRequestId) {
      setBackgroundStatus(error instanceof Error ? error.message : "无法使用这张图片", "error");
    }
  } finally {
    if (requestId === view.backgroundRequestId) {
      elements.backgroundImageInput.disabled = false;
      elements.removeBackgroundImage.disabled = !view.backgroundObjectUrl;
    }
  }
}

async function removeBackgroundImage() {
  const requestId = ++view.backgroundRequestId;
  elements.backgroundImageInput.disabled = true;
  elements.removeBackgroundImage.disabled = true;
  setBackgroundStatus("正在移除背景…");
  try {
    await runBackgroundStorage(deleteStoredBackground);
    if (requestId !== view.backgroundRequestId) return;
    clearRenderedBackground();
    setBackgroundStatus("背景已从当前浏览器移除", "success");
  } catch (_error) {
    if (requestId === view.backgroundRequestId) {
      setBackgroundStatus("无法更新浏览器存储，背景未移除", "error");
    }
  } finally {
    if (requestId === view.backgroundRequestId) {
      elements.backgroundImageInput.disabled = false;
      elements.removeBackgroundImage.disabled = !view.backgroundObjectUrl;
    }
  }
}

async function runBackgroundStorage(operation) {
  const previous = view.backgroundStorageTail;
  let release;
  view.backgroundStorageTail = new Promise((resolve) => { release = resolve; });
  await previous;
  try {
    return await operation();
  } finally {
    release();
  }
}

function savePreferences() {
  const value = {
    serverSort: view.serverSort,
    serverOrder: view.serverOrder,
    gpuSort: view.sort,
    gpuTaskSort: preferences.gpuTaskSort,
    heatMetric: view.heatMetric,
    visualStyle: preferences.visualStyle,
    accent: preferences.accent,
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
  document.documentElement.dataset.style = preferences.visualStyle;
  document.documentElement.dataset.accent = preferences.accent;
  document.documentElement.dataset.density = preferences.density;
  document.documentElement.style.setProperty(
    "--custom-background-opacity",
    String(preferences.backgroundVisibility / 100),
  );
  document.querySelectorAll(".fleet-filter").forEach((button) => {
    const selected = button.dataset.serverFilter === view.serverFilter;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  styleChoiceButtons.forEach((button) => {
    const selected = button.dataset.styleChoice === preferences.visualStyle;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-checked", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  accentChoiceButtons.forEach((button) => {
    const selected = button.dataset.accentChoice === preferences.accent;
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
  preferences.gpuTaskSort = DEFAULT_PREFERENCES.gpuTaskSort;
  view.heatMetric = DEFAULT_PREFERENCES.heatMetric;
  preferences.visualStyle = DEFAULT_PREFERENCES.visualStyle;
  preferences.accent = DEFAULT_PREFERENCES.accent;
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

function refreshRelativeTimes() {
  // One clock read per tick, and unchanged text stays untouched so the
  // every-second loop does not invalidate layout for stable rows.
  const now = Date.now();
  const setText = (element, text) => {
    if (element.textContent !== text) element.textContent = text;
  };
  document.querySelectorAll("[data-retry-at]").forEach((element) => {
    setText(element, retryCountdown(element.dataset.retryAt, now));
  });
  document.querySelectorAll("[data-age-at]").forEach((element) => {
    setText(element, age(element.dataset.ageAt, now));
  });
  // Duration rows only exist inside the GPU detail dialog.
  if (!elements.gpuDetailDialog.open) return;
  document.querySelectorAll("[data-duration-since]").forEach((element) => {
    setText(element, `${element.dataset.durationPrefix || ""}${
      durationSince(element.dataset.durationSince, now)}`);
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
  const staleAfterSeconds = view.snapshot.collectionStaleAfterSeconds;
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
      title = "实时通道已连接，正在等待首个采集批次完成";
    } else if (health.state === "delayed") {
      kind = "delayed";
      label = "采集延迟";
      title = `实时通道已连接，但最近采集批次完成于 ${age(view.snapshot.lastPollCompletedAt)}`;
    } else {
      title = `最近采集批次完成于 ${age(view.snapshot.lastPollCompletedAt)}`;
    }
  }
  // Runs every second: unchanged values must not invalidate style or layout.
  const className = `connection ${kind}`;
  if (elements.connection.className !== className) elements.connection.className = className;
  if (elements.connectionText.textContent !== label) elements.connectionText.textContent = label;
  if (elements.connection.title !== title) elements.connection.title = title;
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
  // Renderers dereference the envelope unguarded: the server-owned
  // thresholds replace any threshold policy in this file, stats always carry
  // the actionable counts, and servers is the spine of every view. A
  // structurally broken snapshot must therefore be rejected here instead of
  // replacing good state and freezing every later render.
  if (
    !snapshot
    || typeof snapshot !== "object"
    || !Number.isSafeInteger(snapshot.version)
    || typeof snapshot.startedAt !== "string"
    || typeof snapshot.collectionStaleAfterSeconds !== "number"
    || !snapshot.thresholds
    || typeof snapshot.thresholds !== "object"
    || !snapshot.stats
    || typeof snapshot.stats !== "object"
    || !Array.isArray(snapshot.servers)
    || snapshot.servers.some(
      (server) => !server || typeof server.host !== "string" || !Array.isArray(server.gpus),
    )
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
    // The collector endpoint accepts a field subset; the poll-interval
    // endpoint is deprecated and only kept server-side for older pages.
    const response = await postJson("/api/settings/collector", { pollIntervalSeconds: requested });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const pollIntervalSeconds = payload.collectorSettings?.pollIntervalSeconds;
    if (
      !Number.isSafeInteger(payload.version)
      || typeof payload.startedAt !== "string"
      || typeof pollIntervalSeconds !== "number"
      || !Number.isFinite(pollIntervalSeconds)
    ) {
      throw new TypeError("Invalid settings response");
    }
    view.cadenceSnapshotFloor = {
      version: payload.version,
      startedAt: payload.startedAt,
    };
    if (view.snapshot?.startedAt === payload.startedAt) {
      view.snapshot.pollIntervalSeconds = pollIntervalSeconds;
      view.snapshot.collectionStaleAfterSeconds = payload.collectionStaleAfterSeconds;
    }
    if (view.inventory?.collectorSettings) {
      view.inventory.collectorSettings.pollIntervalSeconds = pollIntervalSeconds;
      syncCollectorSettings();
    }
    elements.refreshInterval.value = String(pollIntervalSeconds);
    showRefreshFeedback("saved", `已保存为 ${format(pollIntervalSeconds)} 秒`);
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
  const {
    pollIntervalSeconds, probeTimeoutSeconds, connectTimeoutSeconds, maxWorkers,
  } = payload;
  if (
    typeof pollIntervalSeconds !== "number"
    || !Number.isFinite(pollIntervalSeconds)
    || pollIntervalSeconds < 1
    || pollIntervalSeconds > 3600
    || typeof probeTimeoutSeconds !== "number"
    || !Number.isFinite(probeTimeoutSeconds)
    || probeTimeoutSeconds < 2
    || probeTimeoutSeconds > 300
    // Read-only: the service reports it so the dialog can explain the
    // probe-timeout lower bound.
    || typeof connectTimeoutSeconds !== "number"
    || !Number.isFinite(connectTimeoutSeconds)
    || connectTimeoutSeconds <= 0
    || connectTimeoutSeconds > 300
    || !Number.isSafeInteger(maxWorkers)
    || maxWorkers < 1
    || maxWorkers > 64
  ) {
    throw new TypeError("Invalid collector settings response");
  }
  return { pollIntervalSeconds, probeTimeoutSeconds, connectTimeoutSeconds, maxWorkers };
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
  const infrastructureHosts = safeStoredHosts(payload.infrastructureHosts || []);
  const sshDiscoveryWarnings = Array.isArray(payload.sshDiscoveryWarnings)
    ? payload.sshDiscoveryWarnings.filter((item) => typeof item === "string").slice(0, 1024)
    : [];
  const sshDiscoveryMode = payload.sshDiscoveryMode || "aliases";
  if (
    configuredHosts.length !== payload.configuredHosts?.length
    || activeHosts.length !== payload.activeHosts?.length
    || availableHosts.length !== payload.availableHosts?.length
    || (payload.localHost != null && !safeStoredHosts([payload.localHost]).length)
    || typeof payload.autoDiscover !== "boolean"
    || typeof payload.writable !== "boolean"
    || !Number.isSafeInteger(payload.ignoredCodeHostCount)
    || !Number.isSafeInteger(payload.excludedHostCount)
    || !["aliases", "topology"].includes(sshDiscoveryMode)
    || infrastructureHosts.length !== (payload.infrastructureHosts || []).length
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
    infrastructureHosts, sshDiscoveryWarnings, sshDiscoveryMode,
  };
}

function normalizeTopology(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("Invalid topology response");
  }
  if (payload.root == null && Array.isArray(payload.links) && payload.links.length === 0) {
    return { root: null, links: [] };
  }
  const root = safeStoredHosts([payload.root])[0];
  if (!root || !Array.isArray(payload.links) || payload.links.length > 512) {
    throw new TypeError("Invalid topology response");
  }
  const targets = new Set();
  const children = new Map();
  const links = payload.links.map((link) => {
    if (!link || typeof link !== "object" || Array.isArray(link)) {
      throw new TypeError("Invalid topology link");
    }
    const keys = Object.keys(link);
    if (
      !["source", "target", "transport"].every((key) => keys.includes(key))
      || keys.some((key) => !["source", "target", "transport", "label"].includes(key))
    ) {
      throw new TypeError("Invalid topology link");
    }
    const source = safeStoredHosts([link.source])[0];
    const target = safeStoredHosts([link.target])[0];
    const label = link.label == null ? null : link.label;
    if (
      !source
      || !target
      || source === target
      || target === root
      || targets.has(target)
      || !TOPOLOGY_TRANSPORT_VALUES.has(link.transport)
      || (label != null && (
        typeof label !== "string"
        || !label
        || label !== label.trim()
        || [...label].length > 64
        || /\p{C}/u.test(label)
      ))
    ) {
      throw new TypeError("Invalid topology link");
    }
    targets.add(target);
    if (!children.has(source)) children.set(source, []);
    children.get(source).push(target);
    return { source, target, transport: link.transport, label };
  });
  const reachable = new Set([root]);
  const pending = [root];
  while (pending.length) {
    (children.get(pending.pop()) || []).forEach((target) => {
      if (reachable.has(target)) return;
      reachable.add(target);
      pending.push(target);
    });
  }
  if (links.some((link) => !reachable.has(link.source) || !reachable.has(link.target))) {
    throw new TypeError("Invalid topology tree");
  }
  return { root, links };
}

function displayHost(value) { const server = typeof value === "string" ? topologyServer(value) : value; return server?.displayName || server?.host || value || ""; }
function topologyServer(host) {
  return view.snapshot?.servers.find((server) => server.host === host) || null;
}

function updateTopologyNode(reference, host, server) {
  if (!server && reference.signature === "infrastructure") return;
  const state = !server
    ? "infrastructure"
    : server.status === "online" && !server.stale ? "online" : "offline";
  let metaText;
  let statusLabel;
  if (!server) {
    metaText = "连接路径节点 · 不采集资源";
    statusLabel = "连接路径节点，不采集资源";
  } else if (state === "offline") {
    metaText = server.stale ? "数据陈旧" : "连接不可用";
    statusLabel = metaText;
  } else {
    const cpu = numeric(server.system?.cpu_usage_pct, NaN);
    metaText = `${server.gpus.length} GPU · CPU ${Number.isFinite(cpu) ? `${format(cpu)}%` : "—"}`;
    statusLabel = metaText;
  }
  const signature = server ? `${displayHost(server)}\u0000${state}\u0000${metaText}` : "infrastructure";
  if (reference.signature === signature) return;
  reference.signature = signature;
  reference.button.className = `topology-node ${state}`;
  reference.button.disabled = !server;
  reference.button.querySelector("strong").textContent = displayHost(server || host); reference.meta.textContent = metaText;
  reference.button.setAttribute("aria-label", `${displayHost(server || host)}，${statusLabel}`);
}

function topologyNode(host, isRoot, nodeRefs) {
  const button = create("button", "topology-node infrastructure");
  button.type = "button";
  button.dataset.host = host;
  const heading = create("span", "topology-node-heading");
  heading.append(
    create("i", "topology-node-dot"),
    create("strong", "", displayHost(host)),
  );
  if (isRoot) heading.append(create("small", "topology-root-badge", "起点"));
  const meta = create("span", "topology-node-meta");
  button.append(heading, meta);
  button.addEventListener("click", () => {
    const server = topologyServer(host);
    if (!server) return;
    selectHost(host);
    elements.topologyDialog.close();
  });
  const reference = { button, meta, signature: "" };
  nodeRefs.set(host, reference);
  return button;
}

function topologyBranch(host, linksBySource, nodeRefs, isRoot = false) {
  const branch = create("li", "topology-branch");
  branch.append(topologyNode(host, isRoot, nodeRefs));
  const links = linksBySource.get(host) || [];
  if (!links.length) return branch;
  const children = create("ul", "topology-children");
  links.forEach((link) => {
    const child = create("li", "topology-child");
    const connector = create("div", `topology-connector ${link.transport}`);
    const label = link.label || TOPOLOGY_TRANSPORT_LABELS[link.transport];
    connector.setAttribute("role", "note");
    connector.setAttribute(
      "aria-label",
      `${host} 通过 ${label} 连接到 ${link.target}`,
    );
    const visibleLabel = create(
      "span",
      "topology-link-label",
      label,
    );
    visibleLabel.setAttribute("aria-hidden", "true");
    connector.append(visibleLabel);
    child.append(connector, topologyBranch(link.target, linksBySource, nodeRefs));
    children.append(child);
  });
  branch.append(children);
  return branch;
}

function resetTopologyGraph() {
  elements.topologyTree.replaceChildren();
  elements.topologyUnmappedList.replaceChildren();
  view.topologyRenderedRevision = -1;
  view.topologyNodeRefs = new Map();
  view.topologyMappedHosts = new Set();
  view.topologyUnmappedKey = "";
  elements.topologyUnmapped.hidden = true;
}

function buildTopologyGraph(topology) {
  const linksBySource = new Map();
  topology.links.forEach((link) => {
    if (!linksBySource.has(link.source)) linksBySource.set(link.source, []);
    linksBySource.get(link.source).push(link);
  });
  const nodeRefs = new Map();
  const tree = create("ul", "topology-tree-list");
  tree.append(topologyBranch(topology.root, linksBySource, nodeRefs, true));
  elements.topologyTree.replaceChildren(tree);
  view.topologyNodeRefs = nodeRefs;
  view.topologyMappedHosts = new Set([
    topology.root,
    ...topology.links.map((link) => link.target),
  ]);
  view.topologyRenderedRevision = view.topologyRevision;
}

function syncTopologyUnmapped(servers) {
  const key = servers.map((server) => `${server.host}\u0000${server.displayName || ""}`).join("\u0000");
  if (view.topologyUnmappedKey === key) return;
  view.topologyUnmappedKey = key;
  const buttons = servers.map((server) => {
    const button = create("button", "topology-unmapped-node", displayHost(server));
    button.type = "button";
    button.addEventListener("click", () => {
      selectHost(server.host);
      elements.topologyDialog.close();
    });
    return button;
  });
  elements.topologyUnmappedList.replaceChildren(...buttons);
}

function renderTopology() {
  if (!elements.topologyDialog.open) return;
  const topology = view.topology;
  if (view.topologyLoading && !topology) {
    resetTopologyGraph();
    elements.topologyStatus.hidden = false;
    elements.topologyStatus.className = "topology-status";
    elements.topologyStatus.textContent = "正在读取本地拓扑配置…";
    return;
  }
  if (view.topologyError) {
    resetTopologyGraph();
    elements.topologyStatus.hidden = false;
    elements.topologyStatus.className = "topology-status error";
    elements.topologyStatus.textContent = view.topologyError;
    return;
  }
  if (!topology?.root) {
    resetTopologyGraph();
    elements.topologyStatus.hidden = false;
    elements.topologyStatus.className = "topology-status";
    elements.topologyStatus.textContent = "尚未在 config.json 中配置连接拓扑";
    elements.topologyNodeCount.textContent = "0";
    elements.topologyFrpCount.textContent = "0";
    elements.topologyLiveSummary.textContent = "无拓扑配置";
    return;
  }
  if (view.topologyRenderedRevision !== view.topologyRevision) {
    buildTopologyGraph(topology);
  }
  const monitored = view.snapshot?.servers || [];
  const serverByHost = new Map(monitored.map((server) => [server.host, server]));
  view.topologyNodeRefs.forEach((reference, host) => {
    updateTopologyNode(reference, host, serverByHost.get(host) || null);
  });
  const mappedServers = monitored.filter(
    (server) => view.topologyMappedHosts.has(server.host),
  );
  const online = mappedServers.filter(
    (server) => server.status === "online" && !server.stale,
  ).length;
  elements.topologyNodeCount.textContent = String(view.topologyMappedHosts.size);
  elements.topologyFrpCount.textContent = String(
    topology.links.filter((link) => link.transport.startsWith("frp-")).length,
  );
  elements.topologyLiveSummary.textContent = `${online} / ${mappedServers.length} 个监控节点在线`;
  elements.topologyStatus.hidden = true;

  const unmapped = monitored.filter(
    (server) => !view.topologyMappedHosts.has(server.host),
  );
  elements.topologyUnmapped.hidden = unmapped.length === 0;
  syncTopologyUnmapped(unmapped);
}

async function fetchTopology() {
  if (view.topologyLoading) return;
  view.topologyLoading = true;
  view.topologyError = "";
  renderTopology();
  try {
    const response = await fetch("/api/topology");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    view.topology = normalizeTopology(await response.json());
    view.topologyRevision += 1;
  } catch (_error) {
    view.topology = null;
    view.topologyError = "拓扑读取失败，请检查本地配置与 Mocop 服务权限";
    view.topologyRevision += 1;
  } finally {
    view.topologyLoading = false;
    renderTopology();
  }
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
  const allowedKeys = new Set(["until", "reason", "recurring", "active"]);
  const windows = {};
  Object.entries(payload).forEach(([host, window]) => {
    const keys = window && typeof window === "object" && !Array.isArray(window)
      ? Object.keys(window) : null;
    if (
      !configured.has(host)
      || keys == null
      || !keys.includes("until")
      || !keys.includes("reason")
      || keys.some((key) => !allowedKeys.has(key))
      // recurring is only emitted for recurring windows; active always is.
      || (keys.includes("recurring") && typeof window.recurring !== "boolean")
      || typeof window.active !== "boolean"
      || typeof window.until !== "string"
      || !Number.isFinite(Date.parse(window.until))
      || typeof window.reason !== "string"
      || [...window.reason].length > 120
      || /\p{C}/u.test(window.reason)
    ) {
      throw new TypeError("Invalid maintenance windows response");
    }
    windows[host] = {
      until: window.until,
      reason: window.reason,
      recurring: window.recurring === true,
      active: window.active,
    };
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
    const response = await postJson("/api/settings/collector", settings);
    if (response.status === 400) {
      // The service enforces probeTimeoutSeconds > connect_timeout_seconds
      // and reports the read-only floor through the inventory payload.
      const connectTimeout = view.inventory.collectorSettings.connectTimeoutSeconds;
      setCollectorSettingsStatus(
        "error",
        `保存失败：单轮探测超时必须大于 SSH 连接超时（当前 ${
          format(connectTimeout, 1)
        } 秒），且数值需在允许范围内`,
      );
      return;
    }
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
  row.dataset.host = host;
  const identity = create("span", "inventory-host-name");
  identity.append(create("i", "status-dot online"), create("strong", "", host));
  if (action === "remove" && host === view.inventory.localHost) {
    identity.append(create("small", "", "本机"));
  }
  const group = view.inventory.hostGroups[host];
  if (group) identity.append(create("small", "host-group-badge", group));
  const maintenance = view.inventory.maintenanceWindows[host];
  // The "维护至" badge stays reserved for live windows; planned recurring
  // windows get their own badge so an off-period plan reads as schedule, not
  // as an active silence.
  if (maintenance?.active) {
    const badge = create("small", "maintenance-badge", `维护至 ${shortTime(maintenance.until)}`);
    badge.title = maintenance.recurring
      ? `${maintenance.reason}（每周重复）` : maintenance.reason;
    identity.append(badge);
  }
  if (maintenance?.recurring) {
    const plan = create(
      "small",
      `maintenance-plan-badge${maintenance.active ? "" : " inactive"}`,
      "每周维护计划",
    );
    plan.title = `下次窗口至 ${shortTime(maintenance.until)} · ${maintenance.reason}`;
    identity.append(plan);
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
        if (view.groupEditingHost) {
          view.maintenanceEditingHost = null;
          view.maintenanceDraft = null;
        }
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
        view.maintenanceDraft = null;
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
  // A pending or failed save re-renders the list; the draft keeps the
  // operator's unsaved input and focus target across those rebuilds.
  const draft = view.maintenanceDraft?.host === host ? view.maintenanceDraft : null;
  const form = create("form", "maintenance-editor");
  const reason = document.createElement("input");
  reason.type = "text";
  reason.required = true;
  reason.maxLength = 120;
  reason.placeholder = "维护原因，例如：驱动升级";
  reason.value = draft ? draft.reason : (maintenance?.reason || "");
  reason.setAttribute("aria-label", `${host} 维护原因`);
  const duration = document.createElement("select");
  duration.setAttribute("aria-label", `${host} 维护时长`);
  const selectedDuration = draft?.duration || "14400";
  [[3600, "1 小时"], [14400, "4 小时"], [86400, "24 小时"], [604800, "7 天"]]
    .forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = label;
      if (String(value) === selectedDuration) option.selected = true;
      duration.append(option);
    });
  const updateDraft = (focusField) => {
    view.maintenanceDraft = {
      host,
      reason: reason.value,
      duration: duration.value,
      focusField: focusField || view.maintenanceDraft?.focusField || null,
    };
  };
  reason.addEventListener("input", () => updateDraft("reason"));
  reason.addEventListener("focus", () => updateDraft("reason"));
  duration.addEventListener("change", () => updateDraft("duration"));
  duration.addEventListener("focus", () => updateDraft("duration"));
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
    const trimmedReason = reason.value.trim();
    if (!trimmedReason) {
      reason.setCustomValidity("请输入有效的维护原因");
      form.reportValidity();
      reason.setCustomValidity("");
      return;
    }
    if (!form.reportValidity()) return;
    updateDraft();
    changeMaintenance(host, Number(duration.value), trimmedReason);
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
  if (inventory.infrastructureHosts.length) message += `，识别 ${inventory.infrastructureHosts.length} 个跳板/代理节点`;
  if (inventory.sshDiscoveryWarnings.length) message += `，${inventory.sshDiscoveryWarnings.length} 条拓扑解析提示`;
  if (inventory.autoDiscover) message += "；自动发现已开启";
  if (!inventory.writable) message = "当前配置不可由网页修改，请先运行 mocop init 创建用户配置";
  elements.inventoryStatus.className = `inventory-status ${view.inventoryMessageKind}`.trim();
  elements.inventoryStatus.textContent = view.inventoryMessage || message;
  restoreMaintenanceEditorFocus();
  focusPendingMaintenanceHost();
}

function focusPendingMaintenanceHost() {
  // Deferred deep link (incident detail -> maintenance window): act only on
  // the render that follows a completed inventory load, so the row and its
  // expanded maintenance editor actually exist.
  const host = view.maintenanceFocusHost;
  if (!host || view.inventoryLoading) return;
  view.maintenanceFocusHost = null;
  const row = [...elements.configuredHostList.querySelectorAll(".inventory-host")]
    .find((item) => item.dataset.host === host);
  if (!row) return;
  row.scrollIntoView({ block: "center" });
  const target = row.querySelector('.maintenance-editor input[type="text"]')
    || row.querySelector(".maintenance-action");
  target?.focus();
}

function restoreMaintenanceEditorFocus() {
  const draft = view.maintenanceDraft;
  if (
    !draft
    || !draft.focusField
    || view.maintenanceEditingHost !== draft.host
  ) return;
  const active = document.activeElement;
  if (active && active !== document.body) return;
  const editor = elements.configuredHostList.querySelector(".maintenance-editor");
  const target = draft.focusField === "duration"
    ? editor?.querySelector("select")
    : editor?.querySelector('input[type="text"]');
  if (!target) return;
  target.focus();
  if (target instanceof HTMLInputElement) {
    target.setSelectionRange(target.value.length, target.value.length);
  }
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
    const response = await fetch("/api/inventory");
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
    const response = await postJson("/api/settings/host-group", { host, group });
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
    const response = await postJson("/api/settings/maintenance", { host, durationSeconds, reason });
    if (response.status === 409) throw new RangeError("stale inventory");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    view.inventory = normalizeInventory(await response.json());
    view.maintenanceEditingHost = null;
    view.maintenanceDraft = null;
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
    const response = await postJson("/api/settings/hosts", { action, host });
    if (response.status === 409) throw new RangeError("stale inventory");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    view.inventory = normalizeInventory(await response.json());
    view.inventoryMessage = action === "add"
      ? `${host} 已加入监控，正在等待首轮数据`
      : `${host} 已从监控配置移除`;
    view.inventoryMessageKind = "success";
    if (action === "remove" && view.selectedHost === host) selectHost("all");
    await Promise.all([fetchSnapshot(), fetchTopology()]);
  } catch (error) {
    if (error instanceof RangeError) {
      // The service refused a stale view of the inventory: rescan now so
      // the operator's retry runs against the current one.
      view.inventoryPendingHost = null;
      await refreshInventory();
      view.inventoryMessage = "节点清单已变化，已重新扫描，请再试一次";
    } else {
      view.inventoryMessage = "节点配置更新失败，请重新扫描并检查服务权限";
    }
    view.inventoryMessageKind = "error";
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

// Threshold policy lives on the server; the accepted envelope guarantees it.
function limits() {
  return view.snapshot.thresholds;
}

function serverStatus(server) {
  const states = {
    online: ["在线", "online"],
    unreachable: ["SSH 不可达", "issue"],
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
    "SSH transport stopped responding": "SSH 传输失去响应（keepalive 超时）",
    "SSH produced no output before the collection timeout": "SSH 在采集超时前无任何输出",
    "Remote collection stalled after partial output": "远端采集在部分输出后停滞",
    "Resource collection cancelled": "资源采集已取消",
    "Unexpected collector error": "采集器发生未预期错误",
    "nvidia-smi is unavailable": "系统在线，但未安装 nvidia-smi",
    "nvidia-smi query failed": "系统在线，但 GPU 查询失败",
  };
  if (messages[message]) return messages[message];
  // Messages carrying a dynamic exit code only match by prefix; the original
  // parenthesised detail is preserved verbatim.
  const prefixes = [
    ["Remote resource query failed", "远端资源查询失败"],
    ["Local resource query failed", "本机资源查询失败"],
  ];
  if (typeof message === "string") {
    const prefixed = prefixes.find(([prefix]) => message.startsWith(prefix));
    if (prefixed) return prefixed[1] + message.slice(prefixed[0].length);
  }
  return message || "采集失败";
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
  view.historyError = false;
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

// Validate the incidents envelope once and index active conditions by host,
// so per-server and per-GPU renderers never rescan the whole fleet list.
function acceptIncidents(incidents) {
  if (
    !incidents
    || typeof incidents !== "object"
    || !Number.isSafeInteger(incidents.version)
    || !Array.isArray(incidents.active)
    || !Array.isArray(incidents.events)
    || !Array.isArray(incidents.correlations)
  ) {
    throw new TypeError("Invalid incidents envelope");
  }
  const byHost = new Map();
  incidents.active.forEach((condition) => {
    const conditions = byHost.get(condition.host);
    if (conditions) conditions.push(condition);
    else byHost.set(condition.host, [condition]);
  });
  view.incidents = incidents;
  view.incidentsByHost = byHost;
  view.incidentVersion = incidents.version;
}

function hostConditions(host) {
  return view.incidentsByHost.get(host) || [];
}

function serverConditions(server) {
  return hostConditions(server.host)
    .filter((condition) => condition.actionable !== false)
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
      source: condition,
    }));
}

function incidentsSyncedWithSnapshot() {
  return view.incidents != null
    && view.snapshot != null
    && view.incidentVersion === numeric(view.snapshot.incidentVersion, 0);
}

function capacityMatches(request) {
  // The raw active list keeps silenced alerts in scope: an operator
  // acknowledging noise must not turn a faulty GPU back into a candidate.
  return capacityMatch.matches({
    servers: view.snapshot.servers,
    activeConditions: view.incidents?.active,
    request,
    busyPct: limits().gpu_busy_pct,
    temperatureC: limits().gpu_temperature_warning_c,
  });
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
  card.dataset.candidateKey = `${candidate.host}\u0000${candidate.model}`;
  const heading = create("div", "capacity-candidate-heading");
  const identity = create("span", "capacity-candidate-identity");
  const hostName = create("strong", "", displayHost(candidate.host));
  hostName.title = candidate.host;
  const modelName = create("small", "", candidate.model);
  modelName.title = candidate.model;
  identity.append(hostName, modelName);
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
  const actions = create("div", "capacity-candidate-actions");
  const locate = create("button", "inline-action", "查看节点");
  locate.type = "button";
  locate.addEventListener("click", () => {
    elements.capacityDialog.close();
    selectHost(candidate.host);
  });
  actions.append(locate, sshCopyButton(candidate.host));
  card.append(heading, metrics, devices, actions);
  return card;
}

// The copied command uses the configured OpenSSH alias, which is exactly what
// the operator's own ssh client resolves; display names stay presentation-only.
async function copySshCommand(button, host) {
  if (button.dataset.resetTimer) clearTimeout(Number(button.dataset.resetTimer));
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(`ssh ${host}`);
    button.textContent = "已复制";
  } catch (_error) {
    button.textContent = "复制失败";
  }
  button.dataset.resetTimer = String(setTimeout(() => {
    button.textContent = "复制 SSH";
    delete button.dataset.resetTimer;
  }, 2000));
}

function sshCopyButton(host) {
  const button = create("button", "inline-action", "复制 SSH");
  button.type = "button";
  button.title = `复制 ssh ${host}`;
  button.addEventListener("click", () => copySshCommand(button, host));
  return button;
}

function renderOwners() {
  if (!elements.ownersDialog.open) return;
  if (!view.snapshot) {
    elements.ownersSummary.textContent = "等待 GPU 快照";
    return;
  }
  elements.ownersUpdated.textContent = age(view.snapshot.lastPollCompletedAt);
  const owners = new Map();
  const hostLabels = new Map();
  let excludedOfflineHosts = 0;
  let oldestObservedAt = "";
  for (const server of view.snapshot.servers) {
    hostLabels.set(server.host, server.displayName || server.host);
    if (server.status !== "online") {
      // Last-success processes on unreachable nodes may already be gone;
      // counting them as "current" owners would misattribute the fleet.
      if (server.gpus.some((gpu) => (gpu.processes || []).length > 0)) {
        excludedOfflineHosts += 1;
      }
      continue;
    }
    for (const gpu of server.gpus) {
      const processes = gpu.processes || [];
      if (
        processes.length
        && gpu.processes_observed_at
        && Number.isFinite(Date.parse(gpu.processes_observed_at))
        && (
          !oldestObservedAt
          || Date.parse(gpu.processes_observed_at) < Date.parse(oldestObservedAt)
        )
      ) {
        oldestObservedAt = gpu.processes_observed_at;
      }
      for (const process of processes) {
        const owner = process.workload?.owner;
        const key = owner || "\u0000unattributed";
        let entry = owners.get(key);
        if (!entry) {
          entry = {
            label: owner || "未归属",
            attributed: Boolean(owner),
            vramMiB: 0,
            unknownVramKeys: new Set(),
            processKeys: new Set(),
            gpus: new Set(),
            hosts: new Set(),
            kinds: new Set(),
          };
          owners.set(key, entry);
        }
        // VRAM stays a per-card sum; the process count dedupes one PID
        // spanning several GPUs of the same host.
        const processKey = `${server.host}\u0000${process.pid}`;
        const usedMemory = process.used_memory_mib == null
          ? NaN : numeric(process.used_memory_mib, NaN);
        if (Number.isFinite(usedMemory)) entry.vramMiB += usedMemory;
        else entry.unknownVramKeys.add(processKey);
        entry.processKeys.add(processKey);
        entry.gpus.add(`${server.host}\u0000${gpu.uuid || gpu.index}`);
        entry.hosts.add(server.host);
        const kind = process.workload?.kind;
        if (kind && kind !== "process") entry.kinds.add(kind);
      }
    }
  }
  const ranked = [...owners.values()].sort(
    (first, second) => second.vramMiB - first.vramMiB
      || second.gpus.size - first.gpus.size
      || first.label.localeCompare(second.label),
  );
  const offlineNote = excludedOfflineHosts
    ? create(
      "div",
      "owners-footnote",
      `${excludedOfflineHosts} 台离线节点未计入`,
    )
    : null;
  if (offlineNote) {
    offlineNote.title = "离线节点仅保留最后一次成功采集的进程，不代表当前占用";
  }
  elements.ownersResults.replaceChildren();
  if (!ranked.length) {
    elements.ownersSummary.textContent = "当前快照没有 GPU 进程";
    if (offlineNote) elements.ownersResults.append(offlineNote);
    return;
  }
  const visible = ranked.slice(0, 50);
  const totalVram = ranked.reduce((sum, entry) => sum + entry.vramMiB, 0);
  const hasUnknownVram = ranked.some((entry) => entry.unknownVramKeys.size > 0);
  const attributedCount = ranked.filter((entry) => entry.attributed).length;
  elements.ownersSummary.textContent =
    `${ranked.length} 个归属方 · 共占用${hasUnknownVram ? "至少 " : " "}${memory(totalVram)}`
    + (attributedCount < ranked.length ? " · 含未归属进程" : "")
    + (hasUnknownVram ? " · 部分进程显存未知" : "")
    + (ranked.length > visible.length ? ` · 仅展示前 ${visible.length} 项` : "")
    + (oldestObservedAt ? ` · 数据截至 ${age(oldestObservedAt)}` : "");
  for (const entry of visible) {
    const card = create(
      "article",
      `capacity-candidate ${entry.attributed ? "match" : "near"}`,
    );
    const heading = create("div", "capacity-candidate-heading");
    const identity = create("span", "capacity-candidate-identity");
    const ownerName = create("strong", "", entry.label);
    ownerName.title = entry.label;
    const kindText = entry.kinds.size ? [...entry.kinds].sort().join(" · ") : "进程";
    const kindLabel = create("small", "", kindText);
    kindLabel.title = kindText;
    if (!entry.attributed) {
      card.title = "启用 workloads.mode=identity 可显示属主与完整命令行";
    }
    identity.append(ownerName, kindLabel);
    const vramLabel = create(
      "em",
      "",
      `${entry.unknownVramKeys.size ? "至少 " : ""}${memory(entry.vramMiB)}`,
    );
    if (entry.unknownVramKeys.size) {
      vramLabel.title = `${entry.unknownVramKeys.size} 个进程未返回显存占用`;
    }
    heading.append(identity, vramLabel);
    const metrics = create("div", "capacity-candidate-metrics");
    metrics.append(
      create("span", "", `${entry.gpus.size} 张 GPU`),
      create("span", "", `${entry.hosts.size} 个节点`),
      create("span", "", `${entry.processKeys.size} 个进程`),
    );
    const devices = create("div", "capacity-devices");
    [...entry.hosts].sort().slice(0, 8).forEach((host) => {
      // Drill-down: the chip jumps to the host in the fleet view, reusing the
      // regular selection path (URL hash included) via selectHost().
      const chip = create("button", "owner-host-chip", hostLabels.get(host) || host);
      chip.type = "button";
      chip.title = `${host} · 点击查看该节点`;
      chip.addEventListener("click", () => {
        elements.ownersDialog.close();
        selectHost(host);
      });
      devices.append(chip);
    });
    if (entry.hosts.size > 8) {
      devices.append(create("span", "", `+${entry.hosts.size - 8}`));
    }
    card.append(heading, metrics, devices);
    elements.ownersResults.append(card);
  }
  if (offlineNote) elements.ownersResults.append(offlineNote);
}

function gpuHoursLabel(seconds) {
  const value = numeric(seconds);
  if (value < 60) return `${Math.round(value)} 卡·秒`;
  if (value < 5400) return `${Math.round(value / 60)} 卡·分`;
  return `${format(value / 3600, 1)} 卡·时`;
}

async function fetchOwnersUsage() {
  const request = ++view.ownersUsageRequest;
  view.ownersUsageLoading = true;
  view.ownersUsageError = "";
  renderOwnersUsage();
  try {
    const response = await fetch(
      `/api/usage?hours=${encodeURIComponent(view.ownersUsageHours)}&limit=50`,
    );
    const usage = await response.json();
    if (!response.ok) throw new Error(usage.error || "占用账单不可用");
    if (request !== view.ownersUsageRequest) return;
    view.ownersUsage = usage;
  } catch (_error) {
    if (request !== view.ownersUsageRequest) return;
    view.ownersUsage = null;
    view.ownersUsageError = "占用账单加载失败，可重新打开本面板重试";
  } finally {
    if (request === view.ownersUsageRequest) {
      view.ownersUsageLoading = false;
      renderOwnersUsage();
    }
  }
}

function renderOwnersUsage() {
  if (!elements.ownersDialog.open) return;
  elements.ownersUsageResults.replaceChildren();
  if (view.ownersUsageLoading) {
    elements.ownersUsageSummary.textContent = "正在统计占用账单…";
    return;
  }
  if (view.ownersUsageError) {
    elements.ownersUsageSummary.textContent = view.ownersUsageError;
    return;
  }
  const usage = view.ownersUsage;
  if (!usage) {
    elements.ownersUsageSummary.textContent = "等待占用账单";
    return;
  }
  const owners = Array.isArray(usage.owners) ? usage.owners : [];
  if (!owners.length) {
    elements.ownersUsageSummary.textContent = "窗口内没有观测到 GPU 进程";
    return;
  }
  const coverageGap = usage.earliestDataAt && usage.sinceAt
    && Date.parse(usage.earliestDataAt) > Date.parse(usage.sinceAt) + 60_000;
  elements.ownersUsageSummary.textContent =
    `${numeric(usage.totalOwners)} 个归属方 · 共 ${gpuHoursLabel(usage.totalGpuSeconds)}`
    + (coverageGap ? ` · 数据自 ${age(usage.earliestDataAt)}起` : "");
  for (const entry of owners.slice(0, 50)) {
    const card = create(
      "article",
      `capacity-candidate ${entry.owner ? "match" : "near"}`,
    );
    const heading = create("div", "capacity-candidate-heading");
    const identity = create("span", "capacity-candidate-identity");
    const ownerName = create("strong", "", entry.owner || "未归属");
    ownerName.title = entry.owner || "启用 workloads.mode=identity 可区分用户";
    const kinds = entry.kinds && typeof entry.kinds === "object"
      ? Object.keys(entry.kinds)
        .filter((kind) => kind !== "process")
        .map((kind) => WORKLOAD_KIND_LABELS[kind] || kind)
      : [];
    identity.append(ownerName, create("small", "", kinds.length ? kinds.join(" · ") : "进程"));
    heading.append(identity, create("em", "", gpuHoursLabel(entry.gpuSeconds)));
    const metrics = create("div", "capacity-candidate-metrics");
    const idleShare = entry.idleShare == null ? NaN : numeric(entry.idleShare, NaN);
    metrics.append(
      create("span", "", `${numeric(entry.gpus)} 张 GPU`),
      create(
        "span",
        "",
        Array.isArray(entry.hosts) ? `${entry.hosts.length} 个节点` : "节点未知",
      ),
      create("span", "", `${numeric(entry.processes)} 段占用`),
      create(
        "span",
        Number.isFinite(idleShare) && idleShare >= 0.5 ? "owner-idle-high" : "",
        Number.isFinite(idleShare)
          ? `闲置占比 ${format(idleShare * 100)}%`
          : "闲置占比未知",
      ),
    );
    card.append(heading, metrics);
    elements.ownersUsageResults.append(card);
  }
}

function renderCapacityMatcher() {
  if (!elements.capacityDialog.open || !view.snapshot) return;
  syncCapacityModels();
  const request = view.capacityRequest;
  elements.capacityUpdated.textContent = age(view.snapshot.lastPollCompletedAt);
  if (!incidentsSyncedWithSnapshot()) {
    // Without current alert data a "no hardware alerts" promise would be a
    // guess, so hold the verdict until the matching incident version loads.
    elements.capacityRule.textContent = `空闲 = GPU 负载低于 ${format(limits().gpu_busy_pct)}% · 单卡可用显存至少 ${format(request.minVramGiB)} GiB · 温度低于 ${format(limits().gpu_temperature_warning_c)}°C 警戒线 · GPU 硬件告警状态同步中`;
    elements.capacitySummary.textContent = "GPU 告警数据加载中或暂不可用，暂缓给出匹配结论";
    elements.capacityResults.replaceChildren(
      create("div", "capacity-empty", "等待 GPU 告警数据同步后自动更新匹配结果"),
    );
    return;
  }
  const result = capacityMatches(view.capacityRequest);
  const exact = result.candidates.filter((candidate) => candidate.satisfies).length;
  elements.capacityRule.textContent = `空闲 = GPU 负载低于 ${format(limits().gpu_busy_pct)}% · 单卡可用显存至少 ${format(request.minVramGiB)} GiB · 温度低于 ${format(limits().gpu_temperature_warning_c)}°C 警戒线 · 无 GPU 硬件告警`;
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
  const activeElement = document.activeElement;
  const focusedKey = elements.capacityResults.contains(activeElement)
    ? activeElement.closest(".capacity-candidate")?.dataset.candidateKey || null
    : null;
  elements.capacityResults.replaceChildren(...visible.map(capacityCandidateCard));
  if (focusedKey) {
    const restored = [...elements.capacityResults.querySelectorAll(".capacity-candidate")]
      .find((card) => card.dataset.candidateKey === focusedKey);
    restored?.querySelector("button.inline-action")?.focus();
  }
}

// One watch, settled synchronously at every data-acceptance site rather than
// inside the requestAnimationFrame render: browsers pause rAF for hidden
// documents, and a background tab is exactly where the notification and the
// title marker must still fire. The leaf owns the armed/notified edge and its
// cooldown; this layer only projects the result into banner, title, and
// notification.
function settleCapacityWatch() {
  evaluateCapacityWatch();
  renderCapacityWatchBanner();
}

function evaluateCapacityWatch() {
  const watch = view.capacityWatch;
  if (!watch || !view.snapshot || !incidentsSyncedWithSnapshot()) return;
  const result = capacityMatches(watch.request);
  const satisfied = result.candidates.filter((candidate) => candidate.satisfies).length;
  view.capacityWatchSatisfied = satisfied;
  const evaluated = capacityWatch.evaluateWatch(watch, satisfied);
  if (!evaluated.watch) {
    // Another tab stopped or replaced this watch; adopt that, never revive.
    view.capacityWatch = null;
    view.capacityWatchSatisfied = 0;
    view.capacityWatchBannerDismissed = false;
    return;
  }
  if (evaluated.watch.state === "armed" && watch.state === "notified") {
    view.capacityWatchBannerDismissed = false;
  }
  view.capacityWatch = evaluated.watch;
  if (evaluated.shouldNotify) {
    view.capacityWatchBannerDismissed = false;
    deliverCapacityWatchNotification(satisfied);
  }
}

function deliverCapacityWatchNotification(satisfied) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    const notification = new Notification("Mocop · GPU 已就绪", {
      body: capacityWatch.bannerText(view.capacityWatch, satisfied),
      tag: "mocop-capacity-watch",
    });
    notification.addEventListener("click", () => {
      window.focus();
      notification.close();
    });
  } catch (_error) {
    // The in-page banner still reports readiness without system notifications.
  }
}

function renderCapacityWatchControls() {
  const watch = view.capacityWatch;
  if (!watch) {
    elements.capacityWatchToggle.textContent = "空闲时提醒我";
    delete elements.capacityWatchToggle.dataset.watching;
    elements.capacityWatchStatus.className = "capacity-watch-status";
    elements.capacityWatchStatus.textContent =
      "保存当前条件后，页面会在出现满足的空闲组合时提醒（可选浏览器通知）";
    return;
  }
  elements.capacityWatchToggle.textContent = "停止守望";
  elements.capacityWatchToggle.dataset.watching = "true";
  const ready = watch.state === "notified";
  elements.capacityWatchStatus.className =
    `capacity-watch-status ${ready ? "ready" : "armed"}`;
  elements.capacityWatchStatus.textContent = capacityWatch.controlText(
    watch,
    view.capacityWatchSatisfied,
  );
}

function renderCapacityWatchBanner() {
  const banner = elements.capacityWatchBanner;
  const watch = view.capacityWatch;
  const show = Boolean(
    watch && watch.state === "notified" && !view.capacityWatchBannerDismissed,
  );
  banner.hidden = !show;
  const title = show ? `● ${view.baseDocumentTitle}` : view.baseDocumentTitle;
  if (document.title !== title) document.title = title;
  if (show) {
    elements.capacityWatchBannerText.textContent = capacityWatch.bannerText(
      watch,
      view.capacityWatchSatisfied,
    );
  }
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
    conditions,
  };
}

function serverIssue(server) {
  return issueFromConditions(server, serverConditions(server));
}

function attentionIssues() {
  const conditionsByHost = new Map(
    view.snapshot.servers.map((server) => [server.host, serverConditions(server)]),
  );
  const consumed = new Set();
  const issues = [];
  const correlations = view.incidents?.correlations || [];
  correlations.forEach((correlation) => {
    if (
      correlation?.kind !== "configured_shared_path"
      || correlation.confidence !== "possible"
    ) return;
    const anchor = safeStoredHosts([correlation.anchor])[0];
    const hosts = safeStoredHosts(correlation.hosts).filter((host) =>
      conditionsByHost.get(host)?.some((condition) => condition.kind === "connectivity"));
    if (!anchor || hosts.length < 2) return;
    hosts.forEach((host) => {
      conditionsByHost.get(host)
        .filter((condition) => condition.kind === "connectivity")
        .forEach((condition) => consumed.add(`${host}|${condition.id}`));
    });
    issues.push({
      shared: true,
      sharedLabel: "可能的共享链路",
      hosts,
      severity: "critical",
      priority: 3,
      messages: [`${hosts.length} 台节点不可达 · 配置路径经过 ${anchor}`],
      categories: ["connection"],
      sortName: anchor,
    });
  });
  const sharedGroups = new Map();
  conditionsByHost.forEach((conditions, host) => {
    conditions.filter((condition) => condition.sharedKey).forEach((condition) => {
      const group = sharedGroups.get(condition.sharedKey) || [];
      group.push({ host, condition });
      sharedGroups.set(condition.sharedKey, group);
    });
  });

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
      sharedLabel: "共享存储",
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
  const syncFailed = view.incidentSyncFailed;
  if (!issues.length && !syncFailed) {
    view.attentionRenderKey = "empty";
    elements.attentionPanel.hidden = true;
    return;
  }
  const visibleIssues = view.attentionFilter === "all"
    ? issues
    : issues.filter((issue) => issue.categories.includes(view.attentionFilter));
  const renderKey = JSON.stringify({
    filter: view.attentionFilter,
    syncFailed,
    issues: issues.map((issue) => ({
      shared: Boolean(issue.shared),
      sharedLabel: issue.sharedLabel || null,
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
    const selected = button.dataset.attentionFilter === view.attentionFilter;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
    button.disabled = count === 0;
    button.querySelector("span").textContent = count;
  });
  const critical = visibleIssues.filter((issue) => issue.severity === "critical").length;
  const affectedHosts = new Set(visibleIssues.flatMap((issue) => issue.hosts));
  elements.attentionSummary.textContent = syncFailed && !issues.length
    ? "告警数据暂不可用"
    : view.attentionFilter === "all"
      ? `${affectedHosts.size} 台服务器 · ${issues.length} 个问题`
      : `${affectedHosts.size} 台服务器 · ${visibleIssues.length}/${issues.length} 个问题`;
  const fragment = document.createDocumentFragment();
  visibleIssues.forEach((issue) => {
    const item = create(issue.shared ? "article" : "button", `attention-item ${issue.severity}${issue.shared ? " shared" : ""}`);
    if (!issue.shared) item.type = "button";
    const issueLabel = issue.shared ? issue.sharedLabel : displayHost(issue.server);
    item.title = `${issueLabel}：${issue.messages.join(" · ")}`;
    const heading = create("span", "attention-item-heading");
    heading.append(
      create("i"),
      create("strong", "", issueLabel),
      create("em", "", issue.severity === "critical" ? "严重" : "警告"),
    );
    item.append(heading, create("span", "attention-message", issue.messages.join(" · ")));
    if (issue.shared) {
      const hosts = create("span", "attention-hosts");
      issue.hosts.forEach((host) => {
        const button = create("button", "attention-host", displayHost(host));
        button.type = "button";
        button.addEventListener("click", () => selectHost(host));
        hosts.append(button);
      });
      item.append(hosts);
    } else {
      item.addEventListener("click", () => {
        const condition = issue.conditions
          ?.slice()
          .sort((first, second) => second.priority - first.priority)[0]
          ?.source;
        if (condition) openIncidentDetail(condition);
        else selectHost(issue.server.host);
      });
    }
    fragment.append(item);
  });
  if (syncFailed) {
    // A failed /api/incidents sync must not silently freeze or empty this
    // panel; the existing backoff keeps retrying in the background.
    fragment.append(create(
      "div",
      "attention-sync-error",
      "告警详情加载失败，正在重试",
    ));
  } else if (!visibleIssues.length) {
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

function selectedIncidentRecord() {
  if (!view.selectedIncident) return null;
  return hostConditions(view.selectedIncident.host).find((condition) =>
    condition.conditionKey === view.selectedIncident.conditionKey) || null;
}

function diagnosticEvidenceLabel(label) {
  return {
    current: "当前值",
    threshold: "告警阈值",
    consecutiveFailures: "连续失败",
    lastSuccessAt: "最近成功",
    gpuUtilizationPct: "GPU 负载",
    memoryUsedMiB: "进程显存",
    processCount: "活跃进程",
  }[label] || label;
}

function diagnosticEvidenceValue(item) {
  if (item.value == null) return "—";
  if (item.label === "lastSuccessAt") return age(item.value);
  if (typeof item.value === "number") {
    return `${format(item.value, 1)}${item.unit || ""}`;
  }
  return String(item.value);
}

function localizedDiagnosis(condition) {
  const resource = condition.resource || "资源";
  const descriptions = {
    connectivity: [
      "采集链路不可用",
      "固定 SSH 探针未能完成，当前资源数据不可用。",
      ["使用同一 OpenSSH 别名验证非交互连接。", "若多台节点同时失败，优先检查共享跳板机或隧道。"],
    ],
    disk: [
      "文件系统空间不足",
      `${resource} 已超过配置的使用率阈值。`,
      ["确认该文件系统是否仍在按预期增长。", "检查大目录以及日志、缓存和检查点保留策略。"],
    ],
    swap: [
      "Swap 压力偏高",
      "Swap 使用率超过阈值，可能存在持续内存压力。",
      ["对照可用内存和当前任务规模。", "观察 Swap 是否持续增长或已趋于稳定。"],
    ],
    memory: [
      "内存压力偏高",
      "内存使用率超过配置阈值。",
      ["检查当前节点上的主要任务。", "观察采样之间的可用内存是否恢复。"],
    ],
    cpu: [
      "CPU 负载偏高",
      "CPU 使用率超过配置阈值。",
      ["确认数据加载或预处理是否限制 GPU。", "对照同一时段的 CPU 与 GPU 利用率。"],
    ],
    gpu_idle_memory: [
      "显存占用但计算空闲",
      "显存仍被进程占用，但 GPU 计算负载持续低于忙碌阈值。",
      ["查看该 GPU 的进程与任务归属。", "确认进程是在正常等待，还是已经停滞。"],
    ],
    gpu_temperature: [
      "GPU 温度偏高",
      "GPU 温度超过配置的警告阈值。",
      ["检查散热、风扇和相邻设备温度。", "继续长任务前确认硬件降频状态。"],
    ],
    gpu_count: [
      "GPU 数量与配置不一致",
      "当前可见 GPU 数量与 expected_gpu_counts 不一致。",
      ["检查设备可见性和驱动初始化。", "仅在硬件确实调整后修改预期数量。"],
    ],
    gpu_ecc: ["GPU 硬件健康异常", "检测到未纠正 ECC 错误。", ["保留任务上下文并按集群硬件维护流程处理。"]],
    gpu_memory_repair: ["GPU 显存需要修复", "硬件遥测报告待处理的显存修复状态。", ["保留任务上下文并按集群硬件维护流程处理。"]],
    gpu_slowdown: ["GPU 已触发硬件降频", "温度或功率相关硬件状态触发了降频。", ["检查温度、功耗上限和散热条件。"]],
  };
  return descriptions[condition.category] || [
    condition.diagnosis?.title || "资源状态需要处理",
    condition.diagnosis?.summary || incidentConditionMessage(condition),
    condition.diagnosis?.nextSteps || ["确认当前状态是否符合任务预期。"],
  ];
}

function renderIncidentDetail() {
  if (!elements.incidentDetailDialog.open) return;
  const condition = selectedIncidentRecord();
  if (!condition) {
    elements.incidentDetailDialog.close();
    view.selectedIncident = null;
    return;
  }
  const diagnosis = condition.diagnosis || {};
  const [diagnosisTitle, diagnosisSummary, diagnosisSteps] = localizedDiagnosis(condition);
  const actionLabel = condition.action === "silenced"
    ? "已静默" : condition.action === "acknowledged" ? "已确认" : "待处理";
  elements.incidentDetailHost.textContent = `${condition.host} · ${condition.resource || "资源"}`;
  elements.incidentDetailTitle.textContent = diagnosisTitle;
  elements.incidentDetailStatus.className = `incident-detail-status ${condition.severity}`;
  elements.incidentDetailStatus.textContent = [
    condition.severity === "critical" ? "严重" : "警告",
    actionLabel,
    `首次 ${age(condition.firstObservedAt || condition.observedAt)}`,
    condition.actionUntil ? `有效至 ${new Date(condition.actionUntil).toLocaleString("zh-CN")}` : null,
  ].filter(Boolean).join(" · ");
  elements.incidentDetailSummary.textContent = diagnosisSummary;
  const evidence = Array.isArray(diagnosis.evidence) ? diagnosis.evidence : [];
  elements.incidentEvidence.replaceChildren(...evidence.map((item) => {
    const node = create("article", "incident-evidence-item");
    node.append(
      create("span", "", diagnosticEvidenceLabel(item.label)),
      create("strong", "", diagnosticEvidenceValue(item)),
    );
    return node;
  }));
  const steps = Array.isArray(diagnosisSteps) ? diagnosisSteps : [];
  elements.incidentNextSteps.replaceChildren(...steps.map((step) => create("li", "", step)));
  const targetIndex = diagnosis.targetGpuIndex;
  elements.incidentOpenGpu.hidden = !Number.isInteger(targetIndex);
  elements.incidentOpenGpu.dataset.gpuIndex = Number.isInteger(targetIndex) ? targetIndex : "";
  elements.incidentActionReason.value = condition.actionReason || "";
  elements.clearIncidentAction.hidden = !condition.action;
  [
    elements.acknowledgeIncident,
    elements.silenceIncident,
    elements.clearIncidentAction,
    elements.incidentOpenMaintenance,
    elements.incidentActionDuration,
    elements.incidentActionReason,
  ].forEach((element) => { element.disabled = view.incidentActionPending; });
}

function openIncidentDetail(condition) {
  view.selectedIncident = {
    host: condition.host,
    conditionKey: condition.conditionKey,
  };
  elements.incidentActionFeedback.textContent = "";
  elements.incidentActionFeedback.className = "incident-action-feedback";
  openExclusiveDialog(elements.incidentDetailDialog);
  renderIncidentDetail();
}

async function updateIncidentAction(action) {
  const condition = selectedIncidentRecord();
  if (!condition || view.incidentActionPending) return;
  view.incidentActionPending = true;
  renderIncidentDetail();
  elements.incidentActionFeedback.textContent = "正在保存处理状态…";
  elements.incidentActionFeedback.className = "incident-action-feedback";
  const duration = action === "clear"
    ? 0 : Number.parseInt(elements.incidentActionDuration.value, 10);
  try {
    const response = await postJson("/api/settings/incident-action", {
      host: condition.host,
      conditionKey: condition.conditionKey,
      incidentStartedAt: action === "clear"
        ? null : (condition.firstObservedAt || condition.observedAt),
      action,
      durationSeconds: duration,
      reason: action === "clear" ? "" : elements.incidentActionReason.value,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存失败");
    view.incidentVersion = -1;
    view.incidentLoadingVersion = null;
    await syncIncidents();
    elements.incidentActionFeedback.textContent = action === "clear"
      ? "已恢复为待处理" : action === "silenced" ? "已保存静默" : "已确认问题";
    elements.incidentActionFeedback.className = "incident-action-feedback success";
  } catch (error) {
    elements.incidentActionFeedback.textContent = error.message || "处理状态保存失败";
    elements.incidentActionFeedback.className = "incident-action-feedback error";
  } finally {
    view.incidentActionPending = false;
    renderIncidentDetail();
  }
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
    : `${displayHost(view.selectedHost)} · ${events.length} 条近期变化`;
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
    item.title = `${displayHost(event.host)}：${incidentDescription(event)}`;
    const body = create("span", "incident-body");
    const title = create("span", "incident-title");
    title.append(
      create("strong", "", displayHost(event.host)),
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
    item.addEventListener("click", () => {
      const active = hostConditions(event.host).find((condition) =>
        condition.conditionKey === event.conditionKey);
      if (active) openIncidentDetail(active);
      else selectHost(event.host);
    });
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

function moveServerByKeyboard(host, direction) {
  if (!view.snapshot || ![-1, 1].includes(direction)) return;
  const order = syncServerOrder(view.snapshot.servers);
  const currentIndex = order.indexOf(host);
  const nextIndex = currentIndex + direction;
  if (currentIndex < 0 || nextIndex < 0 || nextIndex >= order.length) return;
  [order[currentIndex], order[nextIndex]] = [order[nextIndex], order[currentIndex]];
  view.serverOrder = order;
  view.serverSort = "custom";
  elements.serverSort.value = "custom";
  savePreferences();
  renderServers();
  elements.serverOrderStatus.textContent = `${host} 已${direction < 0 ? "上移" : "下移"}至第 ${nextIndex + 1} 位`;
  requestAnimationFrame(() => {
    elements.serverList.querySelector(`[data-host="${CSS.escape(host)}"]`)?.focus();
  });
}

function enableServerDrag(item, host) {
  item.draggable = true;
  item.dataset.host = host;
  item.setAttribute("aria-keyshortcuts", "Alt+ArrowUp Alt+ArrowDown");
  item.addEventListener("keydown", (event) => {
    if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    moveServerByKeyboard(host, event.key === "ArrowUp" ? -1 : 1);
  });
  item.addEventListener("dragstart", (event) => {
    view.draggedHost = host;
    item.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", host);
  });
  item.addEventListener("dragover", (event) => {
    if (!view.draggedHost || view.draggedHost === host) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    item.classList.add("drag-target");
  });
  item.addEventListener("dragleave", () => item.classList.remove("drag-target"));
  item.addEventListener("drop", (event) => {
    event.preventDefault();
    item.classList.remove("drag-target");
    view.suppressServerClick = true;
    reorderServer(
      view.draggedHost || event.dataTransfer.getData("text/plain"),
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
  const actionableIssues = numeric(snapshot.stats.actionableIssueServers);
  const actionableCritical = numeric(snapshot.stats.actionableCriticalIncidents);
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
  const zeroNodes = numeric(snapshot.stats.servers) === 0;
  elements.serverRatio.textContent = `${snapshot.stats.onlineServers} / ${snapshot.stats.servers}`;
  elements.serverHealth.textContent = zeroNodes
    ? "未配置"
    : actionableCritical
      ? "严重" : actionableIssues ? "需关注" : maintenanceServers ? "维护中" : "健康";
  elements.serverHealth.classList.toggle(
    "warning",
    actionableIssues > 0 && !serverCritical,
  );
  elements.serverHealth.classList.toggle("critical", serverCritical);
  elements.serverCard.classList.toggle("is-warning", actionableIssues > 0 && !serverCritical);
  elements.serverCard.classList.toggle("is-critical", serverCritical);
  elements.serverBar.style.width = `${clamp(onlineRatio)}%`;
  if (zeroNodes) {
    // "健康 / 所有服务器运行正常" would be misleading with nothing monitored;
    // point straight at the settings inventory scan instead. The node is kept
    // across renders so the guide button survives the per-second refresh.
    if (!elements.serverDetail.querySelector(".empty-fleet-action")) {
      const guide = create("button", "inline-action empty-fleet-action", "添加监控节点");
      guide.type = "button";
      guide.title = "打开设置并扫描 OpenSSH 配置中的候选节点";
      guide.addEventListener("click", () => elements.settingsToggle.click());
      elements.serverDetail.replaceChildren("尚未配置监控节点", guide);
    }
  } else {
    elements.serverDetail.textContent = actionableIssues
      ? `${actionableIssues} 台需关注 · ${numeric(snapshot.stats.actionableIncidents)} 个待处理问题`
      : maintenanceServers && snapshot.stats.activeIncidents
        ? `${maintenanceServers} 台维护中 · ${snapshot.stats.activeIncidents} 个活动问题已静默`
        : maintenanceServers
          ? `${maintenanceServers} 台处于计划维护窗口`
          : "所有服务器运行正常";
  }
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
    ? "等待首批完成"
    : `最近批次 ${format(numeric(cycleMilliseconds) / 1000, 1)} 秒`;
  const cycleSlow = cycleMilliseconds != null
    && numeric(cycleMilliseconds) > numeric(snapshot.pollIntervalSeconds) * 1000;
  const collectionDelayed = collectionHealth().state === "delayed";
  elements.pollInfo.textContent = `每 ${format(snapshot.pollIntervalSeconds)} 秒 · ${cycleText}${collectionDelayed ? " · 采集延迟" : ""} · v${snapshot.appVersion}`;
  elements.pollInfo.classList.toggle("warning", cycleSlow || collectionDelayed);
  elements.pollInfo.title = collectionDelayed
    ? "最近采集批次完成时间已超过配置的新鲜度窗口"
    : cycleSlow ? "最近采集批次耗时超过目标轮询间隔" : "";
  elements.collectorError.hidden = !snapshot.collectorError;
  elements.collectorError.textContent = snapshot.collectorError || "";
  renderRuntimeStatus(snapshot);
  syncRefreshControl();
  renderConnectionStatus();
}

function setRuntimeStatus(element, text, kind = "") {
  element.textContent = text;
  element.className = kind;
}

function renderServiceRestartStatus(message = "", kind = "") {
  let text = message;
  if (!text && view.serviceRestartLoading) text = "正在确认运行方式";
  if (!text && view.serviceRestarting) text = "正在等待新进程恢复";
  if (!text && view.serviceRestartSupported) text = "受 systemd 管理 · 可安全重启";
  if (!text) text = "当前启动方式不支持网页重启";
  elements.serviceRestartStatus.textContent = text;
  elements.serviceRestartStatus.className = kind;
  elements.restartService.disabled = (
    !view.serviceRestartSupported
    || view.serviceRestartLoading
    || view.serviceRestarting
  );
}

async function fetchRestartCapability() {
  const response = await fetch("/api/meta");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const supported = (await response.json()).capabilities?.restartSupported;
  if (typeof supported !== "boolean") throw new TypeError("Invalid service capability");
  return supported;
}

async function fetchServiceCapability() {
  if (view.serviceRestartLoading || view.serviceRestarting) return;
  view.serviceRestartLoading = true;
  let errorMessage = "";
  renderServiceRestartStatus();
  try {
    view.serviceRestartSupported = await fetchRestartCapability();
    renderServiceRestartStatus();
  } catch (_error) {
    view.serviceRestartSupported = false;
    errorMessage = "无法读取服务运行方式";
  } finally {
    view.serviceRestartLoading = false;
    renderServiceRestartStatus(errorMessage, errorMessage ? "error" : "");
  }
}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitForServiceRestart(previousStartedAt) {
  const deadline = Date.now() + 45_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch("/api/snapshot");
      if (response.ok) {
        const snapshot = await response.json();
        if (
          typeof snapshot.startedAt === "string"
          && snapshot.startedAt !== previousStartedAt
        ) {
          window.location.reload();
          return;
        }
      }
    } catch (_error) {
      // The expected connection gap proves the old process is stopping.
    }
    await wait(500);
  }
  view.serviceRestarting = false;
  renderServiceRestartStatus("服务未在 45 秒内恢复，请检查 systemd 状态", "error");
  setConnection("offline", "重启超时");
}

async function restartManagedService() {
  if (!view.serviceRestartSupported || view.serviceRestarting) return;
  const previousStartedAt = view.snapshot?.startedAt;
  if (typeof previousStartedAt !== "string") {
    renderServiceRestartStatus("尚未获得有效服务状态，请稍后重试", "error");
    return;
  }
  view.serviceRestarting = true;
  elements.restartConfirmDialog.close();
  renderServiceRestartStatus();
  setConnection("connecting", "正在重启");
  try {
    const response = await postJson("/api/service/restart", {});
    if (!response.ok) {
      view.serviceRestarting = false;
      renderServiceRestartStatus("服务拒绝了重启请求，请刷新状态后重试", "error");
      renderConnectionStatus();
      return;
    }
  } catch (_error) {
    // The connection may close after the restart was accepted, so recovery is
    // authoritative; the POST is never retried.
  }
  await waitForServiceRestart(previousStartedAt);
}

function renderRuntimeStatus(snapshot) {
  const persistence = snapshot.persistence || {};
  if (!persistence.enabled) {
    setRuntimeStatus(elements.persistenceStatus, "仅内存 · 重启后清空");
  } else if (persistence.healthy) {
    const queued = numeric(persistence.queuedWrites);
    const written = numeric(persistence.writtenRecords);
    setRuntimeStatus(
      elements.persistenceStatus,
      queued ? `SQLite 正常 · ${format(queued)} 项待写` : `SQLite 正常 · 已写 ${format(written)} 条`,
      "success",
    );
  } else {
    setRuntimeStatus(
      elements.persistenceStatus,
      `SQLite 异常 · 丢弃 ${format(numeric(persistence.droppedWrites))} 条`,
      "error",
    );
  }

  const notifications = snapshot.notifications || {};
  elements.notificationTest.disabled = !notifications.enabled || view.notificationTestPending;
  if (!notifications.enabled) {
    setRuntimeStatus(elements.notificationStatus, "未配置");
  } else if (notifications.healthy) {
    const endpoints = Array.isArray(notifications.endpoints)
      ? notifications.endpoints.length : 0;
    const queued = numeric(notifications.queuedDeliveries);
    setRuntimeStatus(
      elements.notificationStatus,
      `${format(endpoints)} 个端点正常${queued ? ` · ${format(queued)} 项待发` : ""}`,
      "success",
    );
  } else {
    setRuntimeStatus(
      elements.notificationStatus,
      `投递异常 · ${format(numeric(notifications.droppedDeliveries))} 次失败`,
      "error",
    );
  }
  renderNotificationEndpoints(notifications);
}

function notificationEndpointRow(endpoint) {
  const name = typeof endpoint.name === "string" && endpoint.name
    ? endpoint.name : "未命名端点";
  const healthy = endpoint.healthy === true;
  const queued = numeric(endpoint.queuedDeliveries);
  const dropped = numeric(endpoint.droppedDeliveries);
  const row = create("div", "notification-endpoint");
  const identity = create("span", "notification-endpoint-name");
  identity.append(create("i", `status-dot ${healthy ? "online" : "issue"}`));
  const label = create("strong", "", name);
  label.title = name;
  identity.append(label);
  const metaParts = [`待发 ${format(queued)}`, `累计失败 ${format(dropped)}`];
  if (endpoint.lastSuccessAt) metaParts.push(`最近成功 ${age(endpoint.lastSuccessAt)}`);
  const state = create("em", healthy ? "success" : "error");
  const lastError = typeof endpoint.lastError === "string" ? endpoint.lastError : "";
  state.textContent = healthy ? "正常" : lastError || "投递异常";
  if (!healthy && lastError) state.title = lastError;
  row.append(identity, create("small", "", metaParts.join(" · ")), state);
  return row;
}

function renderNotificationEndpoints(notifications) {
  // Per-endpoint delivery state (snapshot.notifications.endpoints) so one
  // failing webhook is distinguishable from a fleet-wide outage.
  const endpoints = notifications.enabled && Array.isArray(notifications.endpoints)
    ? notifications.endpoints.filter(
      (endpoint) => endpoint && typeof endpoint === "object" && !Array.isArray(endpoint),
    )
    : [];
  if (!endpoints.length) {
    elements.notificationEndpoints.hidden = true;
    elements.notificationEndpoints.replaceChildren();
    return;
  }
  elements.notificationEndpoints.replaceChildren(
    ...endpoints.slice(0, 12).map(notificationEndpointRow),
  );
  elements.notificationEndpoints.hidden = false;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function downloadJson(value, filename) {
  downloadBlob(
    new Blob([`${JSON.stringify(value, null, 2)}\n`], { type: "application/json;charset=utf-8" }),
    filename,
  );
}

async function exportDiagnostics() {
  elements.exportDiagnostics.disabled = true;
  try {
    const query = view.selectedHost === "all"
      ? "" : `?host=${encodeURIComponent(view.selectedHost)}`;
    const response = await fetch(`/api/diagnostics${query}`);
    const bundle = await response.json();
    if (!response.ok) throw new Error(bundle.error || "诊断导出失败");
    const scope = view.selectedHost === "all" ? "fleet" : view.selectedHost;
    downloadJson(bundle, `mocop-diagnostics-${scope}.json`);
  } catch (_error) {
    elements.notificationTestStatus.textContent = "诊断包导出失败";
  } finally {
    elements.exportDiagnostics.disabled = false;
  }
}

async function testNotifications() {
  if (view.notificationTestPending) return;
  view.notificationTestPending = true;
  elements.notificationTest.disabled = true;
  elements.notificationTestStatus.textContent = "正在加入安全投递队列…";
  try {
    const response = await postJson("/api/notifications/test", {});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "测试投递失败");
    elements.notificationTestStatus.textContent = "测试通知已排队，请查看最新投递状态";
  } catch (_error) {
    elements.notificationTestStatus.textContent = "测试不可用或触发过于频繁";
  } finally {
    view.notificationTestPending = false;
    renderRuntimeStatus(view.snapshot || {});
  }
}

async function requestManualProbe() {
  if (view.selectedHost === "all" || view.manualProbePending) return;
  const host = view.selectedHost;
  view.manualProbePending = true;
  elements.probeNow.disabled = true;
  elements.probeNow.textContent = "正在排队";
  try {
    const response = await postJson("/api/probe", { host });
    const result = await response.json();
    if (!response.ok && result.status !== "in_progress") {
      throw new Error(result.error || result.status || "探测请求失败");
    }
    elements.probeNow.textContent = result.status === "in_progress" ? "正在探测" : "已加入队列";
  } catch (_error) {
    elements.probeNow.textContent = "稍后重试";
  } finally {
    view.manualProbePending = false;
    setTimeout(() => {
      if (view.snapshot) renderTable();
    }, 1200);
  }
}

function serverItem(server, selectedHost) {
  const [label, stateClass] = serverStatus(server);
  const resources = serverResources(server);
  const item = create("button", `server-item${selectedHost === server.host ? " selected" : ""}`);
  item.type = "button";
  if (selectedHost === server.host) item.setAttribute("aria-current", "true");
  item.title = [
    `${displayHost(server)} · ${label}`,
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
  identity.append(create("i", `status-dot ${stateClass}`), create("span", "server-name", displayHost(server)));
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
    server.displayName,
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
  item.dataset.host = "all";
  if (view.selectedHost === "all") item.setAttribute("aria-current", "true");
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
  [...view.expandedHosts].forEach((host) => {
    if (!knownHosts.has(host)) view.expandedHosts.delete(host);
  });
  [...view.fleetGroupCache.keys()].forEach((group) => {
    if (!visibleGroupKeys.has(group)) view.fleetGroupCache.delete(group);
  });
  const focusedHost = document.activeElement?.closest(".server-item")?.dataset.host;
  reconcileChildren(elements.serverList, desired);
  if (focusedHost && !document.activeElement?.isConnected) {
    [...elements.serverList.querySelectorAll(".server-item")]
      .find((item) => item.dataset.host === focusedHost)
      ?.focus({ preventScroll: true });
  }
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
  // Fleet view surfaces the worst node: pressure is a per-node stall signal,
  // so averaging across hosts would hide the one node that is actually stuck.
  const psiPeak = (resourceKey) => {
    const values = systems
      .map((system) => system.pressure?.[resourceKey]?.some_avg10)
      .filter((value) => value != null)
      .map((value) => numeric(value, NaN))
      .filter((value) => Number.isFinite(value));
    return values.length ? Math.max(...values) : NaN;
  };
  const psiMemory = psiPeak("memory");
  const psiIo = psiPeak("io");
  const psiCpu = psiPeak("cpu");
  const psiAvailable = [psiMemory, psiIo, psiCpu].some((value) => Number.isFinite(value));
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
      "压力 PSI",
      psiAvailable
        ? `内存 ${Number.isFinite(psiMemory) ? `${format(psiMemory, 1)}%` : "—"}`
        : "内核未提供",
      psiAvailable
        ? `I/O ${Number.isFinite(psiIo) ? `${format(psiIo, 1)}%` : "—"} · CPU ${Number.isFinite(psiCpu) ? `${format(psiCpu, 1)}%` : "—"}${systems.length > 1 ? " · 节点峰值" : ""}`
        : "需要内核 4.20+ 的 PSI 支持",
      psiAvailable && Number.isFinite(psiMemory) ? psiMemory : null,
      threshold.psi_memory_some_pct,
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
  tile.title = `${displayHost(server)} · GPU ${gpu.index} · ${gpu.name} · ${metric.label}`;
  tile.setAttribute("aria-label", tile.title);
  tile.append(
    create("small", "", `#${gpu.index}`),
    create("strong", "", metric.label),
  );
  tile.addEventListener("click", () => openGpuDetail(server, gpu));
  return tile;
}

function heatmapRow(server, columns) {
  const row = create("div", "heatmap-row"); row.dataset.host = server.host;
  row.style.setProperty("--heat-columns", columns);
  const rowCount = Math.max(1, Math.ceil((Math.max(
    ...server.gpus.map((gpu) => numeric(gpu.index)),
  ) + 1) / columns));
  row.style.setProperty("--heat-rows", rowCount);
  const host = create("button", "heatmap-host", displayHost(server));
  host.type = "button";
  host.title = `查看 ${displayHost(server)}`;
  host.addEventListener("click", () => selectHost(server.host));
  row.append(host);
  const byIndex = new Map(server.gpus.map((gpu) => [numeric(gpu.index), gpu]));
  for (let index = 0; index < columns * rowCount; index += 1) {
    const gpu = byIndex.get(index);
    row.append(gpu ? heatmapTile(server, gpu) : create("span", "heatmap-cell placeholder"));
  }
  return row;
}

function cachedHeatmapRow(server, columns) {
  const signature = JSON.stringify({
    metric: `${view.heatMetric}\u0000${server.displayName || ""}`,
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
  const columns = Math.min(
    MAX_HEATMAP_COLUMNS,
    Math.max(
      1,
      ...servers.flatMap((server) => server.gpus.map((gpu) => numeric(gpu.index) + 1)),
    ),
  );
  const activeHosts = new Set(servers.map((server) => server.host));
  [...view.heatmapCache.keys()].forEach((host) => {
    if (!activeHosts.has(host)) view.heatmapCache.delete(host);
  });
  document.querySelectorAll(".heatmap-mode").forEach((button) => {
    const selected = button.dataset.heatMetric === view.heatMetric;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
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

// X positions follow observedAt so uneven sampling keeps its real spacing;
// gapBefore marks pauses clearly longer than the observed cadence, which
// split the drawn path instead of bridging the outage.
function chartPositions(points) {
  const fallback = {
    xs: points.map((_, index) =>
      points.length > 1 ? (index / (points.length - 1)) * 220 : 220),
    gapBefore: points.map(() => false),
  };
  if (points.length < 2) return fallback;
  const times = points.map((point) => Date.parse(point.observedAt));
  if (!times.every(Number.isFinite)) return fallback;
  const span = times.at(-1) - times[0];
  if (!(span > 0)) return fallback;
  const deltas = times.slice(1).map((time, index) => time - times[index]);
  const positive = deltas.filter((delta) => delta > 0).sort((a, b) => a - b);
  const median = positive.length ? positive[Math.floor(positive.length / 2)] : 0;
  const gapThreshold = median > 0 ? median * 3 : Infinity;
  return {
    xs: times.map((time) => ((time - times[0]) / span) * 220),
    gapBefore: times.map((_, index) => index > 0 && deltas[index - 1] > gapThreshold),
  };
}

const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

function svgElement(tag, attributes) {
  const element = document.createElementNS(SVG_NAMESPACE, tag);
  Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
  return element;
}

// Every trend chart shares one 220x54 canvas with a baseline at y=50.
function chartCanvas() {
  const svg = svgElement("svg", {
    viewBox: "0 0 220 54", preserveAspectRatio: "none", "aria-hidden": "true",
  });
  svg.append(svgElement("line", {
    x1: "0", x2: "220", y1: "50", y2: "50", class: "chart-baseline",
  }));
  return svg;
}

function sparkline(points, accessor, color, maximum = null) {
  const svg = chartCanvas();
  const values = points.map(accessor);
  const finite = values.filter((value) => Number.isFinite(value));
  const ceiling = maximum ?? Math.max(1, ...finite);
  const { xs, gapBefore } = chartPositions(points);
  const segments = [];
  let current = [];
  values.forEach((value, index) => {
    if (!Number.isFinite(value)) {
      if (current.length) segments.push(current);
      current = [];
      return;
    }
    if (gapBefore[index] && current.length) {
      segments.push(current);
      current = [];
    }
    const y = 50 - Math.min(1, Math.max(0, value / ceiling)) * 44;
    current.push([xs[index], y]);
  });
  if (current.length) segments.push(current);
  segments.forEach((segment) => {
    if (segment.length === 1) {
      svg.append(svgElement("circle", {
        cx: segment[0][0].toFixed(1), cy: segment[0][1].toFixed(1), r: "1.6", fill: color,
      }));
      return;
    }
    svg.append(svgElement("polyline", {
      points: segment.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "),
      fill: "none",
      stroke: color,
      "stroke-width": "2",
      "vector-effect": "non-scaling-stroke",
    }));
  });
  return svg;
}

function trendCard(label, points, accessor, formatter, color, maximum = null) {
  const card = create("article", "trend-card");
  const values = points.map(accessor).filter((value) => Number.isFinite(value));
  // The current reading comes from the latest sample only; a missing latest
  // metric shows a gap instead of silently holding the previous value.
  const latest = points.length ? accessor(points.at(-1)) : NaN;
  const top = create("div", "trend-card-top");
  top.append(
    create("span", "", label),
    create("strong", "", Number.isFinite(latest) ? formatter(latest) : "—"),
  );
  card.append(top, sparkline(points, accessor, color, maximum));
  const peak = values.length ? Math.max(...values) : null;
  card.append(create("span", "trend-card-foot", peak == null ? "等待有效速率" : `峰值 ${formatter(peak)}`));
  return card;
}

function transportRetryCard(points) {
  const { xs } = chartPositions(points);
  const retriedXs = xs.filter((_, index) => Boolean(points[index].transportRetried));
  const card = create("article", "trend-card");
  const top = create("div", "trend-card-top");
  top.append(
    create("span", "", "链路重试"),
    create("strong", "", `${format(retriedXs.length)} 次`),
  );
  const svg = chartCanvas();
  retriedXs.forEach((x) => {
    svg.append(svgElement("line", {
      x1: x.toFixed(1),
      x2: x.toFixed(1),
      y1: "18",
      y2: "50",
      stroke: "#f5b95f",
      "stroke-width": "2",
      "vector-effect": "non-scaling-stroke",
    }));
  });
  card.append(
    top,
    svg,
    create("span", "trend-card-foot", "样本期间 SSH 传输层重连标记"),
  );
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
  const renderKey = `${view.selectedHost}:${view.historyLoading}:${view.historyError}:${points.length}:${latestPoint}`;
  if (renderKey === view.trendRenderKey) return;
  view.trendRenderKey = renderKey;
  if (view.historyLoading && !view.history) {
    elements.trendRange.textContent = "正在读取历史";
    elements.trendGrid.replaceChildren(create("div", "trend-empty", "正在收集和加载趋势样本…"));
    return;
  }
  elements.trendRange.textContent = historyDuration(points);
  if (!points.length) {
    elements.trendGrid.replaceChildren(create(
      "div",
      "trend-empty",
      view.historyError ? "历史读取失败，稍后自动重试" : "首次成功采集后将显示趋势",
    ));
    return;
  }
  const cards = [
    trendCard("CPU", points, (point) => optionalMetric(point, "cpuUsagePct"), (value) => `${format(value, 1)}%`, "#6d8cff", 100),
    trendCard("内存", points, (point) => optionalMetric(point, "memoryUsagePct"), (value) => `${format(value, 1)}%`, "#5de0a0", 100),
    trendCard("GPU 平均负载", points, (point) => optionalMetric(point, "gpuUsagePct"), (value) => `${format(value, 1)}%`, "#b68cff", 100),
    trendCard("网络总速率", points, (point) => combinedMetric(point, "networkRxBps", "networkTxBps"), rate, "#53b8dc"),
    trendCard("磁盘总 I/O", points, (point) => combinedMetric(point, "diskReadBps", "diskWriteBps"), rate, "#f5b95f"),
  ];
  if (points.some((point) => point.transportRetried)) {
    cards.push(transportRetryCard(points));
  }
  elements.trendGrid.replaceChildren(...cards);
}

async function syncHistory() {
  if (!view.snapshot || view.selectedHost === "all") return;
  const server = view.snapshot.servers.find((item) => item.host === view.selectedHost);
  if (!server) return;
  const key = `${server.host}:${server.lastSuccessAt || "pending"}`;
  // The key is confirmed only after a successful load, so a failed request
  // stays retryable; the single backoff timer owns retries for a failed key
  // even when lastSuccessAt never advances (offline node).
  if (key === view.historyKey || key === view.historyFetchKey) return;
  if (view.historyRetryTimer != null) {
    if (key === view.historyRetryKey) return;
    clearTimeout(view.historyRetryTimer);
    view.historyRetryTimer = null;
    view.historyRetryKey = "";
  }
  view.historyFetchKey = key;
  view.historyLoading = true;
  renderTrends();
  const request = ++view.historyRequest;
  try {
    const response = await fetch(`/api/history?host=${encodeURIComponent(server.host)}&limit=120`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const history = await response.json();
    if (request !== view.historyRequest || view.selectedHost !== history.host) return;
    view.history = history;
    view.historyKey = key;
    view.historyError = false;
    view.historyRetryDelayMs = 0;
  } catch (_error) {
    if (request !== view.historyRequest) return;
    // Existing trend samples stay on screen through transient fetch failures.
    view.historyError = true;
    view.historyRetryDelayMs = Math.min(
      30_000,
      Math.max(4_000, view.historyRetryDelayMs * 2),
    );
    view.historyRetryKey = key;
    view.historyRetryTimer = setTimeout(() => {
      view.historyRetryTimer = null;
      view.historyRetryKey = "";
      syncHistory();
    }, view.historyRetryDelayMs);
  } finally {
    if (view.historyFetchKey === key) view.historyFetchKey = null;
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
  const gpuConditions = hostConditions(server.host).filter(
    (condition) => String(condition.conditionKey || "").endsWith(`:${identity}`),
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

// Function declarations: gpuProcessName is referenced above (process-search
// wiring) before this point in the file, so these must hoist.
function gpuProcessName(process) {
  return gpuTasks.processName(process);
}

function gpuProcessStartMs(process) {
  return gpuTasks.processStartMs(process);
}

// Display name: the extracted entry point when argv0 is a bare interpreter.
function gpuTaskDisplayName(process) {
  return gpuTasks.taskEntry(process) || gpuProcessName(process);
}

function gpuProcessSummary(gpu) {
  const cached = gpuProcessSummaryCache.get(gpu);
  if (cached) return cached;
  const summary = gpuTasks.summarize(gpu);
  gpuProcessSummaryCache.set(gpu, summary);
  return summary;
}

function gpuTaskInsight(label, value, detail) {
  const item = create("article", "gpu-task-insight");
  item.append(
    create("span", "", label),
    create("strong", "", value),
    create("small", "", detail),
  );
  return item;
}

function gpuProcessSummarySignature(gpu) {
  const summary = gpuProcessSummary(gpu);
  return {
    count: summary.count,
    knownMemory: summary.knownMemoryMiB,
    knownMemoryCount: summary.knownMemoryCount,
    ownedCount: summary.ownedCount,
    topPid: summary.topProcess?.pid,
    topName: summary.topProcess?.name,
    topMemory: summary.topMemoryMiB,
  };
}

function programSearchKey({ server, gpu, process }) {
  return [
    server.host,
    String(gpu.uuid || gpu.index),
    String(process.pid),
    String(process.name || ""),
  ].join("\u0000");
}

function programSearchRow() {
  const item = create("article", "program-search-result");
  item.setAttribute("role", "listitem");
  const button = create("button", "program-search-open");
  button.type = "button";
  const identity = create("span", "program-search-identity");
  const name = create("strong", "program-search-name");
  const command = create("small", "program-search-command");
  identity.append(name, command);
  const placement = create("span", "program-search-placement");
  const location = create("strong");
  const context = create("small");
  placement.append(location, context);
  const workload = create("span", "program-search-workload");
  workload.hidden = true;
  const memorySummary = create("span", "program-search-memory");
  const memoryValue = create("strong");
  const memoryShare = create("small");
  memorySummary.append(memoryValue, memoryShare);
  button.append(identity, placement, workload, memorySummary);
  item.append(button);
  return {
    item,
    button,
    name,
    command,
    location,
    context,
    workload,
    memoryValue,
    memoryShare,
  };
}

function updateProgramSearchRow(row, record) {
  const { server, gpu, process } = record;
  const shortName = gpuProcessName(process);
  const fullName = String(process.name || "");
  const command = process.workload?.command
    || (fullName && fullName !== shortName ? fullName : "");
  row.name.textContent = gpuTaskDisplayName(process);
  row.name.title = fullName || "unknown process";
  row.command.textContent = command || "命令行未采集";
  if (command) row.command.title = command;
  else row.command.removeAttribute("title");
  row.location.textContent = `${displayHost(server)} · GPU ${gpu.index} · PID ${process.pid}`;
  const freshness = gpu.processes_observed_at
    ? `任务数据 ${age(gpu.processes_observed_at)}` : "等待任务数据";
  row.context.textContent = `${server.stale ? "历史样本" : "当前样本"} · ${freshness}`;
  row.context.classList.toggle("stale", server.stale);

  const workload = process.workload;
  if (workload && Object.hasOwn(WORKLOAD_KIND_LABELS, workload.kind)) {
    const kind = WORKLOAD_KIND_LABELS[workload.kind];
    const identity = workload.name || workload.workload_id;
    row.workload.textContent = [
      identity ? `${kind} · ${identity}` : kind,
      workload.owner ? `用户 ${workload.owner}` : "",
      workload.queue ? `队列 ${workload.queue}` : "",
      workload.namespace ? `命名空间 ${workload.namespace}` : "",
    ].filter(Boolean).join(" · ");
    row.workload.hidden = false;
  } else {
    row.workload.textContent = "";
    row.workload.hidden = true;
  }

  const usage = ratio(process.used_memory_mib, gpu.memory_total_mib);
  row.memoryValue.textContent = process.used_memory_mib == null
    ? "显存未知" : memory(process.used_memory_mib);
  row.memoryShare.textContent = process.used_memory_mib == null
    ? "占比未知" : `${format(usage, 1)}% GPU 显存`;
  row.item.dataset.host = server.host;
  row.item.dataset.gpuId = String(gpu.uuid || gpu.index);
  row.button.setAttribute(
    "aria-label",
    `打开 ${displayHost(server)} GPU ${gpu.index} 上的 ${shortName}，PID ${process.pid}`,
  );
  row.button.onclick = () => {
    const key = `${process.pid}|${process.name || ""}`;
    openGpuDetail(server, gpu, { processQuery: view.query, processKey: key });
  };
}

function renderProgramSearch() {
  const terms = normalizedSearchTerms(view.query);
  const scope = view.selectedHost === "all" ? "全局" : displayHost(view.selectedHost);
  elements.programSearchScope.textContent = scope;
  elements.search.setAttribute("aria-label", `在${scope}搜索服务器、GPU 或程序`);
  elements.search.placeholder = view.selectedHost === "all"
    ? "搜索服务器、GPU 或程序"
    : `在 ${displayHost(view.selectedHost)} 搜索 GPU 或程序`;
  elements.programSearchPanel.hidden = !terms.length;
  if (!terms.length) {
    view.programSearchRowCache.clear();
    elements.programSearchResults.replaceChildren();
    elements.programSearchCount.textContent = "0";
    elements.programSearchSummary.textContent = "";
    elements.programSearchResults.setAttribute("role", "list");
    return;
  }

  const result = searchProcessRecords(view.snapshot, view.query, view.selectedHost);
  elements.programSearchCount.textContent = String(result.total);
  const notes = [];
  if (!result.total) notes.push(`${scope}没有匹配的活跃程序`);
  else if (result.total > result.matches.length) {
    notes.push(`找到 ${result.total} 条，显示相关度最高的 ${result.matches.length} 条`);
  } else {
    notes.push(`找到 ${result.total} 条 GPU 进程记录`);
  }
  if (result.staleCount) notes.push(`含 ${result.staleCount} 条历史样本`);
  if (result.unavailableGpuCount) {
    notes.push(`${result.unavailableGpuCount} 张 GPU 的任务数据不可用`);
  }
  const summary = notes.join(" · ");
  if (elements.programSearchSummary.textContent !== summary) {
    elements.programSearchSummary.textContent = summary;
  }

  if (!result.matches.length) {
    view.programSearchRowCache.clear();
    elements.programSearchResults.setAttribute("role", "status");
    elements.programSearchResults.replaceChildren(
      create("div", "program-search-empty", "换一个名称、PID、用户或 workload 关键词重试"),
    );
    return;
  }

  elements.programSearchResults.setAttribute("role", "list");
  const seenKeys = new Set();
  const desiredRows = result.matches.map((record) => {
    const key = programSearchKey(record);
    seenKeys.add(key);
    let row = view.programSearchRowCache.get(key);
    if (!row) {
      row = programSearchRow();
      view.programSearchRowCache.set(key, row);
    }
    updateProgramSearchRow(row, record);
    return row.item;
  });
  [...view.programSearchRowCache.keys()].forEach((key) => {
    if (!seenKeys.has(key)) view.programSearchRowCache.delete(key);
  });
  reconcileChildren(elements.programSearchResults, desiredRows);
}

// Rows are keyed by pid|name so snapshot refreshes update fields in place
// instead of rebuilding the list, keeping scroll position and bar widths.
function gpuTaskRow() {
  const item = create("article", "gpu-task");
  item.setAttribute("role", "listitem");
  item.tabIndex = -1;
  const identity = create("div", "gpu-task-identity");
  const name = create("strong", "gpu-task-name");
  const command = create("small", "gpu-task-command");
  command.hidden = true;
  identity.append(name, command);
  const memorySummary = create("div", "gpu-task-memory");
  const memoryValue = create("strong");
  const memoryShare = create("small");
  memorySummary.append(memoryValue, memoryShare);
  const workload = create("div", "gpu-task-workload");
  workload.hidden = true;
  const meta = create("div", "gpu-task-meta");
  const metaInfo = create("span", "gpu-task-meta-info");
  const pid = create("span");
  const runtime = create("span", "gpu-task-duration");
  runtime.hidden = true;
  const footprint = create("span", "gpu-task-footprint");
  footprint.hidden = true;
  metaInfo.append(pid, runtime, footprint);
  const track = create("div", "mini-track");
  const bar = create("i");
  track.append(bar);
  meta.append(metaInfo, track);
  const actions = create("div", "gpu-task-actions");
  const searchFleet = create("button", "gpu-task-action", "全局查找");
  const copyPid = create("button", "gpu-task-action", "复制 PID");
  const copyCommand = create("button", "gpu-task-action", "复制命令");
  [searchFleet, copyPid, copyCommand].forEach((button) => {
    button.type = "button";
  });
  actions.append(searchFleet, copyPid, copyCommand);
  item.append(identity, memorySummary, workload, meta, actions);
  return {
    item,
    name,
    command,
    memoryValue,
    memoryShare,
    workload,
    pid,
    runtime,
    footprint,
    bar,
    searchFleet,
    copyPid,
    copyCommand,
  };
}

function filterCurrentGpuTasks(value) {
  const query = String(value || "").slice(0, MAX_SEARCH_QUERY_LENGTH);
  view.gpuTaskIdentityFilter = "all";
  view.gpuTaskQuery = query;
  view.selectedProcessKey = "";
  elements.gpuTaskSearch.value = query;
  renderGpuDetail();
  elements.gpuTaskSearch.focus({ preventScroll: true });
}

function searchFleetForProcess(process) {
  const query = gpuTaskDisplayName(process).slice(0, MAX_SEARCH_QUERY_LENGTH);
  view.query = query;
  elements.search.value = query;
  elements.gpuDetailDialog.close();
  if (view.selectedHost !== "all") selectHost("all");
  else render();
  elements.search.focus({ preventScroll: true });
  elements.programSearchPanel.scrollIntoView({ block: "nearest" });
}

function setGpuTaskFeedback(message, kind = "success") {
  if (view.gpuTaskFeedbackTimer != null) clearTimeout(view.gpuTaskFeedbackTimer);
  elements.gpuTaskFeedback.textContent = message;
  elements.gpuTaskFeedback.className = `gpu-task-feedback ${kind}`;
  view.gpuTaskFeedbackTimer = setTimeout(() => {
    view.gpuTaskFeedbackTimer = null;
    elements.gpuTaskFeedback.textContent = "";
    elements.gpuTaskFeedback.className = "gpu-task-feedback";
  }, 3000);
}

async function copyGpuTaskText(value, successMessage) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(String(value));
    setGpuTaskFeedback(successMessage);
  } catch (_error) {
    setGpuTaskFeedback("复制失败，请手动选择文本", "error");
  }
}

function updateGpuTaskRow(row, process, gpu) {
  const shortName = gpuProcessName(process);
  // A bare interpreter name identifies nothing; lead with the actual entry
  // point (module or script) extracted from the command line when available.
  const entry = gpuTasks.taskEntry(process);
  row.name.textContent = entry || shortName;
  row.name.title = process.name || "unknown process";
  const fullName = String(process.name || "");
  const command = process.workload?.command
    || (fullName && fullName !== shortName ? fullName : "");
  row.command.hidden = !command;
  row.command.textContent = command;
  if (command) {
    row.command.title = "点击展开或收起完整命令";
    row.command.onclick = () => row.command.classList.toggle("expanded");
  } else {
    row.command.removeAttribute("title");
    row.command.onclick = null;
    row.command.classList.remove("expanded");
  }
  const footprint = gpuTasks.footprint(process, Date.now());
  const footprintParts = [];
  if (footprint.averageCores != null) {
    footprintParts.push(`CPU 均值 ${format(footprint.averageCores, 1)} 核`);
  }
  if (footprint.memoryMiB != null) {
    footprintParts.push(`主机内存 ${memory(footprint.memoryMiB)}`);
  }
  row.footprint.hidden = footprintParts.length === 0;
  row.footprint.textContent = footprintParts.join(" · ");
  const usage = ratio(process.used_memory_mib, gpu.memory_total_mib);
  row.memoryValue.textContent = process.used_memory_mib == null
    ? "显存未知" : memory(process.used_memory_mib);
  row.memoryShare.textContent = process.used_memory_mib == null
    ? "占比未知" : `${format(usage, 1)}% GPU 显存`;
  row.bar.style.width = `${clamp(usage)}%`;
  row.pid.textContent = `PID ${process.pid}`;
  const startedAt = process.workload?.started_at || "";
  const observedAt = process.first_seen_at || "";
  const runtimeSince = startedAt || observedAt;
  if (runtimeSince && Number.isFinite(Date.parse(runtimeSince))) {
    const prefix = startedAt ? "运行 " : "已观测 ";
    row.runtime.hidden = false;
    row.runtime.dataset.durationSince = runtimeSince;
    row.runtime.dataset.durationPrefix = prefix;
    if (startedAt) row.runtime.removeAttribute("title");
    else row.runtime.title = "自监控首次观测起，服务重启后重新计算";
    row.runtime.textContent = `${prefix}${durationSince(runtimeSince)}`;
  } else {
    row.runtime.hidden = true;
    row.runtime.textContent = "";
    delete row.runtime.dataset.durationSince;
    delete row.runtime.dataset.durationPrefix;
    row.runtime.removeAttribute("title");
  }
  const workload = process.workload;
  const chips = [];
  if (workload && Object.hasOwn(WORKLOAD_KIND_LABELS, workload.kind)) {
    const kind = WORKLOAD_KIND_LABELS[workload.kind];
    const workloadIdentity = workload.name || workload.workload_id;
    const identityChip = create(
      workloadIdentity ? "button" : "span",
      "primary",
      workloadIdentity ? `${kind} · ${workloadIdentity}` : kind,
    );
    if (workloadIdentity) {
      identityChip.type = "button";
      identityChip.title = `筛选 ${workloadIdentity}`;
      identityChip.onclick = () => filterCurrentGpuTasks(workloadIdentity);
    }
    chips.push(identityChip);
    if (workload.name && workload.workload_id) {
      chips.push(create("span", "", `ID ${workload.workload_id}`));
    }
    [
      [workload.owner, "用户"],
      [workload.queue, "队列"],
      [workload.namespace, "命名空间"],
    ].forEach(([value, label]) => {
      if (!value) return;
      const chip = create("button", "", `${label} ${value}`);
      chip.type = "button";
      chip.title = `筛选 ${value}`;
      chip.onclick = () => filterCurrentGpuTasks(value);
      chips.push(chip);
    });
  }
  const environment = gpuTasks.environmentName(process);
  if (environment) chips.push(create("span", "", `环境 ${environment}`));
  if (entry) chips.push(create("span", "", `解释器 ${shortName}`));
  row.workload.hidden = chips.length === 0;
  row.workload.replaceChildren(...chips);
  row.searchFleet.setAttribute("aria-label", `在全部服务器查找 ${shortName}`);
  row.searchFleet.onclick = () => searchFleetForProcess(process);
  row.copyPid.setAttribute("aria-label", `复制 ${shortName} 的 PID ${process.pid}`);
  row.copyPid.onclick = () => copyGpuTaskText(process.pid, `已复制 PID ${process.pid}`);
  row.copyCommand.hidden = !command;
  row.copyCommand.setAttribute("aria-label", `复制 ${shortName} 的完整命令`);
  row.copyCommand.onclick = () => copyGpuTaskText(command, "已复制完整命令");
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

function renderGpuHistory() {
  const points = view.gpuHistory?.points || [];
  // Mirror renderTrends: the cards and timeline only change when the fetched
  // history does, not on every snapshot that arrives while the dialog is open.
  const eventCount = view.gpuHistory?.processEvents?.length ?? 0;
  const renderKey = [
    view.gpuHistoryKey, view.gpuHistoryLoading, view.gpuHistoryError,
    points.length, points.at(-1)?.observedAt || "none", eventCount,
  ].join(":");
  if (renderKey === view.gpuHistoryRenderKey) return;
  view.gpuHistoryRenderKey = renderKey;
  if (view.gpuHistoryLoading && !view.gpuHistory) {
    elements.gpuHistoryRange.textContent = "正在读取";
    elements.gpuHistoryGrid.replaceChildren(
      create("div", "gpu-history-empty", "正在加载单卡历史…"),
    );
    elements.gpuProcessTimeline.replaceChildren();
    return;
  }
  elements.gpuHistoryRange.textContent = view.gpuHistoryError && !points.length
    ? "读取失败" : historyDuration(points);
  if (!points.length) {
    // A failed request must read as a failure, not as "no samples yet";
    // existing samples stay on screen through transient failures.
    elements.gpuHistoryGrid.replaceChildren(
      create(
        "div",
        "gpu-history-empty",
        view.gpuHistoryError ? "历史读取失败，稍后重试" : "完成两次成功采集后显示趋势",
      ),
    );
  } else {
    // Missing used or total memory renders as a gap instead of a fake 0%.
    const memoryRatioMetric = (point) =>
      point.memoryUsedMiB == null || !(numeric(point.memoryTotalMiB) > 0)
        ? NaN : ratio(point.memoryUsedMiB, point.memoryTotalMiB);
    elements.gpuHistoryGrid.replaceChildren(
      trendCard("GPU 负载", points, (point) => optionalMetric(point, "utilizationGpuPct"), (value) => `${format(value, 1)}%`, "#6d8cff", 100),
      trendCard("显存", points, memoryRatioMetric, (value) => `${format(value, 1)}%`, "#b68cff", 100),
      trendCard("温度", points, (point) => optionalMetric(point, "temperatureC"), (value) => `${format(value, 1)}°C`, "#f5b95f"),
      trendCard("功耗", points, (point) => optionalMetric(point, "powerDrawW"), (value) => `${format(value, 1)} W`, "#5de0a0"),
    );
  }
  const events = Array.isArray(view.gpuHistory?.processEvents)
    ? view.gpuHistory.processEvents.slice().reverse() : [];
  if (!events.length) {
    elements.gpuProcessTimeline.replaceChildren(
      create(
        "div",
        "gpu-history-empty",
        view.gpuHistoryError && !view.gpuHistory
          ? "历史读取失败，稍后重试" : "暂未记录到进程进入或退出",
      ),
    );
    return;
  }
  elements.gpuProcessTimeline.replaceChildren(...events.slice(0, 24).map((event) => {
    const item = create("article", `gpu-timeline-item ${event.event}`);
    const process = gpuProcessName(event);
    const summary = `${event.event === "started" ? "进入" : "退出"} · ${process} · PID ${event.pid}`;
    item.append(
      create("i"),
      create("span", "", summary),
      create("strong", "age-relative", age(event.observedAt)),
    );
    item.lastElementChild.dataset.ageAt = event.observedAt;
    return item;
  }));
}

function clearGpuHistoryRetry() {
  if (view.gpuHistoryRetryTimer != null) clearTimeout(view.gpuHistoryRetryTimer);
  view.gpuHistoryRetryTimer = null;
  view.gpuHistoryRetryKey = "";
}

async function syncGpuHistory(record) {
  if (!record || !elements.gpuDetailDialog.open) return;
  const gpuId = String(record.gpu.uuid || `index:${record.gpu.index}`);
  // Keyed on lastSuccessAt: only a successful sample can add history points,
  // so failed probe attempts no longer trigger refetches. The key is
  // confirmed only after a successful load, keeping failures retryable
  // through the single backoff timer (mirrors syncHistory).
  const key = `${record.server.host}|${gpuId}|${record.server.lastSuccessAt || ""}`;
  if (key === view.gpuHistoryKey || key === view.gpuHistoryFetchKey) return;
  if (view.gpuHistoryRetryTimer != null) {
    if (key === view.gpuHistoryRetryKey) return;
    clearGpuHistoryRetry();
  }
  view.gpuHistoryFetchKey = key;
  view.gpuHistoryLoading = true;
  const request = ++view.gpuHistoryRequest;
  renderGpuHistory();
  try {
    const response = await fetch(
      `/api/gpu-history?host=${encodeURIComponent(record.server.host)}&gpu=${encodeURIComponent(gpuId)}&limit=120`,
    );
    const history = await response.json();
    if (!response.ok) throw new Error(history.error || "GPU history unavailable");
    if (request !== view.gpuHistoryRequest) return;
    view.gpuHistory = history;
    view.gpuHistoryKey = key;
    view.gpuHistoryError = false;
    view.gpuHistoryRetryDelayMs = 0;
  } catch (_error) {
    if (request !== view.gpuHistoryRequest) return;
    // Existing samples stay on screen; the bounded backoff owns retries for
    // this same dialog and key.
    view.gpuHistoryError = true;
    view.gpuHistoryRetryDelayMs = Math.min(
      30_000,
      Math.max(4_000, view.gpuHistoryRetryDelayMs * 2),
    );
    view.gpuHistoryRetryKey = key;
    view.gpuHistoryRetryTimer = setTimeout(() => {
      view.gpuHistoryRetryTimer = null;
      view.gpuHistoryRetryKey = "";
      syncGpuHistory(selectedGpuRecord());
    }, view.gpuHistoryRetryDelayMs);
  } finally {
    if (view.gpuHistoryFetchKey === key) view.gpuHistoryFetchKey = null;
    if (request === view.gpuHistoryRequest) {
      view.gpuHistoryLoading = false;
      renderGpuHistory();
    }
  }
}

function syncGpuTaskIdentityFilters(processes) {
  const counts = {
    all: processes.length,
    owned: processes.filter((process) => Boolean(process.workload?.owner)).length,
    unowned: processes.filter((process) => !process.workload?.owner).length,
  };
  const labels = { all: "全部", owned: "已归属", unowned: "未归属" };
  document.querySelectorAll("[data-gpu-task-filter]").forEach((button) => {
    const filter = button.dataset.gpuTaskFilter;
    const active = filter === view.gpuTaskIdentityFilter;
    button.textContent = `${labels[filter]} ${counts[filter]}`;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function processMatchesIdentityFilter(process) {
  if (view.gpuTaskIdentityFilter === "owned") return Boolean(process.workload?.owner);
  if (view.gpuTaskIdentityFilter === "unowned") return !process.workload?.owner;
  return true;
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
  const processSummary = gpuProcessSummary(gpu);
  const taskTerms = normalizedSearchTerms(view.gpuTaskQuery);
  const scopedProcesses = processes.filter(processMatchesIdentityFilter);
  const matchingProcesses = taskTerms.length
    ? scopedProcesses.filter(
      (process) => processMatchesSearch(process, taskTerms, server, gpu),
    )
    : scopedProcesses.slice();
  const sortByDuration = preferences.gpuTaskSort === "duration";
  const sortByName = preferences.gpuTaskSort === "name";
  if (sortByDuration) {
    matchingProcesses.sort((a, b) => gpuProcessStartMs(a) - gpuProcessStartMs(b)
      || processMemoryRank(a, b));
  } else if (sortByName) {
    matchingProcesses.sort((a, b) => gpuTaskDisplayName(a).localeCompare(gpuTaskDisplayName(b))
      || numeric(a.pid) - numeric(b.pid));
  } else {
    matchingProcesses.sort(processMemoryRank);
  }
  let visibleProcesses = matchingProcesses.slice(0, MAX_GPU_DETAIL_PROCESSES);
  const selectedProcess = view.selectedProcessKey
    ? matchingProcesses.find(
      (process) => `${process.pid}|${process.name || ""}` === view.selectedProcessKey,
    )
    : null;
  const selectedProcessPinned = selectedProcess != null
    && !visibleProcesses.includes(selectedProcess);
  if (selectedProcessPinned) {
    visibleProcesses = [
      selectedProcess,
      ...visibleProcesses.slice(0, MAX_GPU_DETAIL_PROCESSES - 1),
    ];
  }
  const processMemoryTotal = processSummary.knownMemoryMiB;
  const processMemoryPct = ratio(processMemoryTotal, gpu.memory_total_mib);
  const processFreshness = gpu.processes_observed_at
    ? `任务数据 ${age(gpu.processes_observed_at)}`
    : "等待任务数据";
  const staleProcessFreshness = Boolean(gpu.processes_observed_at)
    && Date.now() - Date.parse(gpu.processes_observed_at)
      > GPU_PROCESS_FRESHNESS_WARNING_MS;
  const emptyCardClass = staleProcessFreshness
    ? "gpu-task-empty gpu-task-freshness-stale" : "gpu-task-empty";

  elements.gpuDetailHost.textContent = `${displayHost(server)} · GPU ${gpu.index}`;
  elements.gpuDetailTitle.textContent = gpu.name || "Unknown NVIDIA GPU";
  elements.gpuDetailSsh.dataset.host = server.host;
  elements.gpuDetailSsh.title = `复制 ssh ${server.host}`;
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
  renderGpuHistory();
  syncGpuHistory(record);
  syncGpuTaskSortButtons();
  syncGpuTaskIdentityFilters(processes);
  const oldestProcess = processSummary.oldestProcess;
  const oldestTimestamp = oldestProcess?.workload?.started_at
    || oldestProcess?.first_seen_at || "";
  const oldestLabel = oldestTimestamp ? durationSince(oldestTimestamp) : "—";
  const oldestDetail = oldestProcess?.workload?.started_at
    ? "按真实启动时间" : oldestProcess?.first_seen_at ? "按监控首次观测" : "缺少时间信号";
  elements.gpuTaskInsights.replaceChildren(
    gpuTaskInsight(
      "活跃进程",
      gpu.processes_available === false ? "—" : String(processSummary.count),
      gpu.processes_sampled === false ? "复用上次样本" : "本轮进程样本",
    ),
    gpuTaskInsight(
      "显存覆盖",
      gpu.processes_available === false
        ? "—" : `${processSummary.knownMemoryCount} / ${processSummary.count}`,
      processSummary.knownMemoryCount
        ? `${memory(processSummary.knownMemoryMiB)} 已知分配` : "没有可用分配值",
    ),
    gpuTaskInsight(
      "身份归属",
      gpu.processes_available === false
        ? "—" : `${processSummary.ownedCount} / ${processSummary.count}`,
      processSummary.count
        ? `${processSummary.ownerCount} 个使用者 · ${processSummary.identifiedCount} 个含 workload`
        : "没有活跃进程",
    ),
    gpuTaskInsight("最长运行", gpu.processes_available === false ? "—" : oldestLabel, oldestDetail),
  );
  const filtered = taskTerms.length || view.gpuTaskIdentityFilter !== "all";
  elements.gpuTaskCount.textContent = gpu.processes_available === false
    ? "—"
    : filtered ? `${matchingProcesses.length} / ${processes.length}` : String(processes.length);
  if (gpu.processes_available === false) {
    elements.gpuTaskOverview.hidden = true;
    view.gpuTaskRowCache.clear();
    elements.gpuTaskList.setAttribute("role", "status");
    elements.gpuTaskList.replaceChildren(
      create("div", "gpu-task-empty", "任务数据暂不可用；GPU 指标仍会继续刷新。"),
    );
    return;
  }
  elements.gpuTaskOverview.hidden = false;
  elements.gpuTaskMemoryTotal.textContent = `${memory(processMemoryTotal)} / ${memory(gpu.memory_total_mib)}`;
  elements.gpuTaskMemoryBar.style.width = `${clamp(processMemoryPct)}%`;
  elements.gpuTaskNote.classList.toggle(
    "gpu-task-freshness-stale", staleProcessFreshness,
  );
  const freshnessHint = "无人查看时采样自动放缓，打开页面后数秒内自动追赶";
  // Identity attribution is an opt-in collector layer; hint once (title only)
  // when every listed process lacks workload metadata.
  const identityOff = visibleProcesses.length > 0
    && visibleProcesses.every((process) => !process.workload);
  elements.gpuTaskNote.title = identityOff
    ? `${freshnessHint}；启用 workloads.mode=identity 可显示属主与完整命令行`
    : freshnessHint;
  const sortLabel = sortByDuration
    ? "按运行时长从长到短排列"
    : sortByName ? "按程序名排列" : "按显存占用从高到低排列";
  if (matchingProcesses.length > visibleProcesses.length) {
    const truncationLabel = sortByDuration
      ? "运行最久"
      : sortByName ? "名称靠前" : "显存占用最高";
    const matchLabel = taskTerms.length ? "匹配进程" : "进程";
    const selectedLabel = selectedProcessPinned ? "，并优先显示所选程序" : "";
    elements.gpuTaskNote.textContent = `共 ${matchingProcesses.length} 个${matchLabel}，仅展示${truncationLabel}的 ${visibleProcesses.length} 个${selectedLabel} · ${processFreshness}`;
  } else if (processSummary.knownMemoryCount < processes.length) {
    const matched = filtered
      ? `${matchingProcesses.length} / ${processes.length} 个匹配 · ` : "";
    elements.gpuTaskNote.textContent = `${matched}${processes.length - processSummary.knownMemoryCount} 个进程未返回显存占用 · ${processFreshness}`;
  } else {
    const matched = filtered
      ? `${matchingProcesses.length} / ${processes.length} 个匹配 · ` : "";
    elements.gpuTaskNote.textContent = `${matched}${sortLabel} · ${processFreshness}`;
  }
  if (!processes.length) {
    view.gpuTaskRowCache.clear();
    elements.gpuTaskList.setAttribute("role", "status");
    const emptyCard = create(
      "div",
      emptyCardClass,
      `当前没有活跃的 CUDA 计算进程 · ${processFreshness}`,
    );
    emptyCard.title = freshnessHint;
    elements.gpuTaskList.replaceChildren(emptyCard);
    return;
  }
  if (!matchingProcesses.length) {
    view.gpuTaskRowCache.clear();
    elements.gpuTaskList.setAttribute("role", "status");
    const filterDescription = view.gpuTaskIdentityFilter === "owned"
      ? "已归属" : view.gpuTaskIdentityFilter === "unowned" ? "未归属" : "当前";
    const emptyMessage = taskTerms.length
      ? `${filterDescription}范围内没有匹配“${view.gpuTaskQuery}”的程序`
      : `没有${filterDescription}的活跃程序`;
    const emptyCard = create("div", emptyCardClass, `${emptyMessage} · ${processFreshness}`);
    emptyCard.title = freshnessHint;
    elements.gpuTaskList.replaceChildren(emptyCard);
    return;
  }
  elements.gpuTaskList.setAttribute("role", "list");
  const seenKeys = new Set();
  const desiredRows = visibleProcesses.map((process) => {
    const key = `${process.pid}|${process.name || ""}`;
    seenKeys.add(key);
    let row = view.gpuTaskRowCache.get(key);
    if (!row) {
      row = gpuTaskRow();
      row.item.dataset.processKey = key;
      view.gpuTaskRowCache.set(key, row);
    }
    row.item.classList.toggle("search-target", key === view.selectedProcessKey);
    updateGpuTaskRow(row, process, gpu);
    return row.item;
  });
  [...view.gpuTaskRowCache.keys()].forEach((key) => {
    if (!seenKeys.has(key)) view.gpuTaskRowCache.delete(key);
  });
  reconcileChildren(elements.gpuTaskList, desiredRows);
}

function openGpuDetail(server, gpu, { processQuery = "", processKey = "" } = {}) {
  view.selectedGpu = {
    host: server.host,
    key: String(gpu.uuid || gpu.index),
  };
  view.gpuTaskQuery = String(processQuery).slice(0, MAX_SEARCH_QUERY_LENGTH);
  view.gpuTaskIdentityFilter = "all";
  view.selectedProcessKey = String(processKey);
  elements.gpuTaskSearch.value = view.gpuTaskQuery;
  view.gpuHistory = null;
  view.gpuHistoryKey = "";
  view.gpuHistoryRenderKey = "";
  view.gpuHistoryFetchKey = null;
  view.gpuHistoryError = false;
  view.gpuHistoryRetryDelayMs = 0;
  clearGpuHistoryRetry();
  view.gpuHistoryRequest += 1;
  openExclusiveDialog(elements.gpuDetailDialog);
  renderGpuDetail();
  const target = view.selectedProcessKey
    ? view.gpuTaskRowCache.get(view.selectedProcessKey)?.item : null;
  if (target) {
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: "nearest" });
  }
}

function gpuProcessCell(gpu) {
  const cell = document.createElement("td");
  cell.className = "gpu-col-process";
  const content = create("div", "gpu-process-cell");
  if (gpu.processes_available === false) {
    content.append(
      create("strong", "unavailable", "不可用"),
      create("small", "", "GPU 指标仍在更新"),
    );
    cell.append(content);
    return cell;
  }
  const summary = gpuProcessSummary(gpu);
  if (!summary.count) {
    content.append(create("strong", "idle", "0 个进程"), create("small", "", "无计算任务"));
    cell.append(content);
    return cell;
  }
  const topName = summary.topProcess ? gpuTaskDisplayName(summary.topProcess) : "未知程序";
  const sampleKind = gpu.processes_sampled === false ? "缓存" : "采样";
  const freshness = gpu.processes_observed_at ? age(gpu.processes_observed_at) : "时间未知";
  const memorySummary = summary.knownMemoryCount
    ? `${memory(summary.knownMemoryMiB)} 已知分配` : "进程显存未知";
  content.append(
    create("strong", "", `${summary.count} · ${topName}`),
    create("small", "", `${memorySummary} · ${sampleKind} ${freshness}`),
  );
  content.title = [
    `${summary.count} 个活跃计算进程`,
    `${summary.knownMemoryCount} 个返回显存，共 ${memory(summary.knownMemoryMiB)}`,
    `${summary.ownedCount} 个具有使用者归属`,
  ].join(" · ");
  cell.append(content);
  return cell;
}

function tableRow(record, grouped = false) {
  const { server, gpu } = record;
  const row = document.createElement("tr");
  row.dataset.host = server.host;
  row.dataset.gpuId = String(gpu.uuid || gpu.index);
  const deviceCell = document.createElement("td");
  const device = create("div", "device-cell");
  device.append(create("span", "gpu-index", String(gpu.index)));
  const deviceText = create("div", "device-text");
  deviceText.append(create("strong", "", grouped ? `GPU ${gpu.index}` : displayHost(server)));
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
  const details = create("button", "gpu-detail-trigger", "详情");
  details.type = "button";
  details.setAttribute("aria-label", `查看 ${server.host} GPU ${gpu.index} 的任务详情`);
  details.addEventListener("click", (event) => {
    event.stopPropagation();
    openGpuDetail(server, gpu);
  });
  statusCell.append(pill, details);
  row.append(
    deviceCell,
    modelCell,
    utilCell,
    memoryCell,
    temperatureCell,
    powerCell,
    gpuProcessCell(gpu),
    statusCell,
  );
  row.addEventListener("click", () => openGpuDetail(server, gpu));
  return row;
}

function filteredRecords() {
  const terms = normalizedSearchTerms(view.query);
  return allGpuRecords().filter(({ server, gpu }) => {
    if (view.selectedHost !== "all" && server.host !== view.selectedHost) return false;
    const utilization = numeric(gpu.utilization_gpu_pct);
    const temperature = numeric(gpu.temperature_c);
    const threshold = limits();
    if (view.filter === "busy" && utilization < threshold.gpu_busy_pct) return false;
    if (view.filter === "idle" && utilization >= threshold.gpu_busy_pct) return false;
    if (view.filter === "hot" && temperature < threshold.gpu_temperature_warning_c) return false;
    if (view.filter === "processes" && !gpuProcessSummary(gpu).count) return false;
    return gpuRecordMatchesSearch(server, gpu, terms);
  });
}

function gpuSortValue(gpu) {
  if (view.sort === "processes") return gpuProcessSummary(gpu).count;
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

const { buildCsv } = globalThis.MocopCsvExport.create({
  ratio,
  gpuProcessSummary,
  gpuState,
});

function exportVisibleCsv() {
  const records = visibleOrderedRecords();
  if (!records.length) return;
  const now = new Date();
  const date = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
  downloadBlob(
    new Blob([buildCsv(records)], { type: "text/csv;charset=utf-8" }),
    `gpu-monitor-${view.selectedHost}-${date}.csv`,
  );
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
  table.setAttribute("aria-label", grouped ? "主机 GPU 设备" : "GPU 设备");
  const head = document.createElement("thead");
  head.className = "sr-only";
  const headingRow = document.createElement("tr");
  ["设备", "型号 / 驱动", "GPU 负载", "显存", "温度", "功耗", "进程", "状态与操作"]
    .forEach((label) => headingRow.append(create("th", "", label)));
  head.append(headingRow);
  const body = document.createElement("tbody");
  body.replaceChildren(...records.map((record) => tableRow(record, grouped)));
  table.append(head, body);
  return table;
}

function groupMetric(label, value) {
  const metric = create("span", "gpu-group-metric");
  metric.append(create("small", "", label), create("strong", "", value));
  return metric;
}

// A status filter or search query temporarily expands every matching group
// without touching the operator's explicit expansion state.
function groupsFocused() {
  return view.filter !== "all" || view.query.trim() !== "";
}

function gpuGroup(group) {
  const { server, records } = group;
  const details = create("details", "gpu-server-group");
  details.open = groupsFocused() || view.expandedHosts.has(server.host);
  details.dataset.host = server.host;
  const summary = create("summary", "gpu-group-summary");
  const identity = create("span", "gpu-group-identity");
  const name = create("span", "gpu-group-name");
  name.append(
    create("strong", "", displayHost(server)),
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
    if (!groupsFocused()) {
      if (details.open) view.expandedHosts.add(server.host);
      else view.expandedHosts.delete(server.host);
    }
    updateGroupToggle();
  });
  return details;
}

function tableSignature(server, records) {
  const system = server.system;
  return JSON.stringify({
    host: `${server.host}\u0000${server.displayName || ""}`,
    incidentVersion: view.incidentVersion,
    filter: view.filter,
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
      processesAvailable: gpu.processes_available,
      processesSampled: gpu.processes_sampled,
      processesObservedAt: gpu.processes_observed_at,
      processSummary: gpuProcessSummarySignature(gpu),
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
  const focusedRow = document.activeElement?.closest("tr[data-gpu-id]");
  const focusedGpu = focusedRow
    ? [focusedRow.dataset.host, focusedRow.dataset.gpuId]
    : null;
  const records = filteredRecords();
  const selected = view.selectedHost === "all"
    ? null
    : view.snapshot.servers.find((server) => server.host === view.selectedHost);
  elements.inventoryTitle.textContent = selected
    ? displayHost(selected)
    : view.serverFilter === "all" ? "全局资源" : SERVER_FILTER_LABELS[view.serverFilter];
  elements.probeNow.hidden = !selected;
  elements.probeNow.disabled = !selected || selected.polling || view.manualProbePending;
  elements.probeNow.textContent = selected?.polling ? "正在探测" : "立即探测";
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
    const nodes = groups.map(cachedGpuGroup);
    reconcileChildren(elements.gpuGroups, nodes);
    // Reused nodes keep their DOM; only the expansion state follows the query.
    const focused = groupsFocused();
    nodes.forEach((node) => {
      const open = focused || view.expandedHosts.has(node.dataset.host);
      if (node.open !== open) node.open = open;
    });
  }
  if (focusedGpu && !document.activeElement?.isConnected) {
    [...elements.gpuGroups.querySelectorAll("tr[data-gpu-id]")]
      .find((row) => row.dataset.host === focusedGpu[0]
        && row.dataset.gpuId === focusedGpu[1])
      ?.querySelector(".gpu-detail-trigger")
      ?.focus({ preventScroll: true });
  }
  updateGroupToggle();
  elements.emptyState.hidden = records.length !== 0;
}

// Stable scalar key for the selected host: the single-host resource panel
// and node notice only need a rebuild when that host's data version moves,
// not on every fleet snapshot (avoids JSON.stringify over the full record).
function selectedHostPanelKey() {
  if (view.selectedHost === "all") return "";
  const server = view.snapshot.servers.find(
    (candidate) => candidate.host === view.selectedHost,
  );
  if (!server) return `${view.selectedHost}\u0000missing`;
  return [
    `${server.host}\u0000${server.displayName || ""}`, server.status, server.stale, server.polling,
    server.lastAttemptAt, server.lastSuccessAt, server.nextRetryAt,
    server.consecutiveFailures, server.message,
    view.incidentVersion,
  ].join("\u0000");
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
  const panelKey = selectedHostPanelKey();
  if (!panelKey || panelKey !== view.selectedPanelKey) {
    view.selectedPanelKey = panelKey;
    renderNodeNotice();
    renderResources();
  }
  renderProgramSearch();
  renderHeatmap();
  renderTrends();
  renderTable();
  renderGpuDetail();
  renderIncidentDetail();
  renderCapacityMatcher();
  renderCapacityWatchControls();
  renderCapacityWatchBanner();
  renderOwners();
  renderTopology();
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

function snapshotBackoffMs() {
  const streak = view.snapshotFailureStreak;
  return streak > 0 ? Math.min(30_000, 4_000 * 2 ** (streak - 1)) : 0;
}

async function fetchSnapshot() {
  if (view.snapshotFetchInFlight) return view.snapshotFetchInFlight;
  const request = (async () => {
    try {
      const response = await fetch("/api/snapshot");
      if (response.status === 403) {
        dashboardAuthentication.forget();
        requestDashboardAuthentication("访问令牌不正确或已失效，请重新输入");
        return false;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const snapshot = await response.json();
      view.snapshotFailureStreak = 0;
      // A page that loaded while the service was restarting recovers here:
      // establish the missing stream once and correct the stuck offline
      // badge, since the polling success path never reported liveness.
      if (view.dashboardStarted && !view.connectStarted) connect();
      if (view.transportKind === "offline") setConnection("delayed", "轮询同步");
      if (!acceptSnapshot(snapshot)) return true;
      view.lastEventAt = Date.now();
      normalizeSelection();
      settleCapacityWatch();
      render();
      syncHistory();
      syncIncidents();
      return true;
    } catch (_error) {
      view.snapshotFailureStreak += 1;
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
  if (view.incidentRetryTimer != null) {
    // A failed version waits for its single backoff timer; only a newer
    // target version justifies an immediate replacement fetch.
    if (targetVersion === view.incidentRetryVersion) return;
    clearTimeout(view.incidentRetryTimer);
    view.incidentRetryTimer = null;
    view.incidentRetryVersion = null;
  }
  view.incidentLoadingVersion = targetVersion;
  const request = ++view.incidentRequest;
  let failed = false;
  try {
    const response = await fetch("/api/incidents?limit=50");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const incidents = await response.json();
    if (request !== view.incidentRequest) return;
    acceptIncidents(incidents);
    view.incidentRetryDelayMs = 0;
    view.incidentSyncFailed = false;
    // The watch waits for incidents that match the snapshot revision, so
    // this is the moment a snapshot-triggered evaluation was deferred to.
    settleCapacityWatch();
    renderIncidents();
    renderAttention();
    renderServers();
    renderTable();
    renderGpuDetail();
    renderIncidentDetail();
    renderCapacityMatcher();
    renderCapacityWatchControls();
  } catch (_error) {
    // Current telemetry remains usable if the optional transition feed is unavailable.
    failed = request === view.incidentRequest;
  } finally {
    if (request === view.incidentRequest) {
      view.incidentLoadingVersion = null;
      if (failed) {
        view.incidentRetryDelayMs = Math.min(
          30_000,
          Math.max(4_000, view.incidentRetryDelayMs * 2),
        );
        view.incidentRetryVersion = targetVersion;
        view.incidentRetryTimer = setTimeout(() => {
          view.incidentRetryTimer = null;
          view.incidentRetryVersion = null;
          syncIncidents();
        }, view.incidentRetryDelayMs);
        // Surface the failure instead of silently freezing/emptying the
        // attention panel while the backoff retries.
        view.incidentSyncFailed = true;
        renderAttention();
      } else if (view.snapshot) {
        // Refetch immediately only when the target version moved while this
        // request was in flight and the response does not already cover it.
        const currentTarget = numeric(view.snapshot.incidentVersion, 0);
        if (currentTarget !== targetVersion && view.incidentVersion !== currentTarget) {
          syncIncidents();
        }
      }
    }
  }
}

function acceptStreamFrame(frame, markLive) {
  let eventName = "message";
  const data = [];
  frame.split("\n").forEach((line) => {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  });
  if (eventName === "heartbeat") {
    view.lastEventAt = Date.now();
    markLive();
    return;
  }
  if (eventName !== "snapshot") return;
  try {
    if (!acceptSnapshot(JSON.parse(data.join("\n")))) return;
    view.lastEventAt = Date.now();
    markLive();
    normalizeSelection();
    settleCapacityWatch();
    scheduleRender();
    syncIncidents();
  } catch (_error) {
    setConnection("offline", "数据异常");
  }
}

async function connectAuthenticatedStream() {
  while (view.dashboardStarted) {
    try {
      const response = await fetch("/api/events", {
        headers: { Accept: "text/event-stream" },
      });
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
      setConnection("live", "实时连接");
      // A healthy stream is as good as a successful poll: clear the failure
      // streak so a later brief drop does not inherit a stale backoff.
      view.snapshotFailureStreak = 0;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (view.dashboardStarted) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer = appendStreamChunk(
          buffer,
          decoder.decode(value, { stream: true }),
        );
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          acceptStreamFrame(frame, () => setConnection("live", "实时连接"));
          boundary = buffer.indexOf("\n\n");
        }
      }
    } catch (_error) {
      const reachable = await fetchSnapshot();
      if (view.authenticationFailed) {
        // A future re-authentication must be able to establish a new stream.
        view.connectStarted = false;
        return;
      }
      setConnection(
        reachable ? "delayed" : "offline",
        reachable ? "轮询同步" : "服务不可达",
      );
    }
    // Downtime uses the shared snapshot backoff so an unreachable service is
    // not hammered at a fixed rate, matching the auxiliary channels.
    if (view.dashboardStarted) await wait(Math.max(1200, snapshotBackoffMs()));
  }
}

function connect() {
  if (view.connectStarted) return;
  view.connectStarted = true;
  connectAuthenticatedStream();
  // Polling a reader route only starts once this document is authenticated,
  // which the accepted snapshot that led here proves. Starting the pill here
  // rather than in startDashboard also covers the recovery path, where the
  // first snapshot only arrives through the polling fallback.
  updatePill.start();
}

document.querySelectorAll(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".filter").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("active", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
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
  const query = elements.search.value.slice(0, MAX_SEARCH_QUERY_LENGTH);
  if (elements.search.value !== query) elements.search.value = query;
  view.query = query;
  renderSearchResults();
});

// Only the program-search panel and the GPU table depend on the query.
function renderSearchResults() {
  if (!view.snapshot) return;
  renderProgramSearch();
  renderTable();
}

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
    renderSearchResults();
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
  openExclusiveDialog(elements.settingsDialog);
  refreshInventory();
  fetchServiceCapability();
});

elements.topologyToggle.addEventListener("click", () => {
  openExclusiveDialog(elements.topologyDialog);
  renderTopology();
  if (!view.topology && !view.topologyLoading) fetchTopology();
});

elements.capacityToggle.addEventListener("click", () => {
  if (!view.snapshot) return;
  elements.capacityGpuCount.value = String(view.capacityRequest.gpuCount);
  elements.capacityVram.value = String(view.capacityRequest.minVramGiB);
  syncCapacityModels();
  elements.capacityModel.value = view.capacityRequest.model;
  openExclusiveDialog(elements.capacityDialog);
  renderCapacityMatcher();
  renderCapacityWatchControls();
});

elements.ownersToggle.addEventListener("click", () => {
  if (!view.snapshot) return;
  openExclusiveDialog(elements.ownersDialog);
  renderOwners();
  fetchOwnersUsage();
});

elements.ownersUsageHours.addEventListener("change", () => {
  const hours = Number.parseInt(elements.ownersUsageHours.value, 10);
  view.ownersUsageHours = Number.isFinite(hours) && hours >= 1 ? hours : 24;
  if (elements.ownersDialog.open) fetchOwnersUsage();
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

elements.capacityWatchToggle.addEventListener("click", () => {
  if (view.capacityWatch) {
    capacityWatch.clearWatch();
    view.capacityWatch = null;
    view.capacityWatchSatisfied = 0;
    view.capacityWatchBannerDismissed = false;
  } else {
    if (!elements.capacityForm.reportValidity()) return;
    const request = {
      gpuCount: Number(elements.capacityGpuCount.value),
      minVramGiB: Number(elements.capacityVram.value),
      model: elements.capacityModel.value,
    };
    const saved = capacityWatch.saveWatch(request);
    if (!saved) return;
    view.capacityWatch = saved;
    view.capacityRequest = { ...request };
    // Permission is requested inside the click gesture; a denied or ignored
    // prompt still leaves the in-page banner and title indicator working.
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
    evaluateCapacityWatch();
    renderCapacityMatcher();
  }
  renderCapacityWatchControls();
  renderCapacityWatchBanner();
});

elements.capacityWatchBannerOpen.addEventListener("click", () => {
  if (!elements.capacityDialog.open) elements.capacityToggle.click();
});

elements.capacityWatchBannerDismiss.addEventListener("click", () => {
  view.capacityWatchBannerDismissed = true;
  renderCapacityWatchBanner();
});

elements.capacityWatchBannerStop.addEventListener("click", () => {
  capacityWatch.clearWatch();
  view.capacityWatch = null;
  view.capacityWatchSatisfied = 0;
  view.capacityWatchBannerDismissed = false;
  renderCapacityWatchControls();
  renderCapacityWatchBanner();
});

elements.gpuDetailSsh.addEventListener("click", () => {
  const host = elements.gpuDetailSsh.dataset.host;
  if (host) copySshCommand(elements.gpuDetailSsh, host);
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
  view.gpuTaskQuery = "";
  view.gpuTaskIdentityFilter = "all";
  view.selectedProcessKey = "";
  elements.gpuTaskSearch.value = "";
  if (view.gpuTaskFeedbackTimer != null) clearTimeout(view.gpuTaskFeedbackTimer);
  view.gpuTaskFeedbackTimer = null;
  elements.gpuTaskFeedback.textContent = "";
  elements.gpuTaskFeedback.className = "gpu-task-feedback";
  // Nothing polls a closed dialog, and the cached rows / history DOM would
  // only keep stale nodes (and their per-second duration scans) alive.
  clearGpuHistoryRetry();
  view.gpuHistory = null;
  view.gpuHistoryKey = "";
  view.gpuHistoryRenderKey = "";
  view.gpuHistoryFetchKey = null;
  view.gpuHistoryError = false;
  view.gpuHistoryRetryDelayMs = 0;
  view.gpuTaskRowCache.clear();
  elements.gpuTaskList.replaceChildren();
  elements.gpuHistoryGrid.replaceChildren();
  elements.gpuProcessTimeline.replaceChildren();
});

// The sort toggle lives next to the task count badge; both move into a
// shared wrapper so the heading keeps its two-side flex layout.
const gpuTaskSortButtons = [
  ["memory", "按显存"],
  ["duration", "按时长"],
  ["name", "按名称"],
].map(
  ([value, label]) => {
    const button = create("button", "gpu-task-sort-choice", label);
    button.type = "button";
    button.dataset.taskSort = value;
    button.addEventListener("click", () => {
      if (preferences.gpuTaskSort === value) return;
      preferences.gpuTaskSort = value;
      savePreferences();
      renderGpuDetail();
    });
    return button;
  },
);

function syncGpuTaskSortButtons() {
  gpuTaskSortButtons.forEach((button) => {
    const active = button.dataset.taskSort === preferences.gpuTaskSort;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

{
  const sortGroup = create("div", "gpu-task-sort");
  sortGroup.setAttribute("role", "group");
  sortGroup.setAttribute("aria-label", "任务排序方式");
  sortGroup.append(...gpuTaskSortButtons);
  elements.gpuTaskHeadingTools.prepend(sortGroup);
  syncGpuTaskSortButtons();
}

elements.gpuTaskSearch.addEventListener("input", () => {
  const query = elements.gpuTaskSearch.value.slice(0, MAX_SEARCH_QUERY_LENGTH);
  if (elements.gpuTaskSearch.value !== query) elements.gpuTaskSearch.value = query;
  view.gpuTaskQuery = query;
  view.selectedProcessKey = "";
  renderGpuDetail();
});

elements.gpuTaskSearch.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !elements.gpuTaskSearch.value) return;
  event.preventDefault();
  elements.gpuTaskSearch.value = "";
  view.gpuTaskQuery = "";
  view.selectedProcessKey = "";
  renderGpuDetail();
});

document.querySelectorAll("[data-gpu-task-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    const filter = button.dataset.gpuTaskFilter;
    if (!["all", "owned", "unowned"].includes(filter)) return;
    view.gpuTaskIdentityFilter = filter;
    view.selectedProcessKey = "";
    renderGpuDetail();
  });
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

function bindAppearanceChoices(buttons, datasetKey, values, select) {
  buttons.forEach((button, index) => {
    button.addEventListener("click", () => {
      const value = button.dataset[datasetKey];
      if (!values.has(value)) return;
      select(value);
      syncPreferenceControls();
      savePreferences();
    });
    button.addEventListener("keydown", (event) => {
      const direction = {
        ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1,
      }[event.key];
      let targetIndex = direction == null ? null : index + direction;
      if (event.key === "Home") targetIndex = 0;
      if (event.key === "End") targetIndex = buttons.length - 1;
      if (targetIndex == null) return;
      event.preventDefault();
      const target = buttons[(targetIndex + buttons.length) % buttons.length];
      target.focus();
      target.click();
    });
  });
}

bindAppearanceChoices(
  styleChoiceButtons,
  "styleChoice",
  VISUAL_STYLE_VALUES,
  (value) => { preferences.visualStyle = value; },
);
bindAppearanceChoices(
  accentChoiceButtons,
  "accentChoice",
  ACCENT_VALUES,
  (value) => { preferences.accent = value; },
);

elements.resetPreferences.addEventListener("click", resetPreferences);
elements.restartService.addEventListener("click", () => {
  if (!view.serviceRestartSupported || view.serviceRestarting) return;
  elements.restartConfirmDialog.showModal();
});
elements.confirmRestartService.addEventListener("click", restartManagedService);
elements.notificationTest.addEventListener("click", testNotifications);
elements.exportDiagnostics.addEventListener("click", exportDiagnostics);
elements.probeNow.addEventListener("click", requestManualProbe);
elements.acknowledgeIncident.addEventListener(
  "click", () => updateIncidentAction("acknowledged"),
);
elements.silenceIncident.addEventListener(
  "click", () => updateIncidentAction("silenced"),
);
elements.clearIncidentAction.addEventListener(
  "click", () => updateIncidentAction("clear"),
);
elements.incidentOpenGpu.addEventListener("click", () => {
  const condition = selectedIncidentRecord();
  const server = view.snapshot?.servers.find((item) => item.host === condition?.host);
  const index = Number.parseInt(elements.incidentOpenGpu.dataset.gpuIndex, 10);
  const gpu = server?.gpus.find((item) => item.index === index);
  if (server && gpu) openGpuDetail(server, gpu);
});
elements.incidentOpenMaintenance.addEventListener("click", () => {
  const host = view.selectedIncident?.host;
  if (!host) return;
  // Pre-expand the maintenance editor for this host, then reuse the regular
  // settings entry point (it closes the incident dialog and rescans nodes).
  view.maintenanceEditingHost = host;
  view.maintenanceDraft = null;
  view.groupEditingHost = null;
  view.maintenanceFocusHost = host;
  elements.settingsToggle.click();
});
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
  if (!view.dashboardStarted) return;
  if (!document.hidden) {
    // Relative-time text is cosmetic; a hidden document repaints nothing,
    // but the staleness gate below must keep running to recover the stream.
    if (view.snapshot) {
      const lastSync = age(view.snapshot.lastPollCompletedAt);
      if (elements.lastSync.textContent !== lastSync) elements.lastSync.textContent = lastSync;
    }
    refreshRelativeTimes();
    renderConnectionStatus();
  }
  const elapsed = Date.now() - view.lastEventAt;
  const fallbackAfter = Math.max(2000, numeric(view.snapshot?.pollIntervalSeconds, 5) * 1000);
  // Consecutive failures widen the poll gate up to 30s so an outage is not
  // hammered at a fixed rate; one success resets the backoff immediately.
  const gate = Math.max(fallbackAfter, snapshotBackoffMs());
  if (view.transportKind !== "live" && elapsed > gate) fetchSnapshot();
  // 16s instead of 15s: named heartbeats arrive on a 15s cadence, so the
  // grace second keeps jitter from triggering a pointless snapshot poll on a
  // healthy stream. Services without named heartbeats still fall back here.
  else if (elapsed > Math.max(16000, snapshotBackoffMs())) fetchSnapshot();
}, 1000);

const requestDashboardAuthentication = dashboardAuthentication.bindPrompt({
  dialog: elements.authenticationDialog,
  form: elements.authenticationForm,
  input: elements.authenticationToken,
  submit: elements.authenticationSubmit,
  status: elements.authenticationStatus,
  authenticate: async () => {
    view.authenticationFailed = false;
    const started = await startDashboard();
    return { started, rejected: view.authenticationFailed };
  },
  onRequired: () => {
    view.authenticationFailed = true;
    view.dashboardStarted = false;
    setConnection("offline", "需要访问令牌");
  },
});

syncPreferenceControls();
renderInventory();

async function startDashboard() {
  loadStoredBackground();
  if (dashboardAuthentication.consumeInvalidFragment()) {
    dashboardAuthentication.forget();
    requestDashboardAuthentication("URL 中的访问令牌格式无效，请重新输入");
    return false;
  }
  // Every private route requires the capability, so a document without one
  // prompts immediately instead of spending a round trip to confirm that.
  if (!dashboardAuthentication.token) {
    requestDashboardAuthentication("请输入此 Mocop 实例的访问令牌");
    return false;
  }
  view.dashboardStarted = true;
  const snapshotLoaded = await fetchSnapshot();
  if (view.authenticationFailed || !snapshotLoaded) return false;
  fetchTopology();
  connect();
  return true;
}

const updatePill = globalThis.MocopUpdatePill
  .create({ button: $("#update-pill"), request: (path, options) => fetch(path, options) });

startDashboard();
