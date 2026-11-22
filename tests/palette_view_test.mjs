import assert from "node:assert/strict";
import { create } from "./fake_dom.mjs";

await import("../src/mocop/static/command-palette.js");
await import("../src/mocop/static/palette-view.js");

const palette = globalThis.MocopCommandPalette.create();
const input = create("input");
input.value = "";
input.listeners = new Map();
input.addEventListener = (type, listener) => input.listeners.set(type, listener);
const fire = (type, event = {}) => input.listeners.get(type)?.({ preventDefault() { event.prevented = true; }, ...event });
const results = create("div");
const ran = [];
const view = globalThis.MocopPaletteView.create({ create, palette, input, results, run: (entry) => ran.push(entry.id) });

const entries = palette.entries({
  pages: [{ id: "overview", label: "总览" }, { id: "alerts", label: "告警" }],
  hosts: [{ host: "atlas-01", detail: "A 组" }, { host: "atlas-02" }],
});
input.value = "stale text";
view.open(entries);
assert.equal(input.value, "", "opening clears the previous query");
assert.equal(results.children.length, 4);
assert.equal(results.children[0].getAttribute("aria-selected"), "true");
assert.equal(results.children[0].find("palette-kind").textContent, "页面");

input.value = "atlas";
fire("input");
assert.deepEqual(view.matches().map((entry) => entry.id), ["atlas-01", "atlas-02"]);
assert.equal(results.children[0].textContent, "主机atlas-01A 组");

fire("keydown", { key: "ArrowDown" });
assert.equal(view.selected(), 1);
assert.equal(results.children[1].getAttribute("aria-selected"), "true");
fire("keydown", { key: "ArrowDown" });
assert.equal(view.selected(), 0, "selection wraps around");
fire("keydown", { key: "ArrowUp" });
assert.equal(view.selected(), 1);
const enter = {};
fire("keydown", Object.assign(enter, { key: "Enter" }));
assert.deepEqual(ran, ["atlas-02"]);
assert.equal(enter.prevented, true);

results.children[0].click();
assert.deepEqual(ran, ["atlas-02", "atlas-01"]);

input.value = "找 2 张 80G";
fire("input");
assert.equal(view.matches()[0].kind, "demand");
assert.equal(results.children[0].find("palette-kind").textContent, "找卡");

input.value = "zzz";
fire("input");
assert.equal(results.children[0].className, "palette-empty");
fire("keydown", { key: "Enter" });
assert.equal(ran.length, 2, "Enter without a match does nothing");

console.log("palette-view tests passed");
