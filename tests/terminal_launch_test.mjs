import assert from "node:assert/strict";

await import("../src/mocop/static/terminal-launch.js");

class FakeOption {
  constructor() {
    this.value = "";
    this.textContent = "";
  }
}

class FakeButton {
  constructor() {
    this.hidden = true;
    this.disabled = false;
    this.textContent = "";
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  async click() {
    await this.listeners.get("click")?.();
  }
}

class FakeSelect extends FakeButton {
  constructor() {
    super();
    this.value = "";
    this.options = [];
    this.ownerDocument = { createElement: () => new FakeOption() };
  }

  replaceChildren(...options) {
    this.options = options;
    if (!options.some((option) => option.value === this.value)) {
      this.value = options[0]?.value ?? "";
    }
  }

  change(value) {
    this.value = value;
    this.listeners.get("change")?.();
  }
}

class MemoryStorage {
  constructor(entries = []) {
    this.values = new Map(entries);
  }

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
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
      const payload = typeof handler === "function" ? handler(options) : handler;
      return {
        ok: payload.status === undefined || payload.status < 400,
        status: payload.status ?? 200,
        json: async () => payload.body ?? payload,
      };
    },
  };
}

const APPS = [
  { id: "ghostty", label: "Ghostty" },
  { id: "terminal", label: "Terminal.app" },
];
const META = { capabilities: { terminalApps: APPS } };
const IDLE = "\u6253\u5f00\u7ec8\u7aef";
// Timers are captured so the feedback reset can be driven deterministically.
function manualDelay() {
  const pending = [];
  return {
    pending,
    delay: (callback) => {
      pending.push(callback);
    },
    flush() {
      while (pending.length) pending.shift()();
    },
  };
}

{
  // Without the capability (older service, other platforms) both controls stay hidden.
  const button = new FakeButton();
  const picker = new FakeSelect();
  const { request } = makeRequest({ "GET /api/meta": { capabilities: { restartSupported: false } } });
  const launch = globalThis.MocopTerminalLaunch.create({ button, picker, request });
  await launch.ready;
  launch.setHost("gpu-01");
  assert.equal(button.hidden, true);
  assert.equal(picker.hidden, true);
  assert.deepEqual(launch.apps, []);
}

{
  // The capability list is validated before use.
  const { validApps } = globalThis.MocopTerminalLaunch;
  assert.deepEqual(validApps(null), []);
  assert.deepEqual(validApps([{ id: "Bad Id", label: "x" }, { id: "ok", label: "" }]), []);
  assert.deepEqual(validApps([{ id: "terminal", label: "Terminal.app", extra: 1 }]).map((app) => app.id), ["terminal"]);
}

{
  // A selected host reveals the button; the picker appears only with a choice to make.
  const button = new FakeButton();
  const picker = new FakeSelect();
  const { request } = makeRequest({ "GET /api/meta": META });
  const launch = globalThis.MocopTerminalLaunch.create({ button, picker, request });
  await launch.ready;
  assert.equal(button.hidden, true);
  launch.setHost("gpu-01");
  assert.equal(button.hidden, false);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, IDLE);
  assert.equal(picker.hidden, false);
  assert.deepEqual(picker.options.map((option) => [option.value, option.textContent]), [
    ["ghostty", "Ghostty"],
    ["terminal", "Terminal.app"],
  ]);
  assert.equal(launch.app, "ghostty");
  launch.setHost(null);
  assert.equal(button.hidden, true);
  assert.equal(picker.hidden, true);
}

{
  // One application: no picker, the button alone.
  const button = new FakeButton();
  const picker = new FakeSelect();
  const { request } = makeRequest({
    "GET /api/meta": { capabilities: { terminalApps: [APPS[1]] } },
  });
  const launch = globalThis.MocopTerminalLaunch.create({ button, picker, request });
  await launch.ready;
  launch.setHost("gpu-01");
  assert.equal(button.hidden, false);
  assert.equal(picker.hidden, true);
  assert.equal(launch.app, "terminal");
}

{
  // The remembered application wins over the service's preference and survives reloads.
  const storage = new MemoryStorage([["mocop.terminalApp.v1", "terminal"]]);
  const button = new FakeButton();
  const picker = new FakeSelect();
  const { request } = makeRequest({ "GET /api/meta": META });
  const launch = globalThis.MocopTerminalLaunch.create({ button, picker, request, storage });
  await launch.ready;
  assert.equal(picker.value, "terminal");
  assert.equal(launch.app, "terminal");
  picker.change("ghostty");
  assert.equal(storage.getItem("mocop.terminalApp.v1"), "ghostty");
  assert.equal(launch.app, "ghostty");
}

{
  // Clicking posts exactly {host, app}, disables the button meanwhile, and reports success.
  const timers = manualDelay();
  const button = new FakeButton();
  const picker = new FakeSelect();
  let inFlight = null;
  const { request, calls } = makeRequest({
    "GET /api/meta": META,
    "POST /api/terminal": (options) => {
      inFlight = { disabled: button.disabled, text: button.textContent };
      return { status: 200, body: { ...JSON.parse(options.body), status: "opened", accepted: true } };
    },
  });
  const launch = globalThis.MocopTerminalLaunch.create({ button, picker, request, delay: timers.delay });
  await launch.ready;
  launch.setHost("gpu-01");
  picker.change("terminal");
  await button.click();
  const post = calls.find((call) => call.path === "/api/terminal");
  assert.equal(post.options.method, "POST");
  assert.equal(post.options.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(post.options.body), { host: "gpu-01", app: "terminal" });
  assert.equal(inFlight.disabled, true);
  assert.equal(inFlight.text, "\u6b63\u5728\u6253\u5f00\u2026");
  assert.equal(button.textContent, "\u5df2\u6253\u5f00\u7ec8\u7aef");
  assert.equal(button.disabled, false);
  timers.flush();
  assert.equal(button.textContent, IDLE);
}

{
  // Service statuses map to their own feedback; transport failures fall back to the generic one.
  const timers = manualDelay();
  const button = new FakeButton();
  const picker = new FakeSelect();
  let response = { status: 429, body: { status: "rate_limited", code: "RATE_LIMITED" } };
  const { request } = makeRequest({
    "GET /api/meta": META,
    "POST /api/terminal": () => {
      if (response instanceof Error) throw response;
      return response;
    },
  });
  const launch = globalThis.MocopTerminalLaunch.create({ button, picker, request, delay: timers.delay });
  await launch.ready;
  launch.setHost("gpu-01");
  await button.click();
  assert.equal(button.textContent, "\u64cd\u4f5c\u8fc7\u5feb\uff0c\u7a0d\u540e\u518d\u8bd5");
  timers.flush();

  response = { status: 404, body: { status: "unknown_host", code: "UNKNOWN_HOST" } };
  await button.click();
  assert.equal(button.textContent, "\u8282\u70b9\u4e0d\u5728\u76d1\u63a7\u5217\u8868");
  timers.flush();

  response = { status: 500, body: { status: "failed", code: "INTERNAL_ERROR" } };
  await button.click();
  assert.equal(button.textContent, "\u6253\u5f00\u5931\u8d25");
  timers.flush();

  response = new Error("network down");
  await button.click();
  assert.equal(button.textContent, "\u6253\u5f00\u5931\u8d25");
  timers.flush();
  assert.equal(button.textContent, IDLE);

  // Clearing the host mid-feedback hides the control without clobbering state.
  response = { status: 200, body: { status: "opened" } };
  await button.click();
  launch.setHost(null);
  assert.equal(button.hidden, true);
  timers.flush();
  launch.setHost("gpu-02");
  assert.equal(button.textContent, IDLE);
}

{
  // No host or an in-flight request: the click is ignored.
  const button = new FakeButton();
  const picker = new FakeSelect();
  const { request, calls } = makeRequest({ "GET /api/meta": META });
  const launch = globalThis.MocopTerminalLaunch.create({ button, picker, request });
  await launch.ready;
  await button.click();
  assert.equal(calls.filter((call) => call.path === "/api/terminal").length, 0);
  launch.setHost("");
  assert.equal(button.hidden, true);
}

assert.throws(
  () => globalThis.MocopTerminalLaunch.create({ button: new FakeButton(), picker: null, request: () => {} }),
  TypeError,
);

console.log("terminal launch contract passed");
