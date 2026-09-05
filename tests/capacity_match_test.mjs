import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

await import("../src/mocop/static/capacity-match.js");

const match = globalThis.MocopCapacityMatch.create();

// The fixture is the contract shared with mocop/capacity.py: every case's
// normalized ranking must come out identical from both implementations.
const fixture = JSON.parse(
  readFileSync(new URL("./fixtures/capacity_match.json", import.meta.url), "utf-8"),
);

function normalize(result) {
  return {
    excludedMaintenance: result.excludedMaintenance,
    excludedHealth: result.excludedHealth,
    candidates: result.candidates.map((candidate) => ({
      host: candidate.host,
      model: candidate.model,
      total: candidate.total,
      available: candidate.available.map((gpu) => gpu.index),
      satisfies: candidate.satisfies,
      deficit: candidate.deficit,
      minimumFreeMiB: candidate.minimumFreeMiB,
      averageUtilization: candidate.averageUtilization,
    })),
  };
}

for (const testCase of fixture.cases) {
  const result = match.matches({
    servers: fixture.servers,
    activeConditions: fixture.activeConditions,
    request: testCase.request,
    busyPct: fixture.busyPct,
    temperatureC: fixture.temperatureC,
  });
  assert.deepEqual(normalize(result), testCase.expected, testCase.name);
}

{
  // The browser result keeps the full GPU objects for rendering; the fixture
  // only pins their identity, so check the shape once here.
  const [first] = match.matches({
    servers: fixture.servers,
    activeConditions: fixture.activeConditions,
    request: fixture.cases[0].request,
    busyPct: fixture.busyPct,
    temperatureC: fixture.temperatureC,
  }).candidates;
  assert.equal(first.host, "roomier-host");
  assert.equal(first.available[0].uuid, "GPU-0");
  assert.equal(first.cpuUsage, 10);
  // A missing activeConditions list is treated as no conditions.
  const bare = match.matches({
    servers: fixture.servers,
    activeConditions: undefined,
    request: fixture.cases[0].request,
    busyPct: fixture.busyPct,
    temperatureC: fixture.temperatureC,
  });
  assert.equal(bare.excludedHealth, 0);
}

console.log("capacity match contract passed");
