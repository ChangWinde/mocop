import assert from "node:assert/strict";

await import("../src/mocop/static/format.js");

const fmt = globalThis.MocopFormat.create();
const NOW = Date.parse("2026-08-14T03:00:00Z");
const at = (secondsBefore) => new Date(NOW - secondsBefore * 1000).toISOString();

{
  // Numbers: anything non-finite falls back, and bounds clamp to 0–100.
  assert.equal(fmt.numeric("12.5"), 12.5);
  assert.equal(fmt.numeric(undefined), 0);
  assert.equal(fmt.numeric("nan", 7), 7);
  assert.equal(fmt.numeric(Infinity, -1), -1);
  assert.equal(fmt.clamp(140), 100);
  assert.equal(fmt.clamp(-3), 0);
  assert.equal(fmt.clamp("abc"), 0);
  assert.equal(fmt.ratio(25, 100), 25);
  assert.equal(fmt.ratio(1, 0), 0);
  assert.equal(fmt.format(1234567.891, 1), "1,234,567.9");
  assert.equal(fmt.format(0.4), "0");
}

{
  // Trend points: an absent metric is NaN (a gap), a combined pair is a gap
  // only when both halves are absent.
  const point = { cpuUsagePct: "42", networkRxBps: 1024, networkTxBps: null };
  assert.equal(fmt.optionalMetric(point, "cpuUsagePct"), 42);
  assert.ok(Number.isNaN(fmt.optionalMetric(point, "memoryUsagePct")));
  assert.ok(Number.isNaN(fmt.optionalMetric({ cpuUsagePct: "x" }, "cpuUsagePct")));
  assert.equal(fmt.combinedMetric(point, "networkRxBps", "networkTxBps"), 1024);
  assert.ok(Number.isNaN(fmt.combinedMetric({}, "diskReadBps", "diskWriteBps")));
}

{
  // Binary units switch at 1024 boundaries; rates say "—" when unknown.
  assert.equal(fmt.memory(512), "512 MiB");
  assert.equal(fmt.memory(1024), "1 GiB");
  assert.equal(fmt.memory(81_920), "80 GiB");
  assert.equal(fmt.storage(1_907_348), "1.8 TiB");
  assert.equal(fmt.storage(812_442), "793.4 GiB");
  assert.equal(fmt.rate(undefined), "—");
  assert.equal(fmt.rate("n/a"), "—");
  assert.equal(fmt.rate(512), "512 B/s");
  assert.equal(fmt.rate(2048), "2 KiB/s");
  assert.equal(fmt.rate(186_646_528), "178 MiB/s");
  assert.equal(fmt.rate(3 * 1024 ** 3), "3 GiB/s");
}

{
  // Uptime and elapsed wording differ from freshness wording.
  assert.equal(fmt.duration(1_428_320), "16 天 12 小时");
  assert.equal(fmt.duration(5_400), "1 小时 30 分钟");
  assert.equal(fmt.durationSince(at(3 * 3600 + 12 * 60), NOW), "3 小时 12 分");
  assert.equal(fmt.durationSince(at(2 * 86400 + 3600), NOW), "2 天 1 小时");
  assert.equal(fmt.durationSince(at(150), NOW), "2 分钟");
  assert.equal(fmt.durationSince(at(9), NOW), "9 秒");
  assert.equal(fmt.durationSince("not a time", NOW), "时长未知");
  assert.equal(fmt.age(null, NOW), "等待数据");
  assert.equal(fmt.age(at(1), NOW), "刚刚");
  assert.equal(fmt.age(at(45), NOW), "45 秒前");
  assert.equal(fmt.age(at(600), NOW), "10 分钟前");
  // A timestamp ahead of the clock never reads as negative.
  assert.equal(fmt.age(at(-30), NOW), "刚刚");
}

{
  // Retry countdowns round up so the promise is never early.
  assert.equal(fmt.retryCountdown(null, NOW), "");
  assert.equal(fmt.retryCountdown(at(5), NOW), "等待重试");
  assert.equal(fmt.retryCountdown(at(-0.2), NOW), "1 秒后重试");
  assert.equal(fmt.retryCountdown(at(-59), NOW), "59 秒后重试");
  assert.equal(fmt.retryCountdown(at(-61), NOW), "2 分钟后重试");
  assert.equal(fmt.shortTime("garbage"), "未知时间");
  assert.match(fmt.shortTime("2026-08-14T03:04:00Z"), /\d+\/\d+ \d{2}:\d{2}/);
}

{
  // SSE framing: CRLF and lone CR become LF, except a CR that ends the chunk,
  // which must wait for the next read to decide.
  assert.equal(fmt.appendStreamChunk("", "event: a\r\ndata: 1\r\n\r\n"), "event: a\ndata: 1\n\n");
  assert.equal(fmt.appendStreamChunk("data: 1", "\rdata: 2\r"), "data: 1\ndata: 2\r");
  assert.equal(fmt.appendStreamChunk("data: 1\r", "\ndata: 2"), "data: 1\ndata: 2");
}

console.log("format contract ok");
