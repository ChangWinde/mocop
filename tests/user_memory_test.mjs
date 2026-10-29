import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

class Element {
  constructor() { this.children = []; this.events = {}; this.ownerDocument = doc; this.open = false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, fn) { this.events[name] = fn; }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.close?.(); }
  set textContent(value) { this.text = value; }
  set innerHTML(_) { throw new Error("untrusted HTML insertion"); }
}
const doc = { createElement: () => new Element() };
const scope = { setTimeout, clearTimeout, console };
vm.runInNewContext(fs.readFileSync(new URL("../src/mocop/static/user-memory.js", import.meta.url), "utf8"), scope);
const button = new Element(), dialog = new Element(), close = new Element(), content = new Element();
let requests = 0, scheduled = 0, cancelled = 0;
const data = { metric: "rss", servers: [{ host: "gpu-01", status: "error", message: "SSH host key changed", sampledAt: "2026-01-01T00:00:00Z", users: [{ uid: 1000, user: "<img onerror=alert(1)>", rssKiB: 1048576, processes: 2 }], totalKiB: 2097152 }] };
const panel = scope.MocopUserMemory.create({ button, dialog, close, content,
  request: async (path) => { assert.equal(path, "/api/user-memory"); requests++; return { ok: true, json: async () => data }; },
  delay: (_fn, ms) => { assert.equal(ms, 60000); scheduled++; return 1; },
  cancel: () => { cancelled++; },
});
assert.equal(requests, 0);
panel.render(data);
const text = (node) => [node.text, ...node.children.map(text)].join(" ");
assert.match(text(content), /<img onerror=alert\(1\)>/);
assert.match(text(content), /1.00 GiB/);
assert.match(text(content), /50.0%/);
assert.match(text(content), /SSH host key changed/);
dialog.showModal();
await panel.refresh();
assert.equal(requests, 1);
assert.equal(scheduled, 1);
close.events.click();
await panel.refresh();
assert.equal(requests, 1);
assert.equal(cancelled, 2);
assert.throws(() => panel.render({}), /invalid memory response/);
const second = { host: "gpu-02", status: "ok", sampledAt: "2026-01-01T00:00:00Z", totalKiB: 2097152, users: [{ uid: 42, user: "second-owner", rssKiB: 1024, processes: 1 }] };
const fleet = { ...data, servers: [data.servers[0], second] };
panel.render(fleet);
const sidebar = content.children[1].children[0];
sidebar.children[2].events.click();
assert.match(text(content.children[1].children[1]), /second-owner/);
assert.doesNotMatch(text(content.children[1].children[1]), /<img/);
panel.render(fleet);
assert.match(text(content.children[1].children[1]), /second-owner/, "selection survives cache refresh");
assert.equal(requests, 1, "server navigation never triggers remote sampling or API reads");
console.log("user memory panel contracts passed");
const diskData = { metric: "disk", servers: [{ host: "storage-01", status: "ok", sampledAt: "2026-01-01T00:00:00Z", directories: [
  { path: "/data/<img onerror=alert(1)>", sizeKiB: 1048576, status: "ok" },
  { path: "/data/unknown", sizeKiB: null, status: "timeout" },
  { path: "/data/empty", sizeKiB: 0, status: "ok" },
], notes: [] }] };
panel.render(diskData);
assert.match(text(content), /目录占用排行/);
assert.match(text(content), /<img onerror=alert\(1\)>/);
assert.match(text(content), /扫描超时/);
assert.match(text(content), /未知占用不会计为 0/);
assert.match(text(content), /100.0%/);
assert.match(text(content), /0.00 GiB/);
assert.match(text(content), /—/);
console.log("directory disk rendering contracts passed");
const diskButton = new Element();
doc.getElementById = (id) => id === "directory-disk-open" ? diskButton : null;
const otherButton = new Element(), otherDialog = new Element(), otherClose = new Element(), otherContent = new Element();
const paths = [];
let resolveDisk;
scope.MocopUserMemory.create({ button: otherButton, dialog: otherDialog, close: otherClose, content: otherContent,
  request: async (path) => { paths.push(path); if (path === "/api/directory-disk") return new Promise((resolve) => { resolveDisk = resolve; }); return { ok: true, json: async () => fleet }; },
  delay: () => 1, cancel: () => {},
});
diskButton.events.click();
assert.equal(paths[0], "/api/directory-disk");
otherClose.events.click();
otherButton.events.click();
resolveDisk({ ok: true, json: async () => diskData });
await new Promise((resolve) => setImmediate(resolve));
assert.deepEqual(paths, ["/api/directory-disk", "/api/user-memory"]);
assert.match(text(otherContent), /用户占用排行/);
assert.doesNotMatch(text(otherContent), /目录占用排行/);
console.log("resource mode switching rejects stale responses");
const mounted = { ...diskData, servers: [{ ...diskData.servers[0], inventoryAt: "2026-01-01T00:00:00Z",
  filesystems: [
    { mountPoint: "/mnt/nas/data", aliases: ["/mnt/nas/data", "/mnt/alias"], kind: "network", fsType: "cifs", totalKiB: 2097152, usedKiB: 1048576, availableKiB: 1048576, percentUsed: 50 },
    { mountPoint: "/data", aliases: ["/data"], kind: "local", fsType: "ext4", totalKiB: null, usedKiB: null, availableKiB: null, percentUsed: null },
  ], directories: [
    { path: "/mnt/nas/data/alice", mountPoint: "/mnt/nas/data", sizeKiB: 1048576, status: "ok" },
    { path: "/data/bob", mountPoint: "/data", sizeKiB: 1024, status: "ok" },
  ] }] };
panel.render(mounted);
assert.match(text(content), /NAS \/ 共享存储/);
assert.match(text(content), /总计 2.00 GiB/);
assert.match(text(content), /同盘入口：\/mnt\/alias/);
const nodes = (node) => [node, ...node.children.flatMap(nodes)];
const cards = nodes(content).filter((n) => n.className?.startsWith("um-mount "));
cards[0].events.click();
const bodyText = text(nodes(content).find((n) => n.className === "um-table-scroll"));
assert.match(bodyText, /alice/);
assert.doesNotMatch(bodyText, /bob/);
console.log("mount inventory, capacity and directory filtering contracts passed");
