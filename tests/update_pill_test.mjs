import assert from "node:assert/strict";

await import("../src/mocop/static/update-pill.js");

class FakeButton {
  constructor() {
    this.hidden = true;
    this.disabled = false;
    this.className = "";
    this.textContent = "";
    this.title = "";
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  async click() {
    await this.listeners.get("click")?.();
  }
}

function makeRequest(handlers) {
  const calls = [];
  return {
    calls,
    request: async (path, options = {}) => {
      calls.push({ path, options });
      const handler = handlers[`${options.method || "GET"} ${path}`];
      if (!handler) throw new Error(`unexpected request: ${path}`);
      const payload = typeof handler === "function" ? handler() : handler;
      return {
        ok: payload.status === undefined || payload.status < 400,
        status: payload.status ?? 200,
        json: async () => payload.body ?? payload,
      };
    },
  };
}

{
  // off mode keeps the pill hidden; current release renders the calm state.
  const button = new FakeButton();
  const { request } = makeRequest({
    "GET /api/update": { mode: "off", currentVersion: "1.0.0" },
  });
  const pill = globalThis.MocopUpdatePill.create({ button, request, reload: () => {} });
  pill.start();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(button.hidden, true);
  pill.stop();
}

{
  // Current release: visible, disabled, and labeled as up to date.
  const button = new FakeButton();
  const { request } = makeRequest({
    "GET /api/update": {
      mode: "self-update",
      currentVersion: "1.0.0",
      latestVersion: "1.0.0",
      updateAvailable: false,
      state: "idle",
      detail: null,
    },
  });
  const pill = globalThis.MocopUpdatePill.create({ button, request, reload: () => {} });
  pill.start();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(button.hidden, false);
  assert.equal(button.disabled, true);
  assert.match(button.textContent, /v1\.0\.0 · 已是最新/);
  assert.match(button.className, /current/);
  pill.stop();
}

{
  // check mode announces the release but never enables the apply action.
  const button = new FakeButton();
  const { request, calls } = makeRequest({
    "GET /api/update": {
      mode: "check",
      currentVersion: "1.0.0",
      latestVersion: "2.0.0",
      updateAvailable: true,
      state: "idle",
      detail: null,
    },
  });
  const pill = globalThis.MocopUpdatePill.create({ button, request, reload: () => {} });
  pill.start();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.match(button.textContent, /有新版本 v2\.0\.0/);
  assert.equal(button.disabled, true);
  await button.click();
  assert.equal(calls.filter((c) => c.options.method === "POST").length, 0);
  pill.stop();
}

{
  // self-update mode: clicking sends exactly one fixed empty POST, then the
  // restarting state probes /api/meta and reloads once the version changes.
  const button = new FakeButton();
  let reloaded = 0;
  let metaVersion = "1.0.0";
  let state = "idle";
  const { request, calls } = makeRequest({
    "GET /api/update": () => ({
      mode: "self-update",
      currentVersion: "1.0.0",
      latestVersion: "2.0.0",
      updateAvailable: true,
      state,
      detail: null,
    }),
    "POST /api/update/apply": () => {
      state = "restarting";
      return { status: 202, body: { status: "updating" } };
    },
    "GET /api/meta": () => ({ appVersion: metaVersion }),
  });
  const pill = globalThis.MocopUpdatePill.create({
    button,
    request,
    reload: () => {
      reloaded += 1;
    },
  });
  pill.start();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.match(button.textContent, /更新到 v2\.0\.0/);
  assert.equal(button.disabled, false);

  await button.click();
  const post = calls.find((c) => c.options.method === "POST");
  assert.equal(post.path, "/api/update/apply");
  assert.equal(post.options.body, "{}");

  // Next poll observes restarting, then the meta probe sees the new version.
  await new Promise((resolve) => setTimeout(resolve, 2_100));
  metaVersion = "2.0.0";
  await new Promise((resolve) => setTimeout(resolve, 4_500));
  assert.equal(reloaded, 1, "reloads once the new version answers");
  pill.stop();
}

console.log("update pill contract passed");
