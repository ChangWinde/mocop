import assert from "node:assert/strict";

await import("../src/mocop/static/alert-noise.js");

const noise = globalThis.MocopAlertNoise.create();
const brief = (opened, hours, recurring = []) => ({
  generatedAt: "2026-11-22T12:00:00Z",
  changes: {
    coveredFromAt: new Date(Date.parse("2026-11-22T12:00:00Z") - hours * 3_600_000).toISOString(),
    opened,
    recurring,
  },
});

assert.equal(noise.summarize(null), null);
assert.equal(noise.summarize({ generatedAt: "bad", changes: {} }), null, "an unreadable window is not zero noise");

const quiet = noise.summarize(brief(12, 24));
assert.equal(quiet.perHour, 0.5);
assert.equal(quiet.state, "");
assert.equal(quiet.target, 6);

assert.equal(noise.summarize(brief(6 * 24, 24)).state, "", "six per hour is still manageable");
assert.equal(noise.summarize(brief(7 * 24, 24)).state, "warning");
assert.equal(noise.summarize(brief(13 * 24, 24)).state, "critical");

const flapping = noise.summarize(brief(246, 15.5, [
  { host: "vega-02", category: "connectivity", resource: "SSH", openings: 237 },
  { host: "vega-07", category: "cpu", resource: null, openings: 6 },
  { host: "a", category: "disk", resource: "/", openings: 2 },
  { host: "b", category: "disk", resource: "/", openings: 1 },
]));
assert.equal(flapping.state, "critical");
assert.equal(flapping.recurring.length, 3, "only the top recurring conditions are shown");
assert.equal(Math.round(flapping.recurring[0].share * 100), 96);

const instant = noise.summarize({ generatedAt: "2026-11-22T12:00:00Z", changes: { coveredFromAt: "2026-11-22T12:00:00Z", opened: 0 } });
assert.equal(instant.perHour, 0, "a zero-length window does not divide by zero");

console.log("alert-noise tests passed");
