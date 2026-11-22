import assert from "node:assert/strict";

await import("../src/mocop/static/preferences.js");

class MemoryStorage {
  constructor(entries = []) {
    this.values = new Map(entries);
  }

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    if (this.full) throw new Error("QuotaExceededError");
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

const safeStoredHosts = (value) => (Array.isArray(value) ? value.filter((host) => /^[a-z0-9-]+$/.test(host)) : []);
const make = (storage) => globalThis.MocopPreferences.create({ storage, safeStoredHosts });

const fresh = make(new MemoryStorage()).load();
assert.equal(fresh.visualStyle, "carbon", "carbon is the default style");
assert.equal(fresh.accent, "auto", "the default accent follows the style");
assert.equal(fresh.textSize, "standard");
assert.deepEqual(fresh.serverOrder, []);

// A version 1 record keeps its view choices but not its retired visual defaults.
const legacyStorage = new MemoryStorage([["mocop.preferences.v1", JSON.stringify({
  visualStyle: "precision", accent: "rose", serverSort: "group", serverOrder: ["atlas-01", "BAD HOST"],
  heatMetric: "temperature", showPower: false,
})]]);
const migrated = make(legacyStorage).load();
assert.equal(migrated.visualStyle, "carbon");
assert.equal(migrated.accent, "auto");
assert.equal(migrated.serverSort, "group");
assert.deepEqual(migrated.serverOrder, ["atlas-01"], "stored hosts are revalidated");
assert.equal(migrated.heatMetric, "temperature");
assert.equal(migrated.showPower, false);
assert.equal(legacyStorage.getItem("mocop.preferences.v1"), null, "the legacy record is removed once migrated");
assert.equal(JSON.parse(legacyStorage.getItem("mocop.preferences.v2")).serverSort, "group");

// After migration a viewer may choose any style again, including precision.
const v2 = new MemoryStorage([["mocop.preferences.v2", JSON.stringify({ visualStyle: "precision", accent: "rose", heatMetric: "bandwidth" })]]);
assert.equal(make(v2).load().visualStyle, "precision");
assert.equal(make(v2).load().heatMetric, "bandwidth");
assert.equal(make(v2).load().accent, "rose");

const hostile = make(new MemoryStorage([["mocop.preferences.v2", JSON.stringify({
  visualStyle: "blueprint", density: 3, backgroundVisibility: 90, me: "eve\u0007", showTemperature: "yes",
})]])).load();
assert.equal(hostile.visualStyle, "carbon", "retired styles fall back to the default");
assert.equal(hostile.density, "comfortable");
assert.equal(hostile.backgroundVisibility, 38);
assert.equal(hostile.me, "", "control characters are refused");
assert.equal(hostile.showTemperature, true);

const corrupt = new MemoryStorage([["mocop.preferences.v2", "{not json"]]);
assert.equal(make(corrupt).load().visualStyle, "carbon", "unreadable JSON degrades to defaults");
assert.equal(make(new MemoryStorage([["mocop.preferences.v2", "[]"]])).load().accent, "auto");

const full = new MemoryStorage([["mocop.preferences.v1", JSON.stringify({ serverSort: "host" })]]);
full.full = true;
const unsaved = make(full).load();
assert.equal(unsaved.serverSort, "host", "a full storage still returns the migrated values");
assert.notEqual(full.getItem("mocop.preferences.v1"), null, "the legacy record stays until a save succeeds");
assert.equal(make(full).save({ ...unsaved, me: "alice" }), false);

const saved = new MemoryStorage();
assert.equal(make(saved).save({ ...fresh, me: "alice", textSize: "large" }), true);
assert.equal(make(saved).load().me, "alice");
assert.equal(make(saved).load().textSize, "large");

console.log("preferences tests passed");
