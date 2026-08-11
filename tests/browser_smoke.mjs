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
  // Shared CI runners can take longer than the monitor warmup to launch Chrome.
  await waitFor(`http://127.0.0.1:${debugPort}/json/version`, 30_000);

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
      persistenceStatus: document.querySelector("#persistence-status")?.textContent,
      notificationStatus: document.querySelector("#notification-status")?.textContent,
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
  assert.match(final.persistenceStatus, /仅内存/);
  assert.equal(final.notificationStatus, "未配置");
  assert.equal(final.gpuGroups, 2);
  assert.equal(final.expandedGroups, 0);
  assert.equal(final.heatmapVisible, true);
  assert.equal(final.attentionVisible, true);
  assert.equal(final.overflow, false);

  const screenshotPath = process.env.MOCOP_BROWSER_SCREENSHOT;
  if (screenshotPath) {
    const screenshot = await cdp.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: false,
    });
    await writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));
  }

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

  const topology = await cdp.evaluate(`(async () => {
    for (let attempt = 0; attempt < 40 && !document.querySelector("#topology-toggle"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    document.querySelector("#topology-toggle").click();
    for (let attempt = 0; attempt < 40 && document.querySelectorAll(".topology-node").length < 5; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const dialog = document.querySelector("#topology-dialog");
    const rect = dialog.getBoundingClientRect();
    const initialNodes = [...document.querySelectorAll(".topology-node")];
    renderTopology();
    const renderedNodes = [...document.querySelectorAll(".topology-node")];
    const result = {
      open: dialog.open,
      nodes: [...document.querySelectorAll(".topology-node strong")].map((node) => node.textContent),
      links: [...document.querySelectorAll(".topology-link-label")].map((node) => node.textContent),
      frpCount: document.querySelector("#topology-frp-count").textContent,
      live: document.querySelector("#topology-live-summary").textContent,
      offline: document.querySelectorAll(".topology-node.offline").length,
      infrastructure: document.querySelectorAll(".topology-node.infrastructure:disabled").length,
      monitored: document.querySelectorAll(".topology-node:not(:disabled)").length,
      infrastructureText: [...document.querySelectorAll(".topology-node.infrastructure .topology-node-meta")].map((node) => node.textContent),
      semanticLists: document.querySelectorAll("#topology-tree ul").length,
      accessibleLinks: [...document.querySelectorAll(".topology-connector")].every((node) => Boolean(node.getAttribute("aria-label"))),
      semanticLinks: [...document.querySelectorAll(".topology-connector")].every((node) => node.getAttribute("role") === "note"),
      linkFontSize: parseFloat(getComputedStyle(document.querySelector(".topology-link-label")).fontSize),
      reused: initialNodes.length === renderedNodes.length && initialNodes.every((node, index) => node === renderedNodes[index]),
      unmappedHidden: document.querySelector("#topology-unmapped").hidden,
      centerDelta: Math.abs((rect.left + rect.right) / 2 - document.documentElement.clientWidth / 2),
    };
    return result;
  })()`, true);
  assert.equal(topology.open, true);
  assert.deepEqual(
    topology.nodes,
    ["monitor-console", "atlas-gateway", "atlas-01", "atlas-02", "atlas-03"],
  );
  assert.deepEqual(topology.links, ["STCP · 7005", "SSH", "SSH", "SSH"]);
  assert.equal(topology.frpCount, "1");
  assert.match(topology.live, /2 \/ 3/);
  assert.equal(topology.offline, 1);
  assert.equal(topology.infrastructure, 2);
  assert.equal(topology.monitored, 3);
  assert.deepEqual(
    topology.infrastructureText,
    ["连接路径节点 · 不采集资源", "连接路径节点 · 不采集资源"],
  );
  assert(topology.semanticLists >= 3);
  assert.equal(topology.accessibleLinks, true);
  assert.equal(topology.semanticLinks, true);
  assert(topology.linkFontSize >= 10);
  assert.equal(topology.reused, true);
  assert.equal(topology.unmappedHidden, true);
  assert(topology.centerDelta < 2);
  let topologyBenchmark = null;
  if (process.env.MOCOP_TOPOLOGY_BENCHMARK === "1") {
    topologyBenchmark = await cdp.evaluate(`(() => {
      const originalTopology = view.topology;
      const benchmarkTopology = normalizeTopology({
        root: "benchmark-root",
        links: Array.from({ length: 512 }, (_, index) => ({
          source: "benchmark-root",
          target: \`benchmark-node-\${index}\`,
          transport: "ssh",
        })),
      });
      view.topology = benchmarkTopology;
      view.topologyRevision += 1;
      renderTopology();
      void document.querySelector("#topology-tree").offsetHeight;
      const rebuild = [];
      const cached = [];
      const sample = (forceRebuild) => {
        if (forceRebuild) view.topologyRenderedRevision = -1;
        const started = performance.now();
        renderTopology();
        void document.querySelector("#topology-tree").offsetHeight;
        return performance.now() - started;
      };
      for (let index = 0; index < 3; index += 1) {
        sample(true);
        sample(false);
      }
      for (let index = 0; index < 20; index += 1) {
        rebuild.push(sample(true));
        cached.push(sample(false));
      }
      const summary = (samples) => {
        const ordered = [...samples].sort((left, right) => left - right);
        const meanMs = samples.reduce((total, value) => total + value, 0) / samples.length;
        const variance = samples.reduce(
          (total, value) => total + (value - meanMs) ** 2,
          0,
        ) / samples.length;
        return {
          meanMs,
          medianMs: ordered[Math.floor(ordered.length / 2)],
          p95Ms: ordered[Math.ceil(ordered.length * 0.95) - 1],
          maxMs: ordered.at(-1),
          stdevMs: Math.sqrt(variance),
        };
      };
      const result = {
        nodes: document.querySelectorAll(".topology-node").length,
        runs: rebuild.length,
        rebuild: summary(rebuild),
        cached: summary(cached),
      };
      view.topology = originalTopology;
      view.topologyRevision += 1;
      renderTopology();
      return result;
    })()`);
    assert.equal(topologyBenchmark.nodes, 513);
    assert.equal(topologyBenchmark.runs, 20);
  }
  if (process.env.MOCOP_TOPOLOGY_SCREENSHOT_PATH) {
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await new Promise((resolve) => setTimeout(resolve, 150));
    const screenshot = await cdp.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: false,
    });
    await writeFile(
      path.resolve(projectRoot, process.env.MOCOP_TOPOLOGY_SCREENSHOT_PATH),
      Buffer.from(screenshot.data, "base64"),
    );
  }
  await cdp.evaluate('document.querySelector("#topology-dialog").close()');

  const capacity = await cdp.evaluate(`(() => {
    document.querySelector("#capacity-toggle").click();
    const initialMatches = document.querySelectorAll("#capacity-results .capacity-candidate.match").length;
    document.querySelector("#capacity-gpu-count").value = "2";
    document.querySelector("#capacity-vram").value = "60";
    document.querySelector("#capacity-form").requestSubmit();
    const dialog = document.querySelector("#capacity-dialog");
    const rect = dialog.getBoundingClientRect();
    const result = {
      open: dialog.open,
      initialMatches,
      matches: document.querySelectorAll("#capacity-results .capacity-candidate.match").length,
      firstHost: document.querySelector("#capacity-results .capacity-candidate.match strong")?.textContent,
      summary: document.querySelector("#capacity-summary")?.textContent,
      rule: document.querySelector("#capacity-rule")?.textContent,
      centerDelta: Math.abs((rect.left + rect.right) / 2 - document.documentElement.clientWidth / 2),
    };
    dialog.close();
    return result;
  })()`);
  assert.equal(capacity.open, true);
  assert.equal(capacity.initialMatches, 2);
  assert.equal(capacity.matches, 1);
  assert.equal(capacity.firstHost, "atlas-02");
  assert.match(capacity.summary, /1 个节点/);
  assert.match(capacity.rule, /60 GiB/);
  assert(capacity.centerDelta < 2);

  const owners = await cdp.evaluate(`(() => {
    document.querySelector("#owners-toggle").click();
    const dialog = document.querySelector("#owners-dialog");
    const rows = [...document.querySelectorAll("#owners-results .capacity-candidate")];
    const rect = dialog.getBoundingClientRect();
    const result = {
      open: dialog.open,
      rows: rows.length,
      ownerNames: rows.map((row) => row.querySelector("strong")?.textContent),
      firstVram: rows[0]?.querySelector("em")?.textContent,
      summary: document.querySelector("#owners-summary")?.textContent,
      centerDelta: Math.abs((rect.left + rect.right) / 2 - document.documentElement.clientWidth / 2),
    };
    dialog.close();
    return result;
  })()`);
  assert.equal(owners.open, true);
  assert(owners.rows >= 1, "owners view lists at least one attribution");
  assert(owners.ownerNames.includes("researcher"), "slurm owner is aggregated");
  assert.match(owners.firstVram, /GiB/);
  assert(owners.centerDelta < 2);

  const grouping = await cdp.evaluate(`(() => {
    const sort = document.querySelector("#server-sort");
    sort.value = "group";
    sort.dispatchEvent(new Event("change", { bubbles: true }));
    return {
      headings: [...document.querySelectorAll(".fleet-group-heading strong")].map(
        (item) => item.textContent,
      ),
      groups: [...document.querySelectorAll(".server-group-badge")].map(
        (item) => item.textContent,
      ),
      order: [...document.querySelectorAll(".server-item[data-host]")].map(
        (item) => item.dataset.host,
      ),
    };
  })()`);
  assert.deepEqual(grouping.headings, ["Lab", "Training"]);
  assert.deepEqual(grouping.groups, ["Lab", "Training", "Training"]);
  assert.deepEqual(grouping.order, ["atlas-03", "atlas-01", "atlas-02"]);

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
    const source = serverItems.find((item) => item.dataset.host === "atlas-02");
    const target = serverItems.find((item) => item.dataset.host === "atlas-01");
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
    for (let attempt = 0; attempt < 20 && document.querySelector("#service-restart-status").textContent.includes("正在确认"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const settingsDialog = document.querySelector("#settings-dialog");
    const settingsRect = settingsDialog.getBoundingClientRect();
    const settingsColumns = getComputedStyle(
      document.querySelector(".settings-sections")
    ).gridTemplateColumns.split(" ").length;
    document.querySelector('[data-style-choice="terminal"]').click();
    const terminalCardStyle = getComputedStyle(document.querySelector(".metric-card"));
    const terminalStyle = {
      radius: terminalCardStyle.borderRadius,
      shadow: terminalCardStyle.boxShadow,
      font: getComputedStyle(document.body).fontFamily,
    };
    document.querySelector('[data-style-choice="ledger"]').click();
    const ledgerStyle = {
      borderLeft: getComputedStyle(document.querySelector(".metric-card")).borderLeftWidth,
      radius: getComputedStyle(document.querySelector(".metric-card")).borderRadius,
      headingFont: getComputedStyle(document.querySelector("h1")).fontFamily,
      columns: getComputedStyle(document.querySelector(".metrics-grid"))
        .gridTemplateColumns.split(" ").length,
      scheme: getComputedStyle(document.documentElement).colorScheme,
      workspaceColumns: getComputedStyle(document.querySelector(".workspace"))
        .gridTemplateColumns.split(" ").length,
    };
    document.querySelector('[data-style-choice="blueprint"]').click();
    const blueprintCardStyle = getComputedStyle(document.querySelector(".metric-card"));
    const blueprintStyle = {
      radius: blueprintCardStyle.borderRadius,
      shadow: blueprintCardStyle.boxShadow,
      headingTransform: getComputedStyle(document.querySelector("h1")).textTransform,
    };
    document.querySelector('[data-style-choice="studio"]').click();
    const studioFleetRect = document.querySelector(".fleet-panel").getBoundingClientRect();
    const studioInventoryRect = document.querySelector(".inventory-panel").getBoundingClientRect();
    const studioStyle = {
      radius: getComputedStyle(document.querySelector(".metric-card")).borderRadius,
      columns: getComputedStyle(document.querySelector(".metrics-grid"))
        .gridTemplateColumns.split(" ").length,
      scheme: getComputedStyle(document.documentElement).colorScheme,
      fleetOnRight: studioFleetRect.left > studioInventoryRect.left,
    };
    const precisionStyle = document.querySelector('[data-style-choice="precision"]');
    precisionStyle.click();
    const precisionAppearance = {
      radius: getComputedStyle(document.querySelector(".metric-card")).borderRadius,
      columns: getComputedStyle(document.querySelector(".metrics-grid"))
        .gridTemplateColumns.split(" ").length,
    };
    const paletteSnapshot = () => {
      const rootStyle = getComputedStyle(document.documentElement);
      const bodyStyle = getComputedStyle(document.body);
      const cardStyle = getComputedStyle(document.querySelector(".metric-card:not(.primary)"));
      const panelStyle = getComputedStyle(document.querySelector(".panel"));
      return [
        rootStyle.backgroundColor,
        bodyStyle.backgroundImage,
        cardStyle.backgroundImage,
        cardStyle.borderTopColor,
        panelStyle.backgroundColor,
        panelStyle.borderTopColor,
      ];
    };
    document.querySelector('[data-accent-choice="cobalt"]').click();
    const cobaltPalette = paletteSnapshot();
    document.querySelector('[data-accent-choice="rose"]').click();
    const rosePalette = paletteSnapshot();
    const themePaletteDelta = cobaltPalette.filter(
      (value, index) => value !== rosePalette[index],
    ).length;
    const styleNames = ["precision", "glass", "terminal", "ledger", "blueprint", "studio"];
    const roundedSurfaceSelectors = [
      ".logo",
      ".metric-card",
      ".panel",
      ".gpu-server-group",
      ".style-choice",
    ];
    const styleCornerRadii = Object.fromEntries(styleNames.map((style) => {
      document.documentElement.dataset.style = style;
      return [style, roundedSurfaceSelectors.map((selector) => (
        Number.parseFloat(getComputedStyle(document.querySelector(selector)).borderRadius)
      ))];
    }));
    const themeDeltas = styleNames.map((style) => {
      document.documentElement.dataset.style = style;
      document.documentElement.dataset.accent = "cobalt";
      const cobalt = paletteSnapshot();
      document.documentElement.dataset.accent = "rose";
      const rose = paletteSnapshot();
      return cobalt.filter((value, index) => value !== rose[index]).length;
    });
    const colorCanvas = document.createElement("canvas");
    colorCanvas.width = 1;
    colorCanvas.height = 1;
    const colorContext = colorCanvas.getContext("2d", { willReadFrequently: true });
    const rgba = (value) => {
      colorContext.clearRect(0, 0, 1, 1);
      colorContext.fillStyle = value;
      colorContext.fillRect(0, 0, 1, 1);
      return [...colorContext.getImageData(0, 0, 1, 1).data].map(
        (channel, index) => index === 3 ? channel / 255 : channel,
      );
    };
    const composite = (foreground, background) => {
      const alpha = foreground[3] + background[3] * (1 - foreground[3]);
      return [0, 1, 2].map(
        (index) => (
          foreground[index] * foreground[3]
          + background[index] * background[3] * (1 - foreground[3])
        ) / alpha,
      ).concat(alpha);
    };
    const luminance = (color) => color.slice(0, 3).reduce((sum, channel, index) => {
      const normalized = channel / 255;
      const linear = normalized <= 0.04045
        ? normalized / 12.92
        : ((normalized + 0.055) / 1.055) ** 2.4;
      return sum + linear * [0.2126, 0.7152, 0.0722][index];
    }, 0);
    const contrast = (first, second) => {
      const [bright, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a);
      return (bright + 0.05) / (dark + 0.05);
    };
    const themeContrastRatios = [];
    for (const style of styleNames) {
      for (const accent of ["cobalt", "cyan", "violet", "emerald", "amber", "rose"]) {
        document.documentElement.dataset.style = style;
        document.documentElement.dataset.accent = accent;
        const rootStyle = getComputedStyle(document.documentElement);
        const panelStyle = getComputedStyle(document.querySelector(".panel"));
        const rootBackground = rgba(rootStyle.backgroundColor);
        const panelBackground = composite(rgba(panelStyle.backgroundColor), rootBackground);
        themeContrastRatios.push(contrast(rgba(rootStyle.color), panelBackground));
      }
    }
    const minimumThemePaletteDelta = Math.min(...themeDeltas);
    const minimumThemeContrast = Math.min(...themeContrastRatios);
    document.documentElement.dataset.style = "precision";
    document.documentElement.dataset.accent = "rose";
    precisionStyle.focus();
    precisionStyle.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    const styleKeyboardFocus = document.activeElement?.dataset.styleChoice;
    const glassCardStyle = getComputedStyle(document.querySelector(".metric-card"));
    const glassStyle = {
      radius: glassCardStyle.borderRadius,
      blur: glassCardStyle.backdropFilter,
    };
    document.querySelector('[data-accent-choice="rose"]').click();
    const accentColor = getComputedStyle(document.documentElement)
      .getPropertyValue("--blue").trim();
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
    const compressionCanvas = document.createElement("canvas");
    compressionCanvas.width = 4096;
    compressionCanvas.height = 4096;
    const compressionContext = compressionCanvas.getContext("2d");
    compressionContext.fillStyle = "#17304a";
    compressionContext.fillRect(0, 0, compressionCanvas.width, compressionCanvas.height);
    const compressionPng = await new Promise(
      (resolve) => compressionCanvas.toBlob(resolve, "image/png"),
    );
    compressionCanvas.width = 1;
    compressionCanvas.height = 1;
    const compressibleBlob = new Blob([
      compressionPng,
      new Uint8Array(8 * 1024 * 1024 + 1 - compressionPng.size),
    ], { type: "image/png" });
    const compressibleTransfer = new DataTransfer();
    compressibleTransfer.items.add(new File(
      [compressibleBlob],
      "compressible.png",
      { type: "image/png" },
    ));
    backgroundInput.files = compressibleTransfer.files;
    backgroundInput.dispatchEvent(new Event("change", { bubbles: true }));
    for (let attempt = 0; attempt < 120 && !document.querySelector("#background-image-status").classList.contains("success"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const compressedBackgroundStatus = document.querySelector("#background-image-status").textContent;
    const compressedBackground = await readStoredBackground().catch(() => null);
    const compressedBackgroundDimensions = compressedBackground
      ? await decodeImageSize(compressedBackground)
      : { width: 0, height: 0 };
    const oversizedTransfer = new DataTransfer();
    oversizedTransfer.items.add(new File(
      [new Uint8Array(32 * 1024 * 1024 + 1)],
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
    document.querySelector("#configured-host-list .group-action").click();
    const groupEditor = document.querySelector("#configured-host-list .host-group-editor");
    groupEditor.querySelector('input[type="text"]').value = "Priority";
    groupEditor.requestSubmit();
    for (let attempt = 0; attempt < 20 && ![...document.querySelectorAll("#configured-host-list .host-group-badge")].some((badge) => badge.textContent === "Priority"); attempt += 1) {
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
    for (let attempt = 0; attempt < 40 && document.querySelectorAll("#gpu-history-grid .trend-card").length < 4; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const taskDialogRect = taskDialog.getBoundingClientRect();
    const result = {
      utilizationVisible,
      reordered,
      savedServerSort: JSON.parse(localStorage.getItem("mocop.preferences.v1")).serverSort,
      savedVisualStyle: JSON.parse(localStorage.getItem("mocop.preferences.v1")).visualStyle,
      savedAccent: JSON.parse(localStorage.getItem("mocop.preferences.v1")).accent,
      savedDensity: JSON.parse(localStorage.getItem("mocop.preferences.v1")).density,
      savedBackgroundVisibility: JSON.parse(localStorage.getItem("mocop.preferences.v1")).backgroundVisibility,
      savedServerFilter: JSON.parse(localStorage.getItem("mocop.preferences.v1")).serverFilter,
      activeVisualStyle: document.documentElement.dataset.style,
      activeAccent: document.documentElement.dataset.accent,
      activeDensity: document.documentElement.dataset.density,
      backgroundActive: document.documentElement.dataset.background,
      backgroundStatus: document.querySelector("#background-image-status").textContent,
      backgroundRemoveEnabled: !document.querySelector("#remove-background-image").disabled,
      backgroundOpacity: getComputedStyle(document.documentElement).getPropertyValue("--custom-background-opacity").trim(),
      rejectedBackground,
      rejectedSpoofed,
      rejectedAnimation,
      compressedBackgroundStatus,
      compressedBackgroundSourceSize: compressibleBlob.size,
      compressedBackgroundSize: compressedBackground?.size || 0,
      compressedBackgroundType: compressedBackground?.type || "",
      compressedBackgroundDimensions,
      rejectedOversized,
      rejectedDimensions,
      terminalStyle,
      ledgerStyle,
      blueprintStyle,
      studioStyle,
      precisionAppearance,
      glassStyle,
      styleKeyboardFocus,
      accentColor,
      themePaletteDelta,
      themeDeltas,
      minimumThemePaletteDelta,
      minimumThemeContrast,
      styleCornerRadii,
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
      hostGroups: persistedCollector.hostGroups,
      maintenanceBadge,
      gpuSort: document.querySelector("#gpu-sort").value,
      powerHidden: document.body.classList.contains("hide-gpu-power"),
      taskDialogOpen: taskDialog.open,
      taskDialogCenterDelta: Math.abs(
        (taskDialogRect.left + taskDialogRect.right) / 2
          - document.documentElement.clientWidth / 2
      ),
      taskCount: document.querySelector("#gpu-task-count").textContent,
      taskNames: document.querySelector("#gpu-task-list").textContent,
      taskProcessRows: document.querySelectorAll("#gpu-task-list .gpu-task").length,
      taskMemoryTotal: document.querySelector("#gpu-task-memory-total")?.textContent || "",
      taskFirstMemoryShare: document.querySelector("#gpu-task-list .gpu-task-memory small")?.textContent || "",
      taskContext: document.querySelector("#gpu-task-list .gpu-task-workload")?.textContent || "",
      healthMetrics: document.querySelector("#gpu-detail-metrics").textContent,
      gpuHistoryRange: document.querySelector("#gpu-history-range").textContent,
      gpuHistoryCards: document.querySelectorAll("#gpu-history-grid .trend-card").length,
      gpuTimelineText: document.querySelector("#gpu-process-timeline").textContent,
      heatmapLegend: Boolean(document.querySelector(".heatmap-legend")),
      styleChoiceCount: document.querySelectorAll("[data-style-choice]").length,
      accentChoiceCount: document.querySelectorAll("[data-accent-choice]").length,
      restartDisabled: document.querySelector("#restart-service").disabled,
      restartStatus: document.querySelector("#service-restart-status").textContent,
    };
    const selectedRecord = selectedGpuRecord();
    const originalProcesses = selectedRecord.gpu.processes;
    selectedRecord.gpu.processes = Array.from({ length: 101 }, (_, index) => ({
      pid: 30000 + index,
      name: "/workspace/process-" + index + ".py",
      used_memory_mib: 101 - index,
      workload: null,
    }));
    renderGpuDetail();
    result.boundedTaskCount = document.querySelector("#gpu-task-count").textContent;
    result.boundedTaskRows = document.querySelectorAll("#gpu-task-list .gpu-task").length;
    result.boundedTaskNote = document.querySelector("#gpu-task-note").textContent;
    selectedRecord.gpu.processes = originalProcesses;
    renderGpuDetail();
    taskDialog.close();
    return result;
  })()`, true);
  assert.equal(personalization.utilizationVisible, true);
  assert.equal(personalization.reordered[0], "atlas-02");
  assert.equal(personalization.savedServerSort, "custom");
  assert.equal(personalization.savedVisualStyle, "glass");
  assert.equal(personalization.savedAccent, "rose");
  assert.equal(personalization.savedDensity, "compact");
  assert.equal(personalization.savedBackgroundVisibility, 52);
  assert.equal(personalization.savedServerFilter, "busy");
  assert.equal(personalization.activeVisualStyle, "glass");
  assert.equal(personalization.activeAccent, "rose");
  assert.equal(personalization.activeDensity, "compact");
  assert.equal(personalization.backgroundActive, "custom");
  assert.match(personalization.backgroundStatus, /4 × 3/);
  assert.equal(personalization.backgroundRemoveEnabled, true);
  assert.equal(personalization.backgroundOpacity, "0.52");
  assert.match(personalization.rejectedBackground, /PNG/);
  assert.match(personalization.rejectedSpoofed, /文件格式不匹配/);
  assert.match(personalization.rejectedAnimation, /动态图片/);
  assert.match(personalization.compressedBackgroundStatus, /已压缩/);
  assert(personalization.compressedBackgroundSourceSize > 8 * 1024 * 1024);
  assert(personalization.compressedBackgroundSize > 0);
  assert(personalization.compressedBackgroundSize <= 8 * 1024 * 1024);
  assert.equal(personalization.compressedBackgroundType, "image/webp");
  assert(personalization.compressedBackgroundDimensions.width <= 4096);
  assert(personalization.compressedBackgroundDimensions.height <= 4096);
  assert(
    personalization.compressedBackgroundDimensions.width
      * personalization.compressedBackgroundDimensions.height
      <= 12_000_000,
  );
  assert.match(personalization.rejectedOversized, /32 MiB/);
  assert.match(personalization.rejectedDimensions, /8192/);
  assert(personalization.themePaletteDelta >= 4);
  assert(
    personalization.minimumThemePaletteDelta >= 4,
    `all style palettes must change materially: ${personalization.themeDeltas.join(",")}`,
  );
  assert(personalization.minimumThemeContrast >= 4.5);
  assert.equal(personalization.terminalStyle.radius, "10px");
  assert.equal(personalization.terminalStyle.shadow, "none");
  assert.match(personalization.terminalStyle.font, /Mono/);
  assert.equal(personalization.ledgerStyle.borderLeft, "3px");
  assert.equal(personalization.ledgerStyle.radius, "14px");
  assert.match(personalization.ledgerStyle.headingFont, /Serif|Georgia|Songti/);
  assert.equal(personalization.ledgerStyle.columns, 3);
  assert.equal(personalization.ledgerStyle.scheme, "light");
  assert.equal(personalization.ledgerStyle.workspaceColumns, 1);
  assert.equal(personalization.blueprintStyle.radius, "12px");
  assert.equal(personalization.blueprintStyle.shadow, "none");
  assert.equal(personalization.blueprintStyle.headingTransform, "uppercase");
  assert.equal(personalization.studioStyle.radius, "22px");
  assert.equal(personalization.studioStyle.columns, 3);
  assert.equal(personalization.studioStyle.scheme, "light");
  assert.equal(personalization.studioStyle.fleetOnRight, true);
  assert.equal(personalization.precisionAppearance.radius, "16px");
  assert.equal(personalization.precisionAppearance.columns, 6);
  assert.equal(personalization.glassStyle.radius, "22px");
  assert.match(personalization.glassStyle.blur, /20px/);
  for (const [style, radii] of Object.entries(personalization.styleCornerRadii)) {
    assert(
      radii.every((radius) => radius >= 8),
      `${style} must keep every primary surface visibly rounded: ${radii.join(",")}`,
    );
  }
  assert.equal(personalization.styleKeyboardFocus, "glass");
  assert.equal(personalization.accentColor, "#f47ca8");
  assert(personalization.settingsCenterDelta < 2);
  assert.equal(personalization.settingsColumns, 2);
  assert.equal(personalization.configuredHosts, "4");
  assert.match(personalization.inventoryStatus, /atlas-04/);
  assert.equal(personalization.settingsOpen, true);
  assert.equal(personalization.collectorSettings.pollIntervalSeconds, 2);
  assert.equal(personalization.collectorSettings.probeTimeoutSeconds, 24);
  assert.equal(personalization.collectorSettings.maxWorkers, 6);
  assert.equal(personalization.maintenanceWindows["atlas-01"].reason, "Driver upgrade");
  assert.equal(personalization.hostGroups["atlas-01"], "Priority");
  assert.match(personalization.maintenanceBadge, /维护至/);
  assert.equal(personalization.gpuSort, "memory");
  assert.equal(personalization.powerHidden, true);
  assert.equal(personalization.taskDialogOpen, true);
  assert(personalization.taskDialogCenterDelta < 2);
  assert.equal(personalization.taskCount, "2");
  assert.equal(personalization.taskProcessRows, 2);
  assert.match(personalization.taskNames, /train\.py/);
  assert.match(personalization.taskNames, /PID 10000/);
  assert.match(personalization.taskMemoryTotal, /70\.5 GiB \/ 80 GiB/);
  assert.match(personalization.taskFirstMemoryShare, /87\.5%/);
  assert.match(personalization.taskContext, /Slurm · llm-train/);
  assert.match(personalization.taskContext, /用户 researcher/);
  assert.match(personalization.taskContext, /队列 gpu-long/);
  assert.equal(personalization.boundedTaskCount, "101");
  assert.equal(personalization.boundedTaskRows, 100);
  assert.match(personalization.boundedTaskNote, /101 个进程/);
  assert.match(personalization.boundedTaskNote, /最高的 100 个/);
  assert.match(personalization.healthMetrics, /硬件健康正常/);
  assert.match(personalization.gpuHistoryRange, /1 个样本/);
  assert.equal(personalization.gpuHistoryCards, 4);
  assert.match(personalization.gpuTimelineText, /暂未记录/);
  assert.equal(personalization.heatmapLegend, false);
  assert.equal(personalization.styleChoiceCount, 6);
  assert.equal(personalization.accentChoiceCount, 6);
  assert.equal(personalization.restartDisabled, true);
  assert.match(personalization.restartStatus, /不支持网页重启/);

  const reloaded = cdp.waitFor("Page.loadEventFired");
  await cdp.send("Page.reload");
  await reloaded;
  await new Promise((resolve) => setTimeout(resolve, 300));
  const persistedAppearance = await cdp.evaluate(`(async () => {
    for (let attempt = 0; attempt < 40 && document.documentElement.dataset.background !== "custom"; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    return {
      visualStyle: document.documentElement.dataset.style,
      accent: document.documentElement.dataset.accent,
      density: document.documentElement.dataset.density,
      background: document.documentElement.dataset.background,
      visibility: document.querySelector("#background-visibility").value,
      removeEnabled: !document.querySelector("#remove-background-image").disabled,
    };
  })()`, true);
  assert.deepEqual(persistedAppearance, {
    visualStyle: "glass",
    accent: "rose",
    density: "compact",
    background: "custom",
    visibility: "52",
    removeEnabled: true,
  });

  if (process.env.MOCOP_SCREENSHOT_PATH) {
    const screenshotStyle = process.env.MOCOP_SCREENSHOT_STYLE || "glass";
    const screenshotAccent = process.env.MOCOP_SCREENSHOT_ACCENT || "rose";
    const screenshotSettings = process.env.MOCOP_SCREENSHOT_SETTINGS === "1";
    assert(
      ["precision", "glass", "terminal", "ledger", "blueprint", "studio"].includes(screenshotStyle),
      `invalid screenshot style: ${screenshotStyle}`,
    );
    assert(
      ["cobalt", "cyan", "violet", "emerald", "amber", "rose"].includes(screenshotAccent),
      `invalid screenshot accent: ${screenshotAccent}`,
    );
    const screenshotPreviousAppearance = await cdp.evaluate(`(() => {
      const previous = {
        style: document.documentElement.dataset.style,
        accent: document.documentElement.dataset.accent,
        background: document.documentElement.dataset.background,
      };
      if (${JSON.stringify(screenshotSettings)} && !document.querySelector("#settings-dialog").open) {
        document.querySelector("#settings-toggle").click();
      }
      document.documentElement.dataset.style = ${JSON.stringify(screenshotStyle)};
      document.documentElement.dataset.accent = ${JSON.stringify(screenshotAccent)};
      document.documentElement.dataset.background = "none";
      return previous;
    })()`);
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
    await cdp.evaluate(`(() => {
      if (${JSON.stringify(screenshotSettings)} && document.querySelector("#settings-dialog").open) {
        document.querySelector("#settings-dialog").close();
      }
      document.documentElement.dataset.style = ${JSON.stringify(screenshotPreviousAppearance.style)};
      document.documentElement.dataset.accent = ${JSON.stringify(screenshotPreviousAppearance.accent)};
      document.documentElement.dataset.background = ${JSON.stringify(screenshotPreviousAppearance.background)};
    })()`);
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
    const result = {
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      gpuMemoryWidth: document.querySelector("#gpu-memory-card")?.getBoundingClientRect().width,
      gridWidth: document.querySelector(".metrics-grid")?.getBoundingClientRect().width,
      settingsCenterDelta: Math.abs(
        (rect.left + rect.right) / 2 - document.documentElement.clientWidth / 2
      ),
    };
    document.querySelector("#settings-dialog").close();
    document.querySelector("#topology-toggle").click();
    const topologyDialog = document.querySelector("#topology-dialog");
    const topologyRect = topologyDialog.getBoundingClientRect();
    const topologyScroll = document.querySelector(".topology-scroll");
    result.topologyCenterDelta = Math.abs(
      (topologyRect.left + topologyRect.right) / 2
        - document.documentElement.clientWidth / 2
    );
    result.topologyWidth = topologyRect.width;
    result.topologyScrollContained = topologyScroll.scrollWidth >= topologyScroll.clientWidth;
    result.topologyDocumentOverflow = document.documentElement.scrollWidth
      > document.documentElement.clientWidth;
    topologyDialog.close();
    document.querySelector(".gpu-table-body tbody tr").click();
    const gpuDetailDialog = document.querySelector("#gpu-detail-dialog");
    const gpuDetailRect = gpuDetailDialog.getBoundingClientRect();
    result.gpuDetailCenterDelta = Math.abs(
      (gpuDetailRect.left + gpuDetailRect.right) / 2
        - document.documentElement.clientWidth / 2
    );
    result.gpuDetailWidth = gpuDetailRect.width;
    result.gpuDetailColumns = getComputedStyle(
      document.querySelector(".gpu-detail-content"),
    ).gridTemplateColumns.split(" ").length;
    result.gpuDetailDocumentOverflow = document.documentElement.scrollWidth
      > document.documentElement.clientWidth;
    gpuDetailDialog.close();
    result.styleOverflows = {};
    for (const style of ["precision", "glass", "terminal", "ledger", "blueprint", "studio"]) {
      document.documentElement.dataset.style = style;
      document.body.getBoundingClientRect();
      result.styleOverflows[style] = document.documentElement.scrollWidth
        > document.documentElement.clientWidth;
    }
    document.documentElement.dataset.style = "glass";
    document.querySelector("#settings-toggle").click();
    return result;
  })()`);
  assert.equal(mobile.overflow, false);
  assert(mobile.gpuMemoryWidth > mobile.gridWidth * 0.9);
  assert(mobile.settingsCenterDelta < 2);
  assert(mobile.topologyCenterDelta < 2);
  assert(mobile.topologyWidth <= 390);
  assert.equal(mobile.topologyScrollContained, true);
  assert.equal(mobile.topologyDocumentOverflow, false);
  assert(mobile.gpuDetailCenterDelta < 2);
  assert(mobile.gpuDetailWidth <= 390);
  assert.equal(mobile.gpuDetailColumns, 1);
  assert.equal(mobile.gpuDetailDocumentOverflow, false);
  assert.deepEqual(mobile.styleOverflows, {
    precision: false,
    glass: false,
    terminal: false,
    ledger: false,
    blueprint: false,
    studio: false,
  });
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
  await cdp.evaluate(`localStorage.setItem(
    "mocop.preferences.v1",
    JSON.stringify({ theme: "aurora" })
  )`);
  const legacyReload = cdp.waitFor("Page.loadEventFired");
  await cdp.send("Page.reload");
  await legacyReload;
  const legacyAppearance = await cdp.evaluate(`(() => ({
    visualStyle: document.documentElement.dataset.style,
    accent: document.documentElement.dataset.accent,
  }))()`);
  assert.deepEqual(legacyAppearance, {
    visualStyle: "blueprint",
    accent: "cyan",
  });
  assert.deepEqual(cdp.errors, []);

  console.log(JSON.stringify({
    browser: "chrome", initial, final, transientConnection, topologyBenchmark,
    capacity, grouping, personalization,
    persistedAppearance, mobile, removedBackground, legacyAppearance,
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
