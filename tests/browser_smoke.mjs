import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { accessSync, constants } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function executable(candidates) {
  for (const candidate of candidates) {
    if (!candidate) continue;
    try {
      accessSync(candidate, constants.X_OK);
      return candidate;
    } catch {}
  }
  throw new Error(`No executable found: ${candidates.filter(Boolean).join(", ")}`);
}

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.equal(typeof address, "object");
  const port = address.port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitFor(url, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return response;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${url}: ${lastError || "not ready"}`);
}

function capture(process) {
  let output = "";
  for (const stream of [process.stdout, process.stderr]) {
    stream?.on("data", (chunk) => {
      output = (output + chunk.toString()).slice(-16_384);
    });
  }
  return () => output;
}

function signalProcess(process, signal) {
  if (!process || process.exitCode !== null || !process.pid) return;
  try {
    process.kill(-process.pid, signal);
  } catch {
    process.kill(signal);
  }
}

async function terminate(process) {
  if (!process || process.exitCode !== null || !process.pid) return;
  const exited = new Promise((resolve) => process.once("exit", resolve));
  signalProcess(process, "SIGTERM");
  const graceful = await Promise.race([
    exited.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 1_000)),
  ]);
  if (!graceful && process.exitCode === null) {
    signalProcess(process, "SIGKILL");
    await Promise.race([
      exited,
      new Promise((resolve) => setTimeout(resolve, 1_000)),
    ]);
  }
}

class CdpClient {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.waiters = new Map();
    this.errors = [];
    socket.addEventListener("message", ({ data }) => this.receive(JSON.parse(data)));
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    return new CdpClient(socket);
  }

  receive(message) {
    if (message.id) {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
      return;
    }
    if (message.method === "Runtime.exceptionThrown") {
      this.errors.push(message.params.exceptionDetails.text);
    }
    if (
      message.method === "Runtime.consoleAPICalled"
      && ["error", "assert"].includes(message.params.type)
    ) {
      this.errors.push(`console.${message.params.type}`);
    }
    const waiter = this.waiters.get(message.method);
    if (waiter) {
      this.waiters.delete(message.method);
      waiter.resolve(message.params);
    }
  }

  send(method, params = {}) {
    const id = this.nextId++;
    const promise = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.socket.send(JSON.stringify({ id, method, params }));
    return promise;
  }

  waitFor(method, timeoutMs = 10_000) {
    assert(!this.waiters.has(method), `Already waiting for ${method}`);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(method);
        reject(new Error(`Timed out waiting for CDP event ${method}`));
      }, timeoutMs);
      this.waiters.set(method, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
      });
    });
  }

  async evaluate(expression, awaitPromise = false) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text);
    }
    return result.result.value;
  }

  close() {
    this.socket.close();
  }
}

const temporary = await mkdtemp(path.join(os.tmpdir(), "mocop-browser-"));
let monitor;
let chrome;
let monitorOutput = () => "";
let chromeOutput = () => "";
let cdp;

try {
  const monitorPort = await freePort();
  const debugPort = await freePort();
  monitor = spawn(process.env.PYTHON || "python3", [
    "-m", "tests.browser_fixture", String(monitorPort),
  ], {
    cwd: projectRoot,
    detached: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  monitorOutput = capture(monitor);
  await waitFor(`http://127.0.0.1:${monitorPort}/healthz`);
  const warmupMs = Number(process.env.MOCOP_BROWSER_WARMUP_MS || 0);
  assert(Number.isInteger(warmupMs) && warmupMs >= 0 && warmupMs <= 30_000);
  if (warmupMs) {
    await new Promise((resolve) => setTimeout(resolve, warmupMs));
  }

  const chromePath = executable([
    process.env.CHROME_PATH,
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ]);
  chrome = spawn(chromePath, [
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    `--user-data-dir=${path.join(temporary, "chrome")}`,
    `--remote-debugging-port=${debugPort}`,
    "about:blank",
  ], { detached: true, stdio: ["ignore", "pipe", "pipe"] });
  chromeOutput = capture(chrome);
  await waitFor(`http://127.0.0.1:${debugPort}/json/version`);

  const targetResponse = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`http://127.0.0.1:${monitorPort}/`)}`,
    { method: "PUT" },
  );
  assert(targetResponse.ok, `Chrome target creation failed: ${targetResponse.status}`);
  const target = await targetResponse.json();
  cdp = await CdpClient.connect(target.webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
    source: `const NativeEventSource = window.EventSource;
    window.EventSource = class MocopObservedEventSource extends NativeEventSource {
      constructor(...arguments_) {
        super(...arguments_);
        window.__mocopEventSource = this;
      }
    };
    window.addEventListener("DOMContentLoaded", () => {
      const select = document.querySelector("#refresh-interval");
      select.value = "2";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      window.__mocopEarlyCadenceChange = true;
    });`,
  });
  const loaded = cdp.waitFor("Page.loadEventFired");
  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${monitorPort}/` });
  await loaded;

  const initial = await cdp.evaluate(`({
    title: document.title,
    heading: document.querySelector("h1")?.textContent,
    interval: document.querySelector("#refresh-interval")?.value,
    earlyCadenceChange: window.__mocopEarlyCadenceChange,
    gpuMemoryCard: Boolean(document.querySelector("#gpu-memory-card")),
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth
  })`);
  assert.equal(initial.title, "Mocop · AI-native GPU cluster monitor");
  assert.equal(initial.heading, "GPU 集群实时监控");
  assert.equal(initial.earlyCadenceChange, true);
  assert.equal(initial.gpuMemoryCard, true);
  assert.equal(initial.overflow, false);

  await new Promise((resolve) => setTimeout(resolve, 4_000));

  const final = await cdp.evaluate(`(async () => {
    const snapshot = await fetch("/api/snapshot").then((response) => response.json());
    return {
      selected: document.querySelector("#refresh-interval")?.value,
      interval: snapshot.pollIntervalSeconds,
      connection: document.querySelector("#connection")?.className,
      feedback: document.querySelector("#refresh-feedback")?.textContent,
      versionLabel: document.querySelector("#poll-info")?.textContent,
      serverRatio: document.querySelector("#server-ratio")?.textContent,
      totalGpus: document.querySelector("#total-gpus")?.textContent,
      gpuGroups: document.querySelectorAll("details.gpu-server-group").length,
      expandedGroups: document.querySelectorAll("details.gpu-server-group[open]").length,
      heatmapVisible: !document.querySelector("#gpu-heatmap")?.hidden,
      attentionVisible: !document.querySelector("#attention-panel")?.hidden,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth
    };
  })()`, true);
  assert.equal(final.interval, 2);
  assert.equal(final.selected, "2");
  assert.match(final.connection, /live/);
  assert.match(final.feedback, /2/);
  assert.match(final.versionLabel, /v0\.8\.0/);
  assert.equal(final.serverRatio, "2 / 3");
  assert.equal(final.totalGpus, "8");
  assert.equal(final.gpuGroups, 2);
  assert.equal(final.expandedGroups, 0);
  assert.equal(final.heatmapVisible, true);
  assert.equal(final.attentionVisible, true);
  assert.equal(final.overflow, false);

  const transientConnection = await cdp.evaluate(`(async () => {
    window.__mocopEventSource.dispatchEvent(new Event("error"));
    const immediate = document.querySelector("#connection-text")?.textContent;
    await new Promise((resolve) => setTimeout(resolve, 1600));
    return {
      immediate,
      settled: document.querySelector("#connection-text")?.textContent,
      className: document.querySelector("#connection")?.className,
    };
  })()`, true);
  assert.notEqual(transientConnection.immediate, "正在重连");
  assert.match(transientConnection.className, /live/);

  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await new Promise((resolve) => setTimeout(resolve, 200));

  const personalization = await cdp.evaluate(`(async () => {
    const serverItems = [...document.querySelectorAll(".server-item[data-host]")];
    const utilizationVisible = serverItems.every(
      (item) => item.textContent.includes("GPU") && item.textContent.includes("CPU"),
    );
    const source = serverItems[1];
    const target = serverItems[0];
    const transfer = new DataTransfer();
    source.dispatchEvent(new DragEvent("dragstart", { bubbles: true, dataTransfer: transfer }));
    target.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: transfer }));
    target.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer }));
    source.dispatchEvent(new DragEvent("dragend", { bubbles: true, dataTransfer: transfer }));
    const reordered = [...document.querySelectorAll(".server-item[data-host]")].map(
      (item) => item.dataset.host,
    );

    document.querySelector("#settings-toggle").click();
    for (let attempt = 0; attempt < 20 && document.querySelector("#configured-host-count").textContent !== "3"; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const settingsDialog = document.querySelector("#settings-dialog");
    const settingsRect = settingsDialog.getBoundingClientRect();
    const settingsColumns = getComputedStyle(
      document.querySelector(".settings-sections")
    ).gridTemplateColumns.split(" ").length;
    document.querySelector('[data-theme-choice="terminal"]').click();
    const terminalCardStyle = getComputedStyle(document.querySelector(".metric-card"));
    const terminalStyle = {
      radius: terminalCardStyle.borderRadius,
      shadow: terminalCardStyle.boxShadow,
      font: getComputedStyle(document.body).fontFamily,
    };
    const auroraTheme = document.querySelector('[data-theme-choice="aurora"]');
    auroraTheme.focus();
    auroraTheme.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    const themeKeyboardFocus = document.activeElement?.dataset.themeChoice;
    const glassCardStyle = getComputedStyle(document.querySelector(".metric-card"));
    const glassStyle = {
      radius: glassCardStyle.borderRadius,
      blur: glassCardStyle.backdropFilter,
    };
    const backgroundInput = document.querySelector("#background-image-input");
    const rejectedTransfer = new DataTransfer();
    rejectedTransfer.items.add(new File(
      ['<svg xmlns="http://www.w3.org/2000/svg"></svg>'],
      "unsafe.svg",
      { type: "image/svg+xml" },
    ));
    backgroundInput.files = rejectedTransfer.files;
    backgroundInput.dispatchEvent(new Event("change", { bubbles: true }));
    for (let attempt = 0; attempt < 20 && !document.querySelector("#background-image-status").classList.contains("error"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const rejectedBackground = document.querySelector("#background-image-status").textContent;
    const spoofedTransfer = new DataTransfer();
    spoofedTransfer.items.add(new File(["not a png"], "spoofed.png", { type: "image/png" }));
    backgroundInput.files = spoofedTransfer.files;
    backgroundInput.dispatchEvent(new Event("change", { bubbles: true }));
    for (let attempt = 0; attempt < 20 && !document.querySelector("#background-image-status").classList.contains("error"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const rejectedSpoofed = document.querySelector("#background-image-status").textContent;
    const animatedTransfer = new DataTransfer();
    animatedTransfer.items.add(new File([
      new Uint8Array([
        137, 80, 78, 71, 13, 10, 26, 10,
        0, 0, 0, 0, 97, 99, 84, 76, 0, 0, 0, 0,
      ]),
    ], "animated.png", { type: "image/png" }));
    backgroundInput.files = animatedTransfer.files;
    backgroundInput.dispatchEvent(new Event("change", { bubbles: true }));
    for (let attempt = 0; attempt < 20 && !document.querySelector("#background-image-status").classList.contains("error"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const rejectedAnimation = document.querySelector("#background-image-status").textContent;
    const oversizedTransfer = new DataTransfer();
    oversizedTransfer.items.add(new File(
      [new Uint8Array(8 * 1024 * 1024 + 1)],
      "oversized.png",
      { type: "image/png" },
    ));
    backgroundInput.files = oversizedTransfer.files;
    backgroundInput.dispatchEvent(new Event("change", { bubbles: true }));
    for (let attempt = 0; attempt < 20 && !document.querySelector("#background-image-status").classList.contains("error"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const rejectedOversized = document.querySelector("#background-image-status").textContent;
    const wideCanvas = document.createElement("canvas");
    wideCanvas.width = 8193;
    wideCanvas.height = 1;
    const wideBlob = await new Promise((resolve) => wideCanvas.toBlob(resolve, "image/png"));
    const wideTransfer = new DataTransfer();
    wideTransfer.items.add(new File([wideBlob], "wide.png", { type: "image/png" }));
    backgroundInput.files = wideTransfer.files;
    backgroundInput.dispatchEvent(new Event("change", { bubbles: true }));
    for (let attempt = 0; attempt < 40 && !document.querySelector("#background-image-status").classList.contains("error"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const rejectedDimensions = document.querySelector("#background-image-status").textContent;
    const canvas = document.createElement("canvas");
    canvas.width = 4;
    canvas.height = 3;
    const context = canvas.getContext("2d");
    context.fillStyle = "#17304a";
    context.fillRect(0, 0, canvas.width, canvas.height);
    const backgroundBlob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    const acceptedTransfer = new DataTransfer();
    acceptedTransfer.items.add(new File([backgroundBlob], "background.png", { type: "image/png" }));
    backgroundInput.files = acceptedTransfer.files;
    backgroundInput.dispatchEvent(new Event("change", { bubbles: true }));
    for (let attempt = 0; attempt < 40 && !document.querySelector("#background-image-status").classList.contains("success"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const visibility = document.querySelector("#background-visibility");
    visibility.value = "52";
    visibility.dispatchEvent(new Event("input", { bubbles: true }));
    const density = document.querySelector("#interface-density");
    density.value = "compact";
    density.dispatchEvent(new Event("change", { bubbles: true }));
    const serverFilter = document.querySelector("#default-server-filter");
    serverFilter.value = "busy";
    serverFilter.dispatchEvent(new Event("change", { bubbles: true }));
    document.querySelector("#settings-probe-timeout").value = "24";
    document.querySelector("#settings-probe-timeout").dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#settings-max-workers").value = "6";
    document.querySelector("#settings-max-workers").dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#collector-settings-form").requestSubmit();
    for (let attempt = 0; attempt < 20 && !document.querySelector("#collector-settings-status").classList.contains("success"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    document.querySelector("#configured-host-list .maintenance-action").click();
    const maintenanceEditor = document.querySelector("#configured-host-list .maintenance-editor");
    maintenanceEditor.querySelector('input[type="text"]').value = "Driver upgrade";
    maintenanceEditor.requestSubmit();
    for (let attempt = 0; attempt < 20 && !document.querySelector("#configured-host-list .maintenance-badge"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const maintenanceBadge = document.querySelector("#configured-host-list .maintenance-badge")?.textContent;
    document.querySelector("#available-host-list .inventory-host-action").click();
    for (let attempt = 0; attempt < 20 && document.querySelector("#configured-host-count").textContent !== "4"; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const gpuSort = document.querySelector("#settings-gpu-sort");
    gpuSort.value = "memory";
    gpuSort.dispatchEvent(new Event("change", { bubbles: true }));
    const power = document.querySelector("#show-power");
    power.checked = false;
    power.dispatchEvent(new Event("change", { bubbles: true }));
    const settingsOpen = document.querySelector("#settings-dialog").open;
    const persistedCollector = await fetch("/api/inventory", {
      headers: { "X-Monitor-Request": "dashboard" },
    }).then((response) => response.json());
    document.querySelector('[data-close-dialog="settings-dialog"]').click();

    document.querySelector(".gpu-table-body tbody tr").click();
    const taskDialog = document.querySelector("#gpu-detail-dialog");
    const result = {
      utilizationVisible,
      reordered,
      savedServerSort: JSON.parse(localStorage.getItem("mocop.preferences.v1")).serverSort,
      savedTheme: JSON.parse(localStorage.getItem("mocop.preferences.v1")).theme,
      savedDensity: JSON.parse(localStorage.getItem("mocop.preferences.v1")).density,
      savedBackgroundVisibility: JSON.parse(localStorage.getItem("mocop.preferences.v1")).backgroundVisibility,
      savedServerFilter: JSON.parse(localStorage.getItem("mocop.preferences.v1")).serverFilter,
      activeTheme: document.documentElement.dataset.theme,
      activeDensity: document.documentElement.dataset.density,
      backgroundActive: document.documentElement.dataset.background,
      backgroundStatus: document.querySelector("#background-image-status").textContent,
      backgroundRemoveEnabled: !document.querySelector("#remove-background-image").disabled,
      backgroundOpacity: getComputedStyle(document.documentElement).getPropertyValue("--custom-background-opacity").trim(),
      rejectedBackground,
      rejectedSpoofed,
      rejectedAnimation,
      rejectedOversized,
      rejectedDimensions,
      terminalStyle,
      glassStyle,
      themeKeyboardFocus,
      settingsCenterDelta: Math.abs(
        (settingsRect.left + settingsRect.right) / 2
          - document.documentElement.clientWidth / 2
      ),
      settingsColumns,
      configuredHosts: document.querySelector("#configured-host-count").textContent,
      inventoryStatus: document.querySelector("#inventory-status").textContent,
      settingsOpen,
      collectorSettings: persistedCollector.collectorSettings,
      maintenanceWindows: persistedCollector.maintenanceWindows,
      maintenanceBadge,
      gpuSort: document.querySelector("#gpu-sort").value,
      powerHidden: document.body.classList.contains("hide-gpu-power"),
      taskDialogOpen: taskDialog.open,
      taskCount: document.querySelector("#gpu-task-count").textContent,
      taskNames: document.querySelector("#gpu-task-list").textContent,
      healthMetrics: document.querySelector("#gpu-detail-metrics").textContent,
      heatmapLegend: Boolean(document.querySelector(".heatmap-legend")),
    };
    taskDialog.close();
    return result;
  })()`, true);
  assert.equal(personalization.utilizationVisible, true);
  assert.equal(personalization.reordered[0], "atlas-02");
  assert.equal(personalization.savedServerSort, "custom");
  assert.equal(personalization.savedTheme, "glass");
  assert.equal(personalization.savedDensity, "compact");
  assert.equal(personalization.savedBackgroundVisibility, 52);
  assert.equal(personalization.savedServerFilter, "busy");
  assert.equal(personalization.activeTheme, "glass");
  assert.equal(personalization.activeDensity, "compact");
  assert.equal(personalization.backgroundActive, "custom");
  assert.match(personalization.backgroundStatus, /4 × 3/);
  assert.equal(personalization.backgroundRemoveEnabled, true);
  assert.equal(personalization.backgroundOpacity, "0.52");
  assert.match(personalization.rejectedBackground, /PNG/);
  assert.match(personalization.rejectedSpoofed, /文件格式不匹配/);
  assert.match(personalization.rejectedAnimation, /动态图片/);
  assert.match(personalization.rejectedOversized, /8 MiB/);
  assert.match(personalization.rejectedDimensions, /8192/);
  assert.equal(personalization.terminalStyle.radius, "2px");
  assert.equal(personalization.terminalStyle.shadow, "none");
  assert.match(personalization.terminalStyle.font, /Mono/);
  assert.equal(personalization.glassStyle.radius, "22px");
  assert.match(personalization.glassStyle.blur, /20px/);
  assert.equal(personalization.themeKeyboardFocus, "glass");
  assert(personalization.settingsCenterDelta < 2);
  assert.equal(personalization.settingsColumns, 2);
  assert.equal(personalization.configuredHosts, "4");
  assert.match(personalization.inventoryStatus, /atlas-04/);
  assert.equal(personalization.settingsOpen, true);
  assert.equal(personalization.collectorSettings.pollIntervalSeconds, 2);
  assert.equal(personalization.collectorSettings.probeTimeoutSeconds, 24);
  assert.equal(personalization.collectorSettings.maxWorkers, 6);
  assert.equal(personalization.maintenanceWindows["atlas-01"].reason, "Driver upgrade");
  assert.match(personalization.maintenanceBadge, /维护至/);
  assert.equal(personalization.gpuSort, "memory");
  assert.equal(personalization.powerHidden, true);
  assert.equal(personalization.taskDialogOpen, true);
  assert.equal(personalization.taskCount, "2");
  assert.match(personalization.taskNames, /train\.py/);
  assert.match(personalization.healthMetrics, /硬件健康正常/);
  assert.equal(personalization.heatmapLegend, false);

  const reloaded = cdp.waitFor("Page.loadEventFired");
  await cdp.send("Page.reload");
  await reloaded;
  await new Promise((resolve) => setTimeout(resolve, 300));
  const persistedAppearance = await cdp.evaluate(`(async () => {
    for (let attempt = 0; attempt < 40 && document.documentElement.dataset.background !== "custom"; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    return {
      theme: document.documentElement.dataset.theme,
      density: document.documentElement.dataset.density,
      background: document.documentElement.dataset.background,
      visibility: document.querySelector("#background-visibility").value,
      removeEnabled: !document.querySelector("#remove-background-image").disabled,
    };
  })()`, true);
  assert.deepEqual(persistedAppearance, {
    theme: "glass",
    density: "compact",
    background: "custom",
    visibility: "52",
    removeEnabled: true,
  });

  if (process.env.MOCOP_SCREENSHOT_PATH) {
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await new Promise((resolve) => setTimeout(resolve, 200));
    const screenshot = await cdp.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: false,
    });
    await writeFile(
      path.resolve(projectRoot, process.env.MOCOP_SCREENSHOT_PATH),
      Buffer.from(screenshot.data, "base64"),
    );
  }

  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  await new Promise((resolve) => setTimeout(resolve, 200));
  const mobile = await cdp.evaluate(`(() => {
    document.querySelector("#settings-toggle").click();
    const rect = document.querySelector("#settings-dialog").getBoundingClientRect();
    return {
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      gpuMemoryWidth: document.querySelector("#gpu-memory-card")?.getBoundingClientRect().width,
      gridWidth: document.querySelector(".metrics-grid")?.getBoundingClientRect().width,
      settingsCenterDelta: Math.abs(
        (rect.left + rect.right) / 2 - document.documentElement.clientWidth / 2
      ),
    };
  })()`);
  assert.equal(mobile.overflow, false);
  assert(mobile.gpuMemoryWidth > mobile.gridWidth * 0.9);
  assert(mobile.settingsCenterDelta < 2);
  const removedBackground = await cdp.evaluate(`(async () => {
    document.querySelector("#remove-background-image").click();
    for (let attempt = 0; attempt < 40 && document.documentElement.dataset.background === "custom"; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    return {
      background: document.documentElement.dataset.background || "",
      removeDisabled: document.querySelector("#remove-background-image").disabled,
      status: document.querySelector("#background-image-status").textContent,
    };
  })()`, true);
  assert.deepEqual(removedBackground, {
    background: "",
    removeDisabled: true,
    status: "背景已从当前浏览器移除",
  });
  assert.deepEqual(cdp.errors, []);

  console.log(JSON.stringify({
    browser: "chrome", initial, final, transientConnection, personalization,
    persistedAppearance, mobile, removedBackground,
  }));
} catch (error) {
  console.error(error);
  if (monitorOutput()) console.error(`monitor output:\n${monitorOutput()}`);
  if (chromeOutput()) console.error(`chrome output:\n${chromeOutput()}`);
  process.exitCode = 1;
} finally {
  cdp?.close();
  await Promise.all([terminate(chrome), terminate(monitor)]);
  await rm(temporary, {
    recursive: true,
    force: true,
    maxRetries: 5,
    retryDelay: 100,
  });
}
