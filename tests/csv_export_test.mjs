import assert from "node:assert/strict";

await import("../src/mocop/static/csv-export.js");

const { buildCsv, csvCell } = globalThis.MocopCsvExport.create({
  ratio: (used, total) => (total > 0 ? (used / total) * 100 : 0),
  gpuProcessSummary: () => ({ count: 2, knownMemoryCount: 2, knownMemoryMiB: 2048 }),
  gpuState: () => ["运行中"],
});

// Spreadsheet formula injection: leading =, +, -, @ (even behind whitespace
// or control characters) must be neutralized with a leading apostrophe.
assert.equal(csvCell("=cmd|calc"), '"\'=cmd|calc"');
assert.equal(csvCell("+SUM(A1)"), '"\'+SUM(A1)"');
assert.equal(csvCell("-2+3"), '"\'-2+3"');
assert.equal(csvCell("@import"), '"\'@import"');
assert.equal(csvCell(" \t=1"), `"' \t=1"`);
assert.equal(csvCell("\u0000=1"), `"'\u0000=1"`);

// Quotes double, plain values pass through, null becomes empty.
assert.equal(csvCell('say "hi"'), '"say ""hi"""');
assert.equal(csvCell(42), '"42"');
assert.equal(csvCell(null), '""');
assert.equal(csvCell(undefined), '""');

const gpu = {
  index: 0,
  uuid: "GPU-1",
  name: '=HYPERLINK("http://evil")',
  driver_version: "550",
  pstate: "P0",
  utilization_gpu_pct: 50,
  memory_used_mib: 1024,
  memory_total_mib: 2048,
  temperature_c: 60,
  power_draw_w: 100,
  power_limit_w: 200,
  processes_available: true,
  processes_sampled: true,
  processes_observed_at: "2026-08-25T00:00:00Z",
};
const server = {
  host: "gpu-01",
  system: { cpu_usage_pct: 10, memory_used_mib: 100, memory_total_mib: 200 },
  lastAttemptAt: "2026-08-25T00:00:01Z",
};
const csv = buildCsv([{ server, gpu }]);
const lines = csv.split("\r\n");

assert.ok(csv.startsWith("\uFEFF"), "BOM keeps Excel reading UTF-8");
assert.equal(lines.length, 3, "header, one row, trailing terminator");
assert.match(lines[0], /^\uFEFF"主机","GPU Index"/);
assert.ok(lines[1].includes('"\'=HYPERLINK(""http://evil"")"'), "model name is neutralized");
assert.ok(lines[1].includes('"50.00"'), "memory ratio is formatted");
assert.ok(lines[1].includes('"2/2"'), "process coverage uses the summary");
assert.ok(lines[1].includes('"运行中"'), "gpu state column is present");

console.log("csv export contract passed");
