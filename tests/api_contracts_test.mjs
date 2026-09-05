import assert from "node:assert/strict";

await import("../src/mocop/static/api-contracts.js");

const contracts = globalThis.MocopApiContracts.create();

function rejects(fn, label) {
  assert.throws(fn, TypeError, label);
}

// --- host aliases -----------------------------------------------------------
assert.deepEqual(
  contracts.safeStoredHosts(["gpu-1", "gpu-1", "a.b_c", 42, "bad host", "", "-lead"]),
  ["gpu-1", "a.b_c"],
);
assert.deepEqual(contracts.safeStoredHosts("gpu-1"), []);

// --- snapshot envelope --------------------------------------------------------
const snapshot = {
  version: 3,
  startedAt: "2026-09-05T00:00:00Z",
  collectionStaleAfterSeconds: 15,
  thresholds: { gpu_busy_pct: 10 },
  stats: { servers: 1 },
  servers: [{ host: "gpu-1", gpus: [] }],
};
contracts.assertSnapshotEnvelope(snapshot);
for (const [label, mutate] of [
  ["null", () => null],
  ["array", () => []],
  ["float version", () => ({ ...snapshot, version: 1.5 })],
  ["missing startedAt", () => ({ ...snapshot, startedAt: undefined })],
  ["stale window as string", () => ({ ...snapshot, collectionStaleAfterSeconds: "15" })],
  ["missing thresholds", () => ({ ...snapshot, thresholds: null })],
  ["missing stats", () => ({ ...snapshot, stats: undefined })],
  ["servers not an array", () => ({ ...snapshot, servers: {} })],
  ["server without gpus", () => ({ ...snapshot, servers: [{ host: "gpu-1" }] })],
  ["server without host", () => ({ ...snapshot, servers: [{ gpus: [] }] })],
]) {
  rejects(() => contracts.assertSnapshotEnvelope(mutate()), `snapshot: ${label}`);
}

// --- incidents envelope -------------------------------------------------------
const incidents = { version: 7, active: [], events: [], correlations: [] };
contracts.assertIncidentsEnvelope(incidents);
for (const key of ["active", "events", "correlations"]) {
  rejects(() => contracts.assertIncidentsEnvelope({ ...incidents, [key]: null }), key);
}
rejects(() => contracts.assertIncidentsEnvelope({ ...incidents, version: "7" }), "version");

// --- collector settings -------------------------------------------------------
const collector = {
  pollIntervalSeconds: 5,
  probeTimeoutSeconds: 12,
  connectTimeoutSeconds: 5,
  maxWorkers: 8,
};
assert.deepEqual(contracts.normalizeCollectorSettings(collector), collector);
for (const [label, patch] of [
  ["interval below 1", { pollIntervalSeconds: 0.5 }],
  ["interval above 3600", { pollIntervalSeconds: 3601 }],
  ["probe timeout below 2", { probeTimeoutSeconds: 1 }],
  ["probe timeout above 300", { probeTimeoutSeconds: 301 }],
  ["connect timeout missing", { connectTimeoutSeconds: undefined }],
  ["connect timeout zero", { connectTimeoutSeconds: 0 }],
  ["workers fractional", { maxWorkers: 2.5 }],
  ["workers above 64", { maxWorkers: 65 }],
  ["NaN interval", { pollIntervalSeconds: Number.NaN }],
]) {
  rejects(() => contracts.normalizeCollectorSettings({ ...collector, ...patch }), label);
}
rejects(() => contracts.normalizeCollectorSettings([]), "array payload");

// --- host groups and maintenance windows ---------------------------------------
const configured = ["gpu-1", "gpu-2"];
assert.deepEqual(
  contracts.normalizeHostGroups({ "gpu-1": "Training" }, configured),
  { "gpu-1": "Training" },
);
for (const [label, payload] of [
  ["unknown host", { "gpu-9": "Training" }],
  ["empty group", { "gpu-1": "" }],
  ["untrimmed group", { "gpu-1": " Training" }],
  ["control character", { "gpu-1": "Train\u0007ing" }],
  ["too long", { "gpu-1": "x".repeat(49) }],
  ["non-string", { "gpu-1": 3 }],
]) {
  rejects(() => contracts.normalizeHostGroups(payload, configured), `groups: ${label}`);
}

const window = { until: "2030-06-15T02:00:00Z", reason: "Firmware", active: false };
assert.deepEqual(
  contracts.normalizeMaintenanceWindows({ "gpu-2": window }, configured),
  { "gpu-2": { ...window, recurring: false } },
);
assert.equal(
  contracts.normalizeMaintenanceWindows(
    { "gpu-2": { ...window, recurring: true } },
    configured,
  )["gpu-2"].recurring,
  true,
);
for (const [label, patch] of [
  ["active missing", { active: undefined }],
  ["active as string", { active: "yes" }],
  ["unknown key", { extra: 1 }],
  ["unparseable until", { until: "tomorrow" }],
  ["long reason", { reason: "r".repeat(121) }],
  ["recurring as string", { recurring: "true" }],
]) {
  rejects(
    () => contracts.normalizeMaintenanceWindows({ "gpu-2": { ...window, ...patch } }, configured),
    `maintenance: ${label}`,
  );
}
rejects(
  () => contracts.normalizeMaintenanceWindows({ "gpu-9": window }, configured),
  "maintenance: unknown host",
);

// --- inventory ---------------------------------------------------------------
const inventory = {
  configuredHosts: ["gpu-1", "gpu-2"],
  activeHosts: ["gpu-1"],
  availableHosts: ["gpu-3"],
  localHost: "gpu-1",
  autoDiscover: true,
  writable: true,
  ignoredCodeHostCount: 0,
  excludedHostCount: 1,
  collectorSettings: collector,
  maintenanceWindows: {},
  hostGroups: { "gpu-1": "Training" },
};
const normalized = contracts.normalizeInventory(inventory);
assert.deepEqual(normalized.configuredHosts, ["gpu-1", "gpu-2"]);
assert.deepEqual(normalized.infrastructureHosts, []);
assert.deepEqual(normalized.sshDiscoveryWarnings, []);
assert.equal(normalized.sshDiscoveryMode, "aliases");
assert.equal(normalized.collectorSettings.maxWorkers, 8);
for (const [label, patch] of [
  ["unsafe configured host", { configuredHosts: ["gpu-1", "bad host"] }],
  ["unsafe local host", { localHost: "bad host" }],
  ["autoDiscover not boolean", { autoDiscover: "yes" }],
  ["writable missing", { writable: undefined }],
  ["negative-count type", { excludedHostCount: "1" }],
  ["unknown discovery mode", { sshDiscoveryMode: "magic" }],
  ["unsafe infrastructure host", { infrastructureHosts: ["jump host"] }],
  ["group for unconfigured host", { hostGroups: { "gpu-9": "x" } }],
]) {
  rejects(() => contracts.normalizeInventory({ ...inventory, ...patch }), `inventory: ${label}`);
}
// Warning strings are bounded and non-strings are dropped, never thrown.
assert.equal(
  contracts.normalizeInventory({
    ...inventory,
    sshDiscoveryWarnings: [...Array(2000).fill("w"), 7],
  }).sshDiscoveryWarnings.length,
  1024,
);

// --- topology ----------------------------------------------------------------
assert.deepEqual(contracts.normalizeTopology({ root: null, links: [] }), { root: null, links: [] });
const topology = {
  root: "monitor",
  links: [
    { source: "monitor", target: "gateway", transport: "frp-stcp", label: "STCP" },
    { source: "gateway", target: "gpu-1", transport: "ssh" },
  ],
};
assert.deepEqual(contracts.normalizeTopology(topology).links[1], {
  source: "gateway", target: "gpu-1", transport: "ssh", label: null,
});
for (const [label, mutate] of [
  ["unknown transport", () => ({ ...topology, links: [{ ...topology.links[0], transport: "teleport" }] })],
  ["extra link key", () => ({ ...topology, links: [{ ...topology.links[0], weight: 1 }] })],
  ["self link", () => ({ ...topology, links: [{ source: "monitor", target: "monitor", transport: "ssh" }] })],
  ["link into root", () => ({ ...topology, links: [{ source: "gpu-1", target: "monitor", transport: "ssh" }] })],
  ["duplicate target", () => ({ ...topology, links: [...topology.links, { source: "monitor", target: "gpu-1", transport: "ssh" }] })],
  ["unreachable subtree", () => ({ ...topology, links: [{ source: "island", target: "gpu-9", transport: "ssh" }] })],
  ["untrimmed label", () => ({ ...topology, links: [{ ...topology.links[0], label: " STCP" }] })],
  ["control character label", () => ({ ...topology, links: [{ ...topology.links[0], label: "a\u0000b" }] })],
  ["too many links", () => ({ root: "monitor", links: Array.from({ length: 513 }, (_, i) => ({ source: "monitor", target: `n${i}`, transport: "ssh" })) })],
  ["missing root", () => ({ links: topology.links })],
]) {
  rejects(() => contracts.normalizeTopology(mutate()), `topology: ${label}`);
}

console.log("api contracts test passed");
