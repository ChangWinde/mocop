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

async function waitForEvaluation(client, expression, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let value;
  while (Date.now() < deadline) {
    value = await client.evaluate(expression, true);
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Timed out waiting for browser expression: ${expression}; last=${value}`);
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
const browserAccessToken = "B".repeat(43);
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
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent("about:blank")}`,
    { method: "PUT" },
  );
  assert(targetResponse.ok, `Chrome target creation failed: ${targetResponse.status}`);
  const target = await targetResponse.json();
  cdp = await CdpClient.connect(target.webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
    source: `const shouldChangeCadence = window.location.hash.includes("access_token=");
    window.addEventListener("DOMContentLoaded", () => {
      if (!shouldChangeCadence) return;
      const select = document.querySelector("#refresh-interval");
      select.value = "2";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      window.__mocopEarlyCadenceChange = true;
    });`,
  });
  // Chrome can be CPU-starved while the Python version matrix runs on the
  // same hosted-runner fleet. Keep the wait finite, but give navigation the
  // same 30-second startup budget as the DevTools endpoint.
  const loaded = cdp.waitFor("Page.loadEventFired", 30_000);
  await cdp.send("Page.navigate", {
    url: `http://127.0.0.1:${monitorPort}/`,
  });
  await loaded;

  // A bare forwarded URL cannot inherit a capability from another tab. It
  // must present an explicit, non-dismissible authentication flow instead of
  // leaving the dashboard as an unexplained collection of empty placeholders.
  await waitForEvaluation(cdp, "document.querySelector('#authentication-dialog')?.open");
  const missingAuthentication = await cdp.evaluate(`({
    open: document.querySelector("#authentication-dialog")?.open,
    focused: document.activeElement?.id,
    connection: document.querySelector("#connection-text")?.textContent,
    stored: window.sessionStorage.getItem("mocop.dashboardAccessToken.v1"),
  })`);
  assert.deepEqual(missingAuthentication, {
    open: true,
    focused: "authentication-token",
    connection: "需要访问令牌",
    stored: null,
  });

  const invalidAuthentication = await cdp.evaluate(`(async () => {
    const input = document.querySelector("#authentication-token");
    input.value = "short";
    document.querySelector("#authentication-form").requestSubmit();
    await new Promise((resolve) => setTimeout(resolve, 50));
    return {
      open: document.querySelector("#authentication-dialog").open,
      status: document.querySelector("#authentication-status").textContent,
      stored: window.sessionStorage.getItem("mocop.dashboardAccessToken.v1"),
    };
  })()`, true);
  assert.equal(invalidAuthentication.open, true);
  assert.match(invalidAuthentication.status, /格式/);
  assert.equal(invalidAuthentication.stored, null);

  const wrongToken = "C".repeat(43);
  await cdp.evaluate(`(() => {
    const input = document.querySelector("#authentication-token");
    input.value = ${JSON.stringify(wrongToken)};
    document.querySelector("#authentication-form").requestSubmit();
  })()`);
  await waitForEvaluation(
    cdp,
    "document.querySelector('#authentication-status')?.textContent.includes('不正确')",
  );
  assert.equal(
    await cdp.evaluate("window.sessionStorage.getItem('mocop.dashboardAccessToken.v1')"),
    null,
  );

  await cdp.evaluate(`(() => {
    const input = document.querySelector("#authentication-token");
    input.value = ${JSON.stringify(browserAccessToken)};
    document.querySelector("#authentication-form").requestSubmit();
  })()`);
  await waitForEvaluation(
    cdp,
    "!document.querySelector('#authentication-dialog')?.open && Boolean(window.sessionStorage.getItem('mocop.dashboardAccessToken.v1'))",
  );
  const authenticatedReload = cdp.waitFor("Page.loadEventFired", 30_000);
  await cdp.send("Page.reload", { ignoreCache: true });
  await authenticatedReload;
  await waitForEvaluation(cdp, "document.querySelector('#server-ratio')?.textContent !== '— / —'");
  assert.equal(
    await cdp.evaluate("document.querySelector('#authentication-dialog')?.open"),
    false,
  );

  // Keep the original capability-link path covered independently: a fragment
  // still auto-authenticates, is scrubbed immediately, and can perform the
  // pre-snapshot collector update without an unauthenticated request.
  await cdp.evaluate("window.sessionStorage.clear()");
  const fragmentLoaded = cdp.waitFor("Page.loadEventFired", 30_000);
  await cdp.send("Page.navigate", {
    url: `http://127.0.0.1:${monitorPort}/?auth=fragment#access_token=${browserAccessToken}`,
  });
  await fragmentLoaded;
  assert.equal(await cdp.evaluate("window.location.hash"), "");
  assert.equal(
    await cdp.evaluate("window.sessionStorage.getItem('mocop.dashboardAccessToken.v1')"),
    browserAccessToken,
  );

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
  assert.equal(
    await cdp.evaluate("window.sessionStorage.getItem('mocop.dashboardAccessToken.v1')"),
    browserAccessToken,
  );

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
  assert.match(final.versionLabel, /v0\.9\.0/);
  assert.equal(final.serverRatio, "2 / 4");
  assert.equal(final.totalGpus, "8");
  assert.match(final.persistenceStatus, /仅内存/);
  // "\u6295\u9012\u5f02\u5e38" = delivery unhealthy (one endpoint failing).
  assert.match(final.notificationStatus, /\u6295\u9012\u5F02\u5E38/);
  assert.match(final.notificationStatus, /3 \u6B21\u5931\u8D25/);
  assert.equal(final.gpuGroups, 2);
  assert.equal(final.expandedGroups, 0);
  assert.equal(final.heatmapVisible, true);
  assert.equal(final.attentionVisible, true);
  assert.equal(final.overflow, false);

  const displayName = await cdp.evaluate(`(() => {
    const server = view.snapshot.servers.find((item) => item.host === "atlas-01");
    const previousDisplayName = server.displayName;
    const previousHost = view.selectedHost;
    server.displayName = "console-0";
    view.serverItemCache.clear();
    view.groupCache.clear();
    view.heatmapCache.clear();
    view.singleTableCache = null;
    render();
    const sidebar = document.querySelector(
      '.server-item[data-host="atlas-01"] .server-name',
    )?.textContent;
    const groupHeading = document.querySelector(
      '.gpu-server-group[data-host="atlas-01"] .gpu-group-name strong',
    )?.textContent;
    const heatmapHeading = document.querySelector(
      '.heatmap-row[data-host="atlas-01"] .heatmap-host',
    )?.textContent;
    selectHost("atlas-01");
    const inventoryTitle = document.querySelector("#inventory-title")?.textContent;
    const tableHeading = document.querySelector(
      '#gpu-groups tr[data-host="atlas-01"] .device-text strong',
    )?.textContent;
    openGpuDetail(server, server.gpus[0]);
    const gpuDetailHeading = document.querySelector("#gpu-detail-host")?.textContent;
    document.querySelector("#gpu-detail-dialog").close();
    server.displayName = previousDisplayName;
    view.selectedHost = previousHost;
    view.serverItemCache.clear();
    view.groupCache.clear();
    view.heatmapCache.clear();
    view.singleTableCache = null;
    render();
    return {
      sidebar,
      groupHeading,
      heatmapHeading,
      inventoryTitle,
      tableHeading,
      gpuDetailHeading,
      internalHost: server.host,
    };
  })()`);
  assert.equal(displayName.sidebar, "console-0");
  assert.equal(displayName.groupHeading, "console-0");
  assert.equal(displayName.heatmapHeading, "console-0");
  assert.equal(displayName.inventoryTitle, "console-0");
  assert.equal(displayName.tableHeading, "console-0");
  assert.match(displayName.gpuDetailHeading, /^console-0 · GPU 0$/);
  assert.equal(displayName.internalHost, "atlas-01");

  const heatmapPacking = await cdp.evaluate(`(() => {
    const previousSnapshot = view.snapshot;
    const previousHost = view.selectedHost;
    const template = previousSnapshot.servers.find(
      (server) => server.status === "online" && server.gpus.length,
    );
    const gpu = template.gpus[0];
    const server = {
      ...template,
      host: "sixteen-gpu",
      gpus: Array.from({ length: 16 }, (_, index) => ({
        ...gpu,
        index,
        uuid: \`GPU-SIXTEEN-\${String(index).padStart(2, "0")}\`,
      })),
    };
    view.snapshot = { ...previousSnapshot, servers: [server] };
    view.selectedHost = "all";
    view.heatmapCache.clear();
    view.heatmapAxisCache = null;
    renderHeatmap();
    const row = document.querySelector(".heatmap-row");
    const tiles = [...row.querySelectorAll(".heatmap-cell:not(.placeholder)")];
    const rows = Map.groupBy(tiles, (tile) => Math.round(tile.getBoundingClientRect().top));
    const result = {
      columns: getComputedStyle(row).gridTemplateColumns.split(" ").length - 1,
      rows: getComputedStyle(row).gridTemplateRows.split(" ").length,
      rowSizes: [...rows.values()].map((items) => items.length),
      tileCount: tiles.length,
    };
    view.snapshot = previousSnapshot;
    view.selectedHost = previousHost;
    view.heatmapCache.clear();
    view.heatmapAxisCache = null;
    renderHeatmap();
    return result;
  })()`);
  assert.deepEqual(heatmapPacking, {
    columns: 8,
    rows: 2,
    rowSizes: [8, 8],
    tileCount: 16,
  });

  const programSearch = await cdp.evaluate(`(() => {
    const input = document.querySelector("#search");
    selectHost("all");
    input.value = "train.py";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    const panel = document.querySelector("#program-search-panel");
    const globalRows = [...document.querySelectorAll(".program-search-result")];
    const firstRow = globalRows[0];
    const firstButton = firstRow.querySelector("button");
    firstButton.focus();
    render();
    const rowReused = firstRow === document.querySelector(".program-search-result");
    const focusPreserved = document.activeElement === firstButton;
    const global = {
      hidden: panel.hidden,
      scope: document.querySelector("#program-search-scope").textContent,
      count: document.querySelector("#program-search-count").textContent,
      hosts: globalRows.map((row) => row.dataset.host),
      names: globalRows.map(
        (row) => row.querySelector(".program-search-name")?.textContent || "",
      ),
      rowReused,
      focusPreserved,
    };
    input.value = "ＴＲＡＩＮ．ＰＹ";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    const normalizedCount = document.querySelector("#program-search-count").textContent;
    input.value = "x".repeat(121);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    const boundedQueryLength = input.value.length;

    const server1 = view.snapshot.servers.find((server) => server.host === "atlas-01");
    const gpu1 = server1.gpus.find((gpu) => gpu.index === 0);
    const originalProcesses = gpu1.processes;
    gpu1.processes = Array.from({ length: 205 }, (_, index) => ({
      pid: 70_000 + index,
      name: "bulk-search-" + index + ".py",
      used_memory_mib: index,
      workload: null,
    }));
    input.value = "bulk-search";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    const bounded = {
      count: document.querySelector("#program-search-count").textContent,
      rows: document.querySelectorAll(".program-search-result").length,
      summary: document.querySelector("#program-search-summary").textContent,
      first: document.querySelector(".program-search-name")?.textContent || "",
    };
    const lastBoundedResult = document.querySelector(
      ".program-search-result:last-child button",
    );
    lastBoundedResult.click();
    const pinnedDetail = {
      visibleRows: [...document.querySelectorAll("#gpu-task-list .gpu-task")]
        .map((row) => row.dataset.processKey),
      focusedKey: document.activeElement?.dataset.processKey || "",
      note: document.querySelector("#gpu-task-note").textContent,
    };
    document.querySelector("#gpu-detail-dialog").close();

    window.__mocopSearchXss = false;
    gpu1.processes = [{
      pid: 99_999,
      name: '<img src=x onerror="window.__mocopSearchXss=true">',
      used_memory_mib: 1,
      workload: {
        kind: "process",
        owner: "<b>root</b>",
        command: '<svg onload="window.__mocopSearchXss=true">',
      },
    }];
    input.value = "onerror";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    const hostile = {
      rows: document.querySelectorAll(".program-search-result").length,
      text: document.querySelector(".program-search-result")?.textContent || "",
      injectedNodes: document.querySelectorAll(
        ".program-search-result img, .program-search-result svg, .program-search-result b",
      ).length,
      executed: window.__mocopSearchXss,
    };
    gpu1.processes = originalProcesses;
    input.value = "atlas-01 researcher";
    input.dispatchEvent(new Event("input", { bubbles: true }));

    selectHost("atlas-01");
    const scopedRows = [...document.querySelectorAll(".program-search-result")];
    const scoped = {
      scope: document.querySelector("#program-search-scope").textContent,
      count: document.querySelector("#program-search-count").textContent,
      hosts: scopedRows.map((row) => row.dataset.host),
    };
    scopedRows[0].querySelector("button").click();
    const detail = {
      open: document.querySelector("#gpu-detail-dialog").open,
      host: document.querySelector("#gpu-detail-host").textContent,
      query: document.querySelector("#gpu-task-search").value,
      visibleRows: [...document.querySelectorAll("#gpu-task-list .gpu-task")]
        .map((row) => row.dataset.processKey),
      focusedTarget: document.activeElement?.classList.contains("gpu-task") || false,
    };
    document.querySelector("#gpu-detail-dialog").close();
    selectHost("all");
    input.value = "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return {
      global, normalizedCount, boundedQueryLength, bounded, pinnedDetail,
      hostile, scoped, detail, maxLength: input.maxLength,
    };
  })()`);
  assert.equal(programSearch.global.hidden, false);
  assert.equal(programSearch.global.scope, "全局");
  assert.equal(programSearch.global.count, "5");
  assert.deepEqual([...new Set(programSearch.global.hosts)], [
    "atlas-01", "atlas-02", "atlas-03",
  ]);
  assert(programSearch.global.names.every((name) => name === "train.py"));
  assert.equal(programSearch.global.rowReused, true);
  assert.equal(programSearch.global.focusPreserved, true);
  assert.equal(programSearch.normalizedCount, "5");
  assert.equal(programSearch.boundedQueryLength, 120);
  assert.equal(programSearch.bounded.count, "205");
  assert.equal(programSearch.bounded.rows, 200);
  assert.match(programSearch.bounded.summary, /205/);
  assert.match(programSearch.bounded.summary, /200/);
  assert.equal(programSearch.bounded.first, "bulk-search-204.py");
  assert.equal(programSearch.pinnedDetail.visibleRows.length, 100);
  assert(programSearch.pinnedDetail.visibleRows.includes("70005|bulk-search-5.py"));
  assert.equal(programSearch.pinnedDetail.focusedKey, "70005|bulk-search-5.py");
  assert.match(programSearch.pinnedDetail.note, /优先显示所选程序/);
  assert.equal(programSearch.hostile.rows, 1);
  assert.match(programSearch.hostile.text, /<img src=x onerror/);
  assert.equal(programSearch.hostile.injectedNodes, 0);
  assert.equal(programSearch.hostile.executed, false);
  assert.equal(programSearch.scoped.scope, "atlas-01");
  assert.equal(programSearch.scoped.count, "2");
  assert.deepEqual([...new Set(programSearch.scoped.hosts)], ["atlas-01"]);
  assert.equal(programSearch.detail.open, true);
  assert.match(programSearch.detail.host, /atlas-01/);
  assert.equal(programSearch.detail.query, "atlas-01 researcher");
  assert.equal(programSearch.detail.visibleRows.length, 1);
  assert.match(programSearch.detail.visibleRows[0], /^10000\|/);
  assert.equal(programSearch.detail.focusedTarget, true);
  assert.equal(programSearch.maxLength, 120);

  let programSearchBenchmark = null;
  if (process.env.MOCOP_PROGRAM_SEARCH_BENCHMARK === "1") {
    programSearchBenchmark = await cdp.evaluate(`(() => {
      const syntheticSnapshot = () => ({
        servers: Array.from({ length: 128 }, (_, hostIndex) => ({
          host: "bench-" + String(hostIndex).padStart(3, "0"),
          stale: false,
          gpus: Array.from({ length: 8 }, (_, gpuIndex) => ({
            index: gpuIndex,
            uuid: "GPU-BENCH-" + hostIndex + "-" + gpuIndex,
            name: "NVIDIA H100",
            memory_total_mib: 81920,
            processes_available: true,
            processes: Array.from({ length: 64 }, (_, processIndex) => ({
              pid: hostIndex * 100000 + gpuIndex * 1000 + processIndex,
              name: "/workspace/train-" + processIndex + ".py",
              used_memory_mib: 512 + processIndex,
              workload: {
                kind: "slurm",
                name: "llm-train",
                owner: "researcher",
                queue: "gpu-long",
                command: "python train.py --stage sft",
              },
            })),
          })),
        })),
      });
      const synthetic = syntheticSnapshot();
      const query = "train researcher";
      const referenceSearch = () => {
        const terms = normalizedSearchTerms(query);
        const matches = [];
        synthetic.servers.forEach((server) => server.gpus.forEach((gpu) => {
          gpu.processes.forEach((process) => {
            if (!processMatchesSearch(process, terms, server, gpu)) return;
            const record = { server, gpu, process, rank: 0 };
            record.rank = processSearchRank(record, terms);
            matches.push(record);
          });
        }));
        matches.sort(compareProcessSearchRecords);
        return { total: matches.length, matches: matches.slice(0, 200) };
      };
      const boundedSearch = () => searchProcessRecords(synthetic, query, "all");
      const coldSnapshot = syntheticSnapshot();
      const coldStarted = performance.now();
      const coldResult = searchProcessRecords(coldSnapshot, query, "all");
      const coldMs = performance.now() - coldStarted;
      const summarizeProcesses = (snapshot) => {
        let count = 0;
        let knownMemoryMiB = 0;
        snapshot.servers.forEach((server) => server.gpus.forEach((gpu) => {
          const summary = gpuProcessSummary(gpu);
          count += summary.count;
          knownMemoryMiB += summary.knownMemoryMiB;
        }));
        return { count, knownMemoryMiB };
      };
      const summaryColdSnapshot = syntheticSnapshot();
      const summaryColdStarted = performance.now();
      const summaryColdResult = summarizeProcesses(summaryColdSnapshot);
      const summaryColdMs = performance.now() - summaryColdStarted;
      for (let index = 0; index < 3; index += 1) {
        referenceSearch();
        boundedSearch();
        summarizeProcesses(synthetic);
      }
      const measure = (fn) => Array.from({ length: 10 }, () => {
        const started = performance.now();
        const result = fn();
        return { elapsed: performance.now() - started, result };
      });
      const reference = measure(referenceSearch);
      const bounded = measure(boundedSearch);
      const summaries = measure(() => summarizeProcesses(synthetic));
      const summarize = (samples) => {
        const values = samples.map((sample) => sample.elapsed).sort((a, b) => a - b);
        const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
        const variance = values.reduce(
          (sum, value) => sum + (value - mean) ** 2,
          0,
        ) / (values.length - 1);
        return {
          medianMs: values[Math.floor(values.length / 2)],
          meanMs: mean,
          stdevMs: Math.sqrt(variance),
          minMs: values[0],
          maxMs: values.at(-1),
        };
      };
      const referenceResult = reference[0].result;
      const boundedResult = bounded[0].result;
      return {
        records: 128 * 8 * 64,
        runs: 10,
        coldMs,
        coldTotal: coldResult.total,
        coldRetained: coldResult.matches.length,
        summaryColdMs,
        summaryCount: summaryColdResult.count,
        summaryKnownMemoryMiB: summaryColdResult.knownMemoryMiB,
        summaryCached: summarize(summaries),
        reference: summarize(reference),
        bounded: summarize(bounded),
        totalsEqual: referenceResult.total === boundedResult.total,
        firstEqual: programSearchKey(referenceResult.matches[0])
          === programSearchKey(boundedResult.matches[0]),
        retained: boundedResult.matches.length,
      };
    })()`);
    assert.equal(programSearchBenchmark.records, 65_536);
    assert.equal(programSearchBenchmark.runs, 10);
    assert.equal(programSearchBenchmark.coldTotal, 65_536);
    assert.equal(programSearchBenchmark.coldRetained, 200);
    assert.equal(programSearchBenchmark.summaryCount, 65_536);
    assert(programSearchBenchmark.summaryKnownMemoryMiB > 0);
    assert.equal(programSearchBenchmark.totalsEqual, true);
    assert.equal(programSearchBenchmark.firstEqual, true);
    assert.equal(programSearchBenchmark.retained, 200);
  }

  // Localized transport-failure copy: the fixture's atlas-06 carries a real
  // collector error message, the remaining mappings are checked directly.
  const failureMappings = await cdp.evaluate(`(() => {
    const fleetItem = document.querySelector('.server-item[data-host="atlas-06"]');
    return {
      fleetIssueText: fleetItem?.querySelector(".issue-text")?.textContent || "",
      attentionTexts: [...document.querySelectorAll(
        "#attention-list .attention-item .attention-message",
      )].map((node) => node.textContent),
      transportStopped: failureText("SSH transport stopped responding"),
      noOutput: failureText("SSH produced no output before the collection timeout"),
      stalled: failureText("Remote collection stalled after partial output"),
      cancelled: failureText("Resource collection cancelled"),
      unexpected: failureText("Unexpected collector error"),
      remoteQuery: failureText("Remote resource query failed (exit 137)"),
      localQuery: failureText("Local resource query failed (exit 1)"),
      unknownPassthrough: failureText("Some new backend message"),
    };
  })()`);
  // "SSH \u4f20\u8f93\u5931\u53bb\u54cd\u5e94\uff08keepalive \u8d85\u65f6\uff09"
  // = SSH transport lost responsiveness (keepalive timeout).
  const transportStoppedText =
    "SSH \u4F20\u8F93\u5931\u53BB\u54CD\u5E94\uFF08keepalive \u8D85\u65F6\uFF09";
  assert.equal(
    failureMappings.fleetIssueText,
    transportStoppedText,
    "fleet list localizes the transport-stopped error host",
  );
  assert(
    failureMappings.attentionTexts.some(
      (text) => text.includes(transportStoppedText),
    ),
    "attention panel localizes the transport-stopped incident",
  );
  assert.equal(failureMappings.transportStopped, transportStoppedText);
  // "SSH \u5728\u91c7\u96c6\u8d85\u65f6\u524d\u65e0\u4efb\u4f55\u8f93\u51fa"
  // = no output before the collection timeout.
  assert.equal(
    failureMappings.noOutput,
    "SSH \u5728\u91C7\u96C6\u8D85\u65F6\u524D\u65E0\u4EFB\u4F55\u8F93\u51FA",
  );
  // "\u8fdc\u7aef\u91c7\u96c6\u5728\u90e8\u5206\u8f93\u51fa\u540e\u505c\u6ede"
  // = remote collection stalled after partial output.
  assert.equal(
    failureMappings.stalled,
    "\u8FDC\u7AEF\u91C7\u96C6\u5728\u90E8\u5206\u8F93\u51FA\u540E\u505C\u6EDE",
  );
  // "\u8d44\u6e90\u91c7\u96c6\u5df2\u53d6\u6d88" = collection cancelled.
  assert.equal(failureMappings.cancelled, "\u8D44\u6E90\u91C7\u96C6\u5DF2\u53D6\u6D88");
  // "\u91c7\u96c6\u5668\u53d1\u751f\u672a\u9884\u671f\u9519\u8bef"
  // = unexpected collector error.
  assert.equal(
    failureMappings.unexpected,
    "\u91C7\u96C6\u5668\u53D1\u751F\u672A\u9884\u671F\u9519\u8BEF",
  );
  // Prefix mappings keep the dynamic exit-code suffix verbatim:
  // "\u8fdc\u7aef/\u672c\u673a\u8d44\u6e90\u67e5\u8be2\u5931\u8d25"
  // = remote/local resource query failed.
  assert.equal(
    failureMappings.remoteQuery,
    "\u8FDC\u7AEF\u8D44\u6E90\u67E5\u8BE2\u5931\u8D25 (exit 137)",
    "remote query prefix mapping keeps the exit-code suffix",
  );
  assert.equal(
    failureMappings.localQuery,
    "\u672C\u673A\u8D44\u6E90\u67E5\u8BE2\u5931\u8D25 (exit 1)",
    "local query prefix mapping keeps the exit-code suffix",
  );
  assert.equal(failureMappings.unknownPassthrough, "Some new backend message");

  const screenshotPath = process.env.MOCOP_BROWSER_SCREENSHOT;
  if (screenshotPath) {
    const screenshot = await cdp.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: false,
    });
    await writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));
  }

  // Authenticated fetch-stream heartbeats refresh liveness, and its parser
  // accepts a CRLF delimiter split at an arbitrary network chunk boundary.
  const heartbeat = await cdp.evaluate(`(() => {
    view.lastEventAt = 0;
    acceptStreamFrame("event: heartbeat\\ndata: {}", () => setConnection("live", "实时连接"));
    const partial = appendStreamChunk("", "event: heartbeat\\r");
    const complete = appendStreamChunk(partial, "\\ndata: {}\\r\\n\\r\\n");
    return {
      updated: view.lastEventAt > 0,
      connectionClass: document.querySelector("#connection")?.className || "",
      splitFrame: complete === "event: heartbeat\\ndata: {}\\n\\n",
    };
  })()`);
  assert.equal(heartbeat.updated, true, "named heartbeat refreshes lastEventAt");
  assert.match(heartbeat.connectionClass, /live/);
  assert.equal(heartbeat.splitFrame, true, "CRLF split parser produces one frame");

  const topology = await cdp.evaluate(`(async () => {
    for (let attempt = 0; attempt < 40 && !document.querySelector("#topology-toggle"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    document.querySelector("#topology-toggle").click();
    for (let attempt = 0; attempt < 40 && document.querySelectorAll(".topology-node").length < 6; attempt += 1) {
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
    ["monitor-console", "atlas-gateway", "atlas-01", "atlas-02", "atlas-03", "atlas-06"],
  );
  assert.deepEqual(topology.links, ["STCP · 7005", "SSH", "SSH", "SSH", "SSH"]);
  assert.equal(topology.frpCount, "1");
  assert.match(topology.live, /2 \/ 4/);
  assert.equal(topology.offline, 2);
  assert.equal(topology.infrastructure, 2);
  assert.equal(topology.monitored, 4);
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
  // "\u6e29\u5ea6\u4f4e\u4e8e ... \u8b66\u6212\u7ebf" = temperature below the
  // warning threshold, mirroring the temperature filter in capacityMatches().
  assert.match(
    capacity.rule,
    /\u6E29\u5EA6\u4F4E\u4E8E 80\u00B0C \u8B66\u6212\u7EBF/,
    "capacity rule discloses the temperature condition",
  );
  assert(capacity.centerDelta < 2);

  const capacityWatchFlow = await cdp.evaluate(`(() => {
    document.querySelector("#capacity-toggle").click();
    document.querySelector("#capacity-gpu-count").value = "2";
    document.querySelector("#capacity-vram").value = "60";
    const toggle = document.querySelector("#capacity-watch-toggle");
    const status = document.querySelector("#capacity-watch-status");
    const banner = document.querySelector("#capacity-watch-banner");
    toggle.click();
    const afterSave = {
      toggleText: toggle.textContent,
      watching: toggle.dataset.watching || "",
      statusText: status.textContent,
      bannerVisible: !banner.hidden,
      bannerText: document.querySelector("#capacity-watch-banner-text").textContent,
      title: document.title,
      stored: JSON.parse(localStorage.getItem("mocop.capacityWatch.v1") || "null"),
    };
    document.querySelector("#capacity-watch-banner-dismiss").click();
    const afterDismiss = { bannerVisible: !banner.hidden, title: document.title };
    toggle.click();
    const afterStop = {
      toggleText: toggle.textContent,
      watching: toggle.dataset.watching || "",
      bannerVisible: !banner.hidden,
      stored: localStorage.getItem("mocop.capacityWatch.v1"),
    };
    document.querySelector("#capacity-dialog").close();
    return { afterSave, afterDismiss, afterStop };
  })()`);
  // Saving the watch while the fixture already satisfies 2x60 GiB must fire
  // the ready edge immediately: banner, title marker, and durable state.
  assert.equal(capacityWatchFlow.afterSave.toggleText, "\u505C\u6B62\u5B88\u671B");
  assert.equal(capacityWatchFlow.afterSave.watching, "true");
  assert.match(capacityWatchFlow.afterSave.statusText, /\u5DF2\u5C31\u7EEA/);
  assert.equal(capacityWatchFlow.afterSave.bannerVisible, true);
  assert.match(capacityWatchFlow.afterSave.bannerText, /1 \u4E2A\u8282\u70B9\u6EE1\u8DB3/);
  assert.match(capacityWatchFlow.afterSave.bannerText, /60 GiB/);
  assert(capacityWatchFlow.afterSave.title.startsWith("\u25CF "));
  assert.equal(capacityWatchFlow.afterSave.stored?.state, "notified");
  assert.equal(capacityWatchFlow.afterSave.stored?.request?.gpuCount, 2);
  assert.equal(capacityWatchFlow.afterDismiss.bannerVisible, false);
  assert(!capacityWatchFlow.afterDismiss.title.startsWith("\u25CF "));
  assert.equal(capacityWatchFlow.afterStop.toggleText, "\u7A7A\u95F2\u65F6\u63D0\u9192\u6211");
  assert.equal(capacityWatchFlow.afterStop.watching, "");
  assert.equal(capacityWatchFlow.afterStop.bannerVisible, false);
  assert.equal(capacityWatchFlow.afterStop.stored, null);

  const owners = await cdp.evaluate(`(() => {
    document.querySelector("#owners-toggle").click();
    const dialog = document.querySelector("#owners-dialog");
    const rows = [...document.querySelectorAll("#owners-results .capacity-candidate")];
    const metricNumbers = (row) => [...row.querySelectorAll(".capacity-candidate-metrics span")]
      .map((item) => Number.parseInt(item.textContent, 10));
    const rowInfo = (row) => ({
      name: row.querySelector("strong")?.textContent,
      nameTitle: row.querySelector("strong")?.title,
      vram: row.querySelector("em")?.textContent,
      metrics: metricNumbers(row),
      hostChips: [...row.querySelectorAll(".capacity-devices .owner-host-chip")].map(
        (chip) => chip.textContent,
      ),
      cardTitle: row.title || "",
    });
    const researcherRows = rows.filter(
      (row) => row.querySelector("strong")?.textContent === "researcher",
    );
    const rect = dialog.getBoundingClientRect();
    const result = {
      open: dialog.open,
      rows: rows.length,
      ownerNames: rows.map((row) => row.querySelector("strong")?.textContent),
      researcherRowCount: researcherRows.length,
      researcher: researcherRows.length ? rowInfo(researcherRows[0]) : null,
      unattributed: rows.length > 1 ? rowInfo(rows[1]) : null,
      firstVram: rows[0]?.querySelector("em")?.textContent,
      summary: document.querySelector("#owners-summary")?.textContent,
      offlineNote: document.querySelector("#owners-results .owners-footnote")?.textContent || "",
      centerDelta: Math.abs((rect.left + rect.right) / 2 - document.documentElement.clientWidth / 2),
    };
    dialog.close();
    return result;
  })()`);
  assert.equal(owners.open, true);
  assert.equal(owners.rows, 2);
  assert(owners.ownerNames.includes("researcher"), "slurm owner is aggregated");
  assert.equal(owners.researcherRowCount, 1, "researcher aggregates into one row");
  assert.equal(owners.ownerNames[0], "researcher");
  assert.equal(owners.researcher.nameTitle, "researcher");
  // researcher: 4 GPUs on 2 nodes, 4 distinct host+pid processes.
  assert.deepEqual(owners.researcher.metrics, [4, 2, 4]);
  assert.equal(owners.researcher.vram, "262.8 GiB");
  assert.deepEqual(owners.researcher.hostChips, ["atlas-01", "atlas-02"]);
  assert.match(owners.firstVram, /GiB/);
  // Unattributed: the shared data-worker PID counts once per host and the
  // unknown-memory probe keeps the VRAM sum an explicit lower bound.
  assert.deepEqual(owners.unattributed.metrics, [4, 2, 3]);
  assert.equal(owners.unattributed.vram, "至少 2 GiB");
  assert.match(owners.summary, /共占用至少 264\.8 GiB/);
  assert.match(owners.summary, /部分进程显存未知/);
  assert(owners.centerDelta < 2);
  // The stale atlas-03 keeps last-success processes but is offline: it stays
  // out of "current" owners, disclosed by the footnote. "\u53f0\u79bb\u7ebf
  // \u8282\u70b9\u672a\u8ba1\u5165" = offline hosts excluded; "\u6570\u636e
  // \u622a\u81f3" = data-cutoff wording from processes_observed_at.
  assert.match(owners.offlineNote, /1 \u53F0\u79BB\u7EBF\u8282\u70B9\u672A\u8BA1\u5165/);
  assert.match(owners.summary, /\u6570\u636E\u622A\u81F3/);
  // Unattributed card hints at the opt-in identity layer (title only).
  assert.match(owners.unattributed.cardTitle, /workloads\.mode=identity/);

  const ownersUsage = await cdp.evaluate(`(async () => {
    document.querySelector("#owners-toggle").click();
    const deadline = Date.now() + 3000;
    while (
      !document.querySelectorAll("#owners-usage-results .capacity-candidate").length
      && Date.now() < deadline
    ) await new Promise((resolve) => setTimeout(resolve, 25));
    const rows = [...document.querySelectorAll(
      "#owners-usage-results .capacity-candidate",
    )];
    const result = {
      summary: document.querySelector("#owners-usage-summary")?.textContent || "",
      owners: rows.map((row) => row.querySelector("strong")?.textContent),
      gpuHours: rows.map((row) => row.querySelector("em")?.textContent),
      idleLabels: rows.map((row) =>
        [...row.querySelectorAll(".capacity-candidate-metrics span")]
          .at(-1)?.textContent,
      ),
    };
    document.querySelector("#owners-dialog").close();
    return result;
  })()`, true);
  assert.deepEqual(ownersUsage.owners, ["researcher", "未归属"]);
  assert.match(ownersUsage.summary, /2 个归属方/);
  assert(ownersUsage.gpuHours.every((label) => /卡·时/.test(label)));
  assert(ownersUsage.idleLabels.every((label) => /闲置占比/.test(label)));

  // Owner host chips drill down into the fleet view: clicking closes the
  // dialog and selects the host through the regular selection path.
  const ownersDrilldown = await cdp.evaluate(`(() => {
    document.querySelector("#owners-toggle").click();
    const dialog = document.querySelector("#owners-dialog");
    const chip = [...document.querySelectorAll("#owners-results .owner-host-chip")]
      .find((node) => node.textContent === "atlas-01");
    const chipIsButton = chip?.tagName === "BUTTON";
    chip?.click();
    const result = {
      chipFound: Boolean(chip),
      chipIsButton,
      dialogOpenAfterClick: dialog.open,
      selectedHost: view.selectedHost,
      locationHash: window.location.hash,
    };
    selectHost("all");
    return result;
  })()`);
  assert.equal(ownersDrilldown.chipFound, true, "owners view renders host chips");
  assert.equal(
    ownersDrilldown.chipIsButton,
    true,
    "owner host chip is a keyboard-reachable button",
  );
  assert.equal(
    ownersDrilldown.dialogOpenAfterClick,
    false,
    "owner host chip closes the owners dialog",
  );
  assert.equal(
    ownersDrilldown.selectedHost,
    "atlas-01",
    "owner host chip selects the corresponding host",
  );
  assert.equal(ownersDrilldown.locationHash, "#atlas-01");

  // Incident detail offers a direct path to the host's maintenance window:
  // the shortcut closes the diagnosis dialog, opens settings and lands on the
  // expanded maintenance editor of that host.
  const incidentMaintenance = await cdp.evaluate(`(async () => {
    const condition = (view.incidents?.active || []).find(
      (item) => item.host === "atlas-03" && item.category === "connectivity",
    );
    if (!condition) return { conditionFound: false };
    openIncidentDetail(condition);
    const incidentDialog = document.querySelector("#incident-detail-dialog");
    const button = document.querySelector("#incident-open-maintenance");
    const hint = document.querySelector(".incident-action-hint");
    const result = {
      conditionFound: true,
      incidentDialogOpen: incidentDialog.open,
      buttonVisible: Boolean(button) && !button.hidden && !button.disabled,
      buttonLabel: button?.textContent || "",
      hintText: hint?.textContent || "",
    };
    button.click();
    const settingsDialog = document.querySelector("#settings-dialog");
    let row = null;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      row = [...document.querySelectorAll("#configured-host-list .inventory-host")]
        .find((item) => item.dataset.host === "atlas-03");
      if (row && row.querySelector(".maintenance-editor")) break;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    result.settingsOpen = settingsDialog.open;
    result.incidentDialogClosedAfter = !incidentDialog.open;
    result.rowFound = Boolean(row);
    result.editorExpanded = Boolean(row?.querySelector(".maintenance-editor"));
    const rowRect = row ? row.getBoundingClientRect() : null;
    const dialogRect = settingsDialog.getBoundingClientRect();
    result.rowVisible = Boolean(rowRect)
      && rowRect.height > 0
      && rowRect.bottom > dialogRect.top
      && rowRect.top < dialogRect.bottom;
    result.focusInRow = Boolean(row) && row.contains(document.activeElement);
    view.maintenanceEditingHost = null;
    view.maintenanceFocusHost = null;
    renderInventory();
    settingsDialog.close();
    return result;
  })()`, true);
  assert.equal(
    incidentMaintenance.conditionFound,
    true,
    "atlas-03 keeps an active connectivity incident",
  );
  assert.equal(incidentMaintenance.incidentDialogOpen, true);
  assert.equal(
    incidentMaintenance.buttonVisible,
    true,
    "incident detail offers a maintenance-window shortcut",
  );
  // "\u8bbe\u7f6e\u7ef4\u62a4\u7a97\u53e3" = set maintenance window.
  assert.equal(
    incidentMaintenance.buttonLabel,
    "\u8BBE\u7F6E\u7EF4\u62A4\u7A97\u53E3",
  );
  // Static legend: "\u786e\u8ba4\uff1d..." = acknowledge keeps the alert
  // count but marks it as noted; "\u9759\u9ed8\uff1d..." = silence stops
  // pending-count and notifications for the chosen period.
  assert.equal(
    incidentMaintenance.hintText,
    "\u786E\u8BA4\uFF1D\u4FDD\u7559\u544A\u8B66\u8BA1\u6570\u4F46\u6807\u8BB0"
      + "\u5DF2\u77E5\u6653\uFF1B\u9759\u9ED8\uFF1D\u5728\u671F\u9650\u5185"
      + "\u4E0D\u518D\u8BA1\u5165\u5F85\u5904\u7406\u4E0E\u901A\u77E5",
    "incident actions carry the acknowledge/silence legend",
  );
  assert.equal(
    incidentMaintenance.settingsOpen,
    true,
    "maintenance shortcut opens the settings dialog",
  );
  assert.equal(
    incidentMaintenance.incidentDialogClosedAfter,
    true,
    "maintenance shortcut closes the incident dialog",
  );
  assert.equal(incidentMaintenance.rowFound, true);
  assert.equal(
    incidentMaintenance.editorExpanded,
    true,
    "maintenance editor pre-expands for the incident host",
  );
  assert.equal(
    incidentMaintenance.rowVisible,
    true,
    "target host row is visible inside the settings dialog",
  );
  assert.equal(
    incidentMaintenance.focusInRow,
    true,
    "focus lands in the maintenance editor of the target host",
  );

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
      order: [...document.querySelectorAll(".server-item[data-host]:not([data-host='all'])")].map(
        (item) => item.dataset.host,
      ),
    };
  })()`);
  assert.deepEqual(grouping.headings, ["Lab", "Training"]);
  assert.deepEqual(grouping.groups, ["Lab", "Lab", "Training", "Training"]);
  assert.deepEqual(grouping.order, ["atlas-03", "atlas-06", "atlas-01", "atlas-02"]);

  // Zero configured nodes must not read as "all servers healthy". The
  // fixture always has nodes, so exercise the state on a synthetic snapshot.
  const emptyFleet = await cdp.evaluate(`(() => {
    const originalSnapshot = view.snapshot;
    view.snapshot = {
      ...originalSnapshot,
      servers: [],
      stats: {
        ...originalSnapshot.stats,
        servers: 0, onlineServers: 0, issueServers: 0,
        actionableIssueServers: 0, incidentServers: 0,
        actionableIncidentServers: 0, maintenanceServers: 0,
        staleServers: 0, pollingServers: 0,
        activeIncidents: 0, criticalIncidents: 0,
        actionableIncidents: 0, actionableCriticalIncidents: 0,
        gpus: 0, busyGpus: 0, memoryTotalMiB: 0, memoryUsedMiB: 0,
        cpuAveragePct: null, cpuCores: 0,
        systemMemoryTotalMiB: 0, systemMemoryUsedMiB: 0,
      },
    };
    render();
    const health = document.querySelector("#server-health")?.textContent || "";
    const detail = document.querySelector("#server-detail")?.textContent || "";
    const guide = document.querySelector("#server-detail .empty-fleet-action");
    let settingsOpened = false;
    if (guide) {
      guide.click();
      settingsOpened = document.querySelector("#settings-dialog").open;
      document.querySelector("#settings-dialog").close();
    }
    view.snapshot = originalSnapshot;
    render();
    return {
      health,
      detail,
      hasGuide: Boolean(guide),
      settingsOpened,
      restoredHealth: document.querySelector("#server-health")?.textContent || "",
    };
  })()`);
  // "\u672a\u914d\u7f6e" = unconfigured badge; the detail line reads
  // "\u5c1a\u672a\u914d\u7f6e\u76d1\u63a7\u8282\u70b9" with a guide button.
  assert.equal(emptyFleet.health, "\u672A\u914D\u7F6E");
  assert.match(emptyFleet.detail, /\u5C1A\u672A\u914D\u7F6E\u76D1\u63A7\u8282\u70B9/);
  assert.equal(emptyFleet.hasGuide, true);
  assert.equal(emptyFleet.settingsOpened, true, "guide button opens settings scan");
  assert.notEqual(emptyFleet.restoredHealth, "\u672A\u914D\u7F6E");

  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await new Promise((resolve) => setTimeout(resolve, 200));

  const personalization = await cdp.evaluate(`(async () => {
    const serverItems = [...document.querySelectorAll(".server-item[data-host]:not([data-host='all'])")];
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
    const reordered = [...document.querySelectorAll(".server-item[data-host]:not([data-host='all'])")].map(
      (item) => item.dataset.host,
    );
    document.querySelector(".server-item[data-host='atlas-02']").focus();
    document.querySelector(".server-item[data-host='atlas-02']").dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "ArrowDown", altKey: true, bubbles: true, cancelable: true,
      }),
    );
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const keyboardOrder = [...document.querySelectorAll(".server-item[data-host]:not([data-host='all'])")].map(
      (item) => item.dataset.host,
    );
    const keyboardFocus = document.activeElement?.dataset.host || "";

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
    const planBadgeNode = document.querySelector("#configured-host-list .maintenance-plan-badge");
    const planBadge = planBadgeNode?.textContent || "";
    const planBadgeInactive = Boolean(planBadgeNode?.classList.contains("inactive"));
    const planBadgeTitle = planBadgeNode?.title || "";
    const endpointsHidden = document.querySelector("#notification-endpoints").hidden;
    const endpointRows = [...document.querySelectorAll(
      "#notification-endpoints .notification-endpoint",
    )].map((row) => ({
      name: row.querySelector("strong")?.textContent || "",
      state: row.querySelector("em")?.textContent || "",
      stateClass: row.querySelector("em")?.className || "",
      meta: row.querySelector("small")?.textContent || "",
    }));
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
      reordered, keyboardOrder, keyboardFocus,
      savedServerSort: JSON.parse(localStorage.getItem("mocop.preferences.v1")).serverSort,
      savedVisualStyle: JSON.parse(localStorage.getItem("mocop.preferences.v1")).visualStyle,
      savedAccent: JSON.parse(localStorage.getItem("mocop.preferences.v1")).accent,
      savedDensity: JSON.parse(localStorage.getItem("mocop.preferences.v1")).density,
      savedBackgroundVisibility: JSON.parse(localStorage.getItem("mocop.preferences.v1")).backgroundVisibility,
      savedServerFilter: JSON.parse(localStorage.getItem("mocop.preferences.v1")).serverFilter,
      pressedFleetFilters: [...document.querySelectorAll(".fleet-filter[aria-pressed='true']")]
        .map((button) => button.dataset.serverFilter),
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
      planBadge,
      planBadgeInactive,
      planBadgeTitle,
      endpointsHidden,
      endpointRows,
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
      taskNoteTitle: document.querySelector("#gpu-task-note")?.title || "",
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
    result.boundedTaskNoteTitle = document.querySelector("#gpu-task-note").title;
    const taskSearch = document.querySelector("#gpu-task-search");
    taskSearch.value = "process-100.py";
    taskSearch.dispatchEvent(new Event("input", { bubbles: true }));
    result.boundedFilteredCount = document.querySelector("#gpu-task-count").textContent;
    result.boundedFilteredRows = document.querySelectorAll("#gpu-task-list .gpu-task").length;
    result.boundedFilteredKey = document.querySelector(
      "#gpu-task-list .gpu-task",
    )?.dataset.processKey || "";
    taskSearch.value = "";
    taskSearch.dispatchEvent(new Event("input", { bubbles: true }));
    selectedRecord.gpu.processes = originalProcesses;
    renderGpuDetail();
    taskDialog.close();
    return result;
  })()`, true);
  assert.equal(personalization.utilizationVisible, true);
  assert.equal(personalization.reordered[0], "atlas-02");
  assert.equal(personalization.keyboardOrder[1], "atlas-02");
  assert.equal(personalization.keyboardFocus, "atlas-02");
  assert.equal(personalization.savedServerSort, "custom");
  assert.equal(personalization.savedVisualStyle, "glass");
  assert.equal(personalization.savedAccent, "rose");
  assert.equal(personalization.savedDensity, "compact");
  assert.equal(personalization.savedBackgroundVisibility, 52);
  assert.equal(personalization.savedServerFilter, "busy");
  assert.deepEqual(personalization.pressedFleetFilters, ["busy"]);
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
  assert.equal(personalization.maintenanceWindows["atlas-01"].active, true);
  assert.equal(personalization.hostGroups["atlas-01"], "Priority");
  // atlas-02 carries a planned recurring window (active: false): the badge
  // "\u6bcf\u5468\u7ef4\u62a4\u8ba1\u5212" renders grayed with the next
  // window in its title, while the live "\u7ef4\u62a4\u81f3" badge stays
  // reserved for active windows.
  assert.equal(personalization.planBadge, "\u6BCF\u5468\u7EF4\u62A4\u8BA1\u5212");
  assert.equal(personalization.planBadgeInactive, true);
  assert.match(personalization.planBadgeTitle, /Weekly firmware inspection/);
  // "\u4e0b\u6b21\u7a97\u53e3\u81f3" = next-window-until prefix.
  assert.match(personalization.planBadgeTitle, /\u4E0B\u6B21\u7A97\u53E3\u81F3/);
  // Per-endpoint webhook state from snapshot.notifications.endpoints.
  assert.equal(personalization.endpointsHidden, false);
  assert.equal(personalization.endpointRows.length, 2);
  assert.equal(personalization.endpointRows[0].name, "ops-webhook");
  assert.match(personalization.endpointRows[0].stateClass, /success/);
  // "\u5f85\u53d1" = queued deliveries; "\u7d2f\u8ba1\u5931\u8d25" = dropped.
  assert.match(personalization.endpointRows[0].meta, /\u5F85\u53D1 2/);
  assert.match(personalization.endpointRows[0].meta, /\u6700\u8FD1\u6210\u529F/);
  assert.equal(personalization.endpointRows[1].name, "sms-bridge");
  assert.match(personalization.endpointRows[1].stateClass, /error/);
  assert.equal(personalization.endpointRows[1].state, "HTTP 503 from relay");
  assert.match(personalization.endpointRows[1].meta, /\u7D2F\u8BA1\u5931\u8D25 3/);
  assert.match(personalization.maintenanceBadge, /维护至/);
  assert.equal(personalization.gpuSort, "memory");
  assert.equal(personalization.powerHidden, true);
  assert.equal(personalization.taskDialogOpen, true);
  assert(personalization.taskDialogCenterDelta < 2);
  // Freshness title explains the attended sampling cadence
  // ("\u65e0\u4eba\u67e5\u770b\u65f6\u91c7\u6837\u81ea\u52a8\u653e\u7f13");
  // the identity hint only appears when no process carries workload metadata.
  assert.match(
    personalization.taskNoteTitle,
    /\u65E0\u4EBA\u67E5\u770B\u65F6\u91C7\u6837\u81EA\u52A8\u653E\u7F13/,
  );
  assert(!personalization.taskNoteTitle.includes("workloads.mode=identity"));
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
  assert.match(personalization.boundedTaskNoteTitle, /workloads\.mode=identity/);
  assert.match(personalization.boundedTaskNote, /101 个进程/);
  assert.match(personalization.boundedTaskNote, /最高的 100 个/);
  assert.equal(personalization.boundedFilteredCount, "1 / 101");
  assert.equal(personalization.boundedFilteredRows, 1);
  assert.match(personalization.boundedFilteredKey, /^30100\|/);
  assert.match(personalization.healthMetrics, /硬件健康正常/);
  assert.match(personalization.gpuHistoryRange, /2 个样本/);
  assert.equal(personalization.gpuHistoryCards, 4);
  assert.match(personalization.gpuTimelineText, /暂未记录/);
  assert.equal(personalization.heatmapLegend, false);
  assert.equal(personalization.styleChoiceCount, 6);
  assert.equal(personalization.accentChoiceCount, 6);
  assert.equal(personalization.restartDisabled, true);
  assert.match(personalization.restartStatus, /不支持网页重启/);

  const gpuTasks = await cdp.evaluate(`(async () => {
    const server1 = view.snapshot.servers.find((item) => item.host === "atlas-01");
    const gpu1 = server1.gpus.find((item) => item.index === 0);
    selectHost("atlas-01");
    const inventoryProcessText = [...document.querySelectorAll(
      ".gpu-table-body tr[data-gpu-id]",
    )].find((row) => row.dataset.gpuId === String(gpu1.uuid || gpu1.index))
      ?.querySelector(".gpu-process-cell")?.textContent || "";
    openGpuDetail(server1, gpu1);
    const taskDialog = document.querySelector("#gpu-detail-dialog");
    const processFirst = Boolean(
      document.querySelector(".gpu-task-workspace")?.compareDocumentPosition(
        document.querySelector(".gpu-history-section"),
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    );
    const insightText = document.querySelector("#gpu-task-insights")?.textContent || "";
    const identityFilters = [...document.querySelectorAll("[data-gpu-task-filter]")]
      .map((button) => button.dataset.gpuTaskFilter);
    const processCsv = buildCsv([{ server: server1, gpu: gpu1 }]);
    taskDialog.close();
    await new Promise((resolve) => setTimeout(resolve, 20));
    selectHost("all");
    const processSortControl = document.querySelector("#gpu-sort");
    processSortControl.value = "processes";
    processSortControl.dispatchEvent(new Event("change", { bubbles: true }));
    const processSortedFirst = document.querySelector(".gpu-table-body tr[data-gpu-id]");
    const processSortedPlacement = processSortedFirst
      ? processSortedFirst.dataset.host + "|" + processSortedFirst.dataset.gpuId : "";
    document.querySelector('[data-filter="processes"]').click();
    const processFilterRows = [...document.querySelectorAll(
      ".gpu-table-body tr[data-gpu-id]",
    )];
    const processFilterCount = processFilterRows.length;
    const processFilterHasZero = processFilterRows.some(
      (row) => /0 个进程|不可用/.test(row.querySelector(".gpu-process-cell")?.textContent || ""),
    );
    document.querySelector('[data-filter="all"]').click();
    processSortControl.value = "memory";
    processSortControl.dispatchEvent(new Event("change", { bubbles: true }));
    selectHost("atlas-01");
    openGpuDetail(server1, gpu1);
    const rowKeys = () => [...document.querySelectorAll("#gpu-task-list .gpu-task")].map(
      (row) => row.dataset.processKey,
    );
    const rowDuration = (pid) => document.querySelector(
      '#gpu-task-list .gpu-task[data-process-key^="' + pid + '|"] .gpu-task-duration',
    );
    const memoryOrder = rowKeys();
    const trainDuration = rowDuration("10000")?.textContent || "";
    const workerDuration = rowDuration("20000")?.textContent || "";
    const workerDurationTitle = rowDuration("20000")?.title || "";
    const commandNode = document.querySelector(
      '#gpu-task-list .gpu-task[data-process-key^="10000|"] .gpu-task-command',
    );
    const command = commandNode?.textContent || "";
    const commandTitle = commandNode?.title || "";
    const commandHidden = commandNode ? commandNode.hidden : true;
    const firstRowBefore = document.querySelector("#gpu-task-list .gpu-task");
    const barBefore = firstRowBefore.querySelector(".mini-track i");
    renderGpuDetail();
    const firstRowAfter = document.querySelector("#gpu-task-list .gpu-task");
    const rowReused = firstRowBefore === firstRowAfter
      && barBefore === firstRowAfter.querySelector(".mini-track i");
    document.querySelector('[data-gpu-task-filter="unowned"]').click();
    const unownedOrder = rowKeys();
    document.querySelector('[data-gpu-task-filter="owned"]').click();
    const ownedOrder = rowKeys();
    document.querySelector('[data-gpu-task-filter="all"]').click();
    const ownerChip = [...document.querySelectorAll(".gpu-task-workload button")]
      .find((button) => button.textContent.includes("researcher"));
    ownerChip.click();
    const ownerChipQuery = document.querySelector("#gpu-task-search").value;
    const ownerChipOrder = rowKeys();
    document.querySelector("#gpu-task-search").value = "";
    document.querySelector("#gpu-task-search").dispatchEvent(
      new Event("input", { bubbles: true }),
    );
    let copiedText = "";
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value) => { copiedText = String(value); } },
    });
    document.querySelector(
      '#gpu-task-list .gpu-task[data-process-key^="10000|"] .gpu-task-action:last-child',
    ).click();
    await Promise.resolve();
    await Promise.resolve();
    const copyFeedback = document.querySelector("#gpu-task-feedback").textContent;
    const taskSearch = document.querySelector("#gpu-task-search");
    taskSearch.value = "researcher";
    taskSearch.dispatchEvent(new Event("input", { bubbles: true }));
    const ownerSearchOrder = rowKeys();
    const ownerSearchCount = document.querySelector("#gpu-task-count").textContent;
    taskSearch.value = "";
    taskSearch.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector('.gpu-task-sort [data-task-sort="name"]').click();
    const nameOrder = rowKeys();
    const nameButtonActive = document.querySelector(
      '.gpu-task-sort [data-task-sort="name"]',
    ).classList.contains("active");
    // Only judge the fresh-note styling while the fixture data is still
    // comfortably inside the 90-second freshness window.
    const freshAgeMs = Date.now() - Date.parse(gpu1.processes_observed_at);
    const freshNoteStale = freshAgeMs < 60_000
      ? document.querySelector("#gpu-task-note").classList.contains("gpu-task-freshness-stale")
      : null;
    document.querySelector('.gpu-task-sort [data-task-sort="duration"]').click();
    const durationOrder = rowKeys();
    const durationButtonActive = document.querySelector(
      '.gpu-task-sort [data-task-sort="duration"]',
    ).classList.contains("active");
    const savedTaskSort = JSON.parse(
      localStorage.getItem("mocop.preferences.v1"),
    ).gpuTaskSort;
    const taskNote = document.querySelector("#gpu-task-note").textContent;
    const server2 = view.snapshot.servers.find((item) => item.host === "atlas-02");
    openGpuDetail(server2, server2.gpus.find((item) => item.index === 0));
    const staleNote = document.querySelector("#gpu-task-note")
      .classList.contains("gpu-task-freshness-stale");
    openGpuDetail(server2, server2.gpus.find((item) => item.index === 2));
    const unavailableCount = document.querySelector("#gpu-task-count").textContent;
    const unavailableText = document.querySelector("#gpu-task-list").textContent;
    taskDialog.close();
    selectHost("all");
    return {
      inventoryProcessText, processFirst, insightText, identityFilters,
      processCsv,
      processSortedPlacement, processFilterCount, processFilterHasZero,
      unownedOrder, ownedOrder, ownerChipQuery, ownerChipOrder,
      copiedText, copyFeedback,
      memoryOrder, trainDuration, workerDuration, workerDurationTitle,
      command, commandTitle, commandHidden, rowReused, freshNoteStale,
      ownerSearchOrder, ownerSearchCount, nameOrder, nameButtonActive,
      durationOrder, durationButtonActive, savedTaskSort, taskNote,
      staleNote, unavailableCount, unavailableText,
    };
  })()`, true);
  assert.match(gpuTasks.inventoryProcessText, /2/);
  assert.match(gpuTasks.inventoryProcessText, /train\.py/);
  assert.equal(gpuTasks.processFirst, true);
  assert.match(gpuTasks.insightText, /2/);
  assert.deepEqual(gpuTasks.identityFilters, ["all", "owned", "unowned"]);
  assert.match(gpuTasks.processCsv, /"进程数"/);
  assert.match(gpuTasks.processCsv, /"进程已知分配显存 MiB"/);
  assert.match(gpuTasks.processCsv, /"2","72192","2\/2","sampled"/);
  assert.match(gpuTasks.processSortedPlacement, /^atlas-02\|GPU-DEMO-atlas-02-01$/);
  assert.equal(gpuTasks.processFilterCount, 4);
  assert.equal(gpuTasks.processFilterHasZero, false);
  assert.deepEqual(gpuTasks.unownedOrder, ["20000|python data_worker.py"]);
  assert.deepEqual(gpuTasks.ownedOrder, ["10000|/workspace/train.py"]);
  assert.equal(gpuTasks.ownerChipQuery, "researcher");
  assert.deepEqual(gpuTasks.ownerChipOrder, ["10000|/workspace/train.py"]);
  assert.match(gpuTasks.copiedText, /python train\.py --config/);
  assert.match(gpuTasks.copyFeedback, /已复制完整命令/);
  assert.equal(gpuTasks.memoryOrder[0], "10000|/workspace/train.py");
  // "\u8fd0\u884c" = run-prefix, "\u5c0f\u65f6" = hours unit.
  assert.match(gpuTasks.trainDuration, /^\u8fd0\u884c /);
  assert.match(gpuTasks.trainDuration, /\u5c0f\u65f6/);
  // "\u5df2\u89c2\u6d4b" = observed-prefix for first_seen_at-only processes.
  assert.match(gpuTasks.workerDuration, /^\u5df2\u89c2\u6d4b /);
  assert.match(gpuTasks.workerDuration, /\u5c0f\u65f6/);
  assert.equal(
    gpuTasks.workerDurationTitle,
    "\u81ea\u76d1\u63a7\u9996\u6b21\u89c2\u6d4b\u8d77\uff0c\u670d\u52a1\u91cd\u542f\u540e\u91cd\u65b0\u8ba1\u7b97",
  );
  assert.equal(gpuTasks.commandHidden, false);
  assert.match(gpuTasks.command, /--config configs\/llm-70b\.yaml/);
  assert.equal(
    gpuTasks.commandTitle,
    "python train.py --config configs/llm-70b.yaml --stage sft",
  );
  assert.equal(gpuTasks.rowReused, true);
  assert.deepEqual(gpuTasks.ownerSearchOrder, ["10000|/workspace/train.py"]);
  assert.equal(gpuTasks.ownerSearchCount, "1 / 2");
  assert.equal(gpuTasks.nameOrder[0], "20000|python data_worker.py");
  assert.equal(gpuTasks.nameButtonActive, true);
  if (gpuTasks.freshNoteStale !== null) {
    assert.equal(gpuTasks.freshNoteStale, false);
  }
  assert.equal(gpuTasks.durationOrder[0], "20000|python data_worker.py");
  assert.equal(gpuTasks.durationButtonActive, true);
  assert.equal(gpuTasks.savedTaskSort, "duration");
  // "\u6309\u8fd0\u884c\u65f6\u957f" = sorted-by-runtime note wording.
  assert.match(gpuTasks.taskNote, /\u6309\u8fd0\u884c\u65f6\u957f/);
  assert.equal(gpuTasks.staleNote, true);
  assert.equal(gpuTasks.unavailableCount, "\u2014");
  // "\u4efb\u52a1\u6570\u636e\u6682\u4e0d\u53ef\u7528" = tasks unavailable notice.
  assert.match(gpuTasks.unavailableText, /\u4efb\u52a1\u6570\u636e\u6682\u4e0d\u53ef\u7528/);

  const gpuTaskFleetSearch = await cdp.evaluate(`(async () => {
    const server = view.snapshot.servers.find((item) => item.host === "atlas-01");
    openGpuDetail(server, server.gpus.find((item) => item.index === 0));
    document.querySelector(
      '#gpu-task-list .gpu-task[data-process-key^="10000|"] .gpu-task-action',
    ).click();
    await new Promise((resolve) => setTimeout(resolve, 50));
    const result = {
      dialogOpen: document.querySelector("#gpu-detail-dialog").open,
      selectedHost: view.selectedHost,
      query: document.querySelector("#search").value,
      resultCount: document.querySelector("#program-search-count").textContent,
      searchFocused: document.activeElement === document.querySelector("#search"),
    };
    document.querySelector("#search").value = "";
    document.querySelector("#search").dispatchEvent(new Event("input", { bubbles: true }));
    return result;
  })()`, true);
  assert.equal(gpuTaskFleetSearch.dialogOpen, false);
  assert.equal(gpuTaskFleetSearch.selectedHost, "all");
  assert.equal(gpuTaskFleetSearch.query, "train.py");
  assert.equal(gpuTaskFleetSearch.resultCount, "5");
  assert.equal(gpuTaskFleetSearch.searchFocused, true);

  const resilience = await cdp.evaluate(`(async () => {
    // Dialog close events fire from queued tasks; flush any close pending
    // from earlier steps before reopening, or it would wipe this test state.
    document.querySelector("#gpu-detail-dialog").close();
    await new Promise((resolve) => setTimeout(resolve, 50));
    const server = view.snapshot.servers.find((item) => item.host === "atlas-01");
    const gpu = server.gpus.find((item) => item.index === 0);
    openGpuDetail(server, gpu);
    // Supersede the in-flight load with a request the service rejects (404):
    // the failure must render as a failure and schedule a bounded retry.
    syncGpuHistory({
      server: { host: "atlas-01", lastSuccessAt: "2031-01-01T00:00:00Z" },
      gpu: { uuid: "GPU-DEMO-MISSING", index: 9 },
    });
    // Mark the real record as already loaded so the per-second snapshot
    // renders cannot start a competing fetch that would supersede the
    // failing request this test observes.
    view.gpuHistoryKey = server.host + "|" + gpu.uuid + "|" + (server.lastSuccessAt || "");
    for (
      let attempt = 0;
      attempt < 80 && (!view.gpuHistoryError || view.gpuHistoryLoading);
      attempt += 1
    ) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const errorFlag = view.gpuHistoryError;
    const retryScheduled = view.gpuHistoryRetryTimer != null;
    const retryDelayMs = view.gpuHistoryRetryDelayMs;
    // Re-render synchronously so the DOM reads cannot race the per-second
    // snapshot renders that may refetch the real history.
    renderGpuHistory();
    const failureText = document.querySelector("#gpu-history-grid")?.textContent || "";
    const timelineText = document.querySelector("#gpu-process-timeline")?.textContent || "";
    document.querySelector("#gpu-detail-dialog").close();
    // The dialog close event is dispatched from a queued task.
    await new Promise((resolve) => setTimeout(resolve, 50));
    const cleanedUp = view.gpuHistoryRetryTimer == null
      && view.gpuTaskRowCache.size === 0
      && document.querySelector("#gpu-task-list").children.length === 0
      && document.querySelector("#gpu-history-grid").children.length === 0;

    view.incidentSyncFailed = true;
    renderAttention();
    const attentionError = document.querySelector(
      "#attention-list .attention-sync-error",
    )?.textContent || "";
    const attentionVisible = !document.querySelector("#attention-panel").hidden;
    view.incidentSyncFailed = false;
    renderAttention();
    const attentionErrorCleared = !document.querySelector(
      "#attention-list .attention-sync-error",
    );

    selectHost("atlas-01");
    const tile = document.querySelector("#resource-grid .resource-tile");
    render();
    const panelReused = tile != null
      && tile === document.querySelector("#resource-grid .resource-tile");
    selectHost("all");
    return {
      failureText, timelineText, errorFlag, retryScheduled, retryDelayMs,
      cleanedUp, attentionError, attentionVisible, attentionErrorCleared,
      panelReused,
    };
  })()`, true);
  // "\u5386\u53f2\u8bfb\u53d6\u5931\u8d25\uff0c\u7a0d\u540e\u91cd\u8bd5"
  // = history load failed, retrying soon (not the fake "no samples" copy).
  assert.match(resilience.failureText, /\u5386\u53F2\u8BFB\u53D6\u5931\u8D25\uFF0C\u7A0D\u540E\u91CD\u8BD5/);
  assert.match(resilience.timelineText, /\u5386\u53F2\u8BFB\u53D6\u5931\u8D25/);
  assert.equal(resilience.errorFlag, true);
  assert.equal(resilience.retryScheduled, true, "failed gpu history schedules a retry");
  assert.equal(resilience.retryDelayMs, 4000);
  assert.equal(resilience.cleanedUp, true, "closing the dialog clears caches and retry");
  // "\u544a\u8b66\u8be6\u60c5\u52a0\u8f7d\u5931\u8d25\uff0c\u6b63\u5728\u91cd\u8bd5"
  // = alert-details load failed, retrying (attention panel stays visible).
  assert.match(resilience.attentionError, /\u544A\u8B66\u8BE6\u60C5\u52A0\u8F7D\u5931\u8D25\uFF0C\u6B63\u5728\u91CD\u8BD5/);
  assert.equal(resilience.attentionVisible, true);
  assert.equal(resilience.attentionErrorCleared, true);
  assert.equal(
    resilience.panelReused,
    true,
    "selected-host resource panel skips rebuild without a data change",
  );

  const reloaded = cdp.waitFor("Page.loadEventFired", 30_000);
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

  const persistedTaskSort = await cdp.evaluate(`(async () => {
    for (let attempt = 0; attempt < 200 && !view.snapshot; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const server = view.snapshot.servers.find((item) => item.host === "atlas-01");
    openGpuDetail(server, server.gpus.find((item) => item.index === 0));
    const result = {
      saved: JSON.parse(localStorage.getItem("mocop.preferences.v1")).gpuTaskSort,
      activeChoice: document.querySelector(".gpu-task-sort .active")?.dataset.taskSort,
      firstRowKey: document.querySelector("#gpu-task-list .gpu-task")?.dataset.processKey,
    };
    document.querySelector("#gpu-detail-dialog").close();
    return result;
  })()`, true);
  assert.equal(persistedTaskSort.saved, "duration");
  assert.equal(persistedTaskSort.activeChoice, "duration");
  assert.equal(persistedTaskSort.firstRowKey, "20000|python data_worker.py");

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
    selectHost("all");
    const search = document.querySelector("#search");
    search.value = "train.py";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    const programSearchColumns = getComputedStyle(
      document.querySelector("#program-search-results"),
    ).gridTemplateColumns.split(" ").length;
    const programSearchDocumentOverflow = document.documentElement.scrollWidth
      > document.documentElement.clientWidth;
    search.value = "";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#settings-toggle").click();
    const rect = document.querySelector("#settings-dialog").getBoundingClientRect();
    const result = {
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      programSearchColumns,
      programSearchDocumentOverflow,
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
    result.topologyScrollOverflow = topologyScroll.scrollWidth > topologyScroll.clientWidth;
    topologyScroll.scrollLeft = topologyScroll.scrollWidth;
    result.topologyScrolledToEnd = topologyScroll.scrollLeft > 0
      && topologyScroll.scrollLeft + topologyScroll.clientWidth
        >= topologyScroll.scrollWidth - 1;
    topologyScroll.scrollLeft = 0;
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
  assert.equal(mobile.programSearchColumns, 1);
  assert.equal(mobile.programSearchDocumentOverflow, false);
  assert(mobile.gpuMemoryWidth > mobile.gridWidth * 0.9);
  assert(mobile.settingsCenterDelta < 2);
  assert(mobile.topologyCenterDelta < 2);
  assert(mobile.topologyWidth <= 390);
  assert.equal(mobile.topologyScrollOverflow, true, "fixture topology overflows on mobile");
  assert.equal(mobile.topologyScrolledToEnd, true, "topology scroll area actually scrolls");
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
  // Service metadata endpoint (new contract) plus the end-to-end viewer
  // marker audit: every dashboard-initiated read of the level-triggered API
  // paths must have carried X-Monitor-Request, or SSE-outage polling would
  // drop the page back to the unattended sampling cadence.
  const meta = await (
    await fetch(`http://127.0.0.1:${monitorPort}/api/meta`)
  ).json();
  assert.equal(meta.apiVersion, "2");
  assert.equal(typeof meta.appVersion, "string");
  assert.equal(typeof meta.schemaVersion, "number");
  assert.equal(meta.capabilities.restartSupported, false);
  assert(Array.isArray(meta.endpoints)
    && meta.endpoints.some((endpoint) => endpoint.path === "/api/snapshot"));
  assert.equal(
    meta.fixture.unmarkedDashboardReads,
    0,
    "all dashboard reads carry the X-Monitor-Request marker",
  );
  assert.equal(
    meta.fixture.unauthenticatedPrivateRequests,
    1,
    "only the explicit wrong-token submission reaches a private route unauthenticated",
  );
  assert.deepEqual(await cdp.send("Network.getAllCookies"), { cookies: [] });
  assert.deepEqual(cdp.errors, []);

  console.log(JSON.stringify({
    browser: "chrome", initial, final, displayName, heatmapPacking, failureMappings,
    heartbeat, topologyBenchmark, programSearchBenchmark,
    capacity, owners, ownersUsage, ownersDrilldown,
    incidentMaintenance, grouping, emptyFleet,
    personalization, gpuTasks, resilience,
    persistedAppearance, persistedTaskSort, mobile, removedBackground, meta,
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
