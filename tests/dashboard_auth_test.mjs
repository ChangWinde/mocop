import assert from "node:assert/strict";

await import("../mocop/static/dashboard-auth.js");

const VALID_TOKEN = "A".repeat(43);

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

  removeItem(key) {
    this.values.delete(key);
  }
}

class FakeElement {
  constructor() {
    this.listeners = new Map();
    this.className = "";
    this.disabled = false;
    this.open = false;
    this.textContent = "";
    this.value = "";
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  async emit(type) {
    await this.listeners.get(type)?.({ preventDefault() {} });
  }

  close() {
    this.open = false;
  }

  focus() {
    this.focused = true;
  }

  showModal() {
    this.open = true;
  }
}

function makeBrowser({ hash = "", stored = [] } = {}) {
  const replacements = [];
  return {
    history: { replaceState: (_state, _unused, value) => replacements.push(value) },
    location: { hash, pathname: "/dashboard", search: "?host=gpu-01" },
    replacements,
    requestAnimationFrame: (callback) => callback(),
    sessionStorage: new MemoryStorage(stored),
  };
}

const storageKey = "mocop.dashboardAccessToken.v1";

{
  const browser = makeBrowser({ hash: `#access_token=${VALID_TOKEN}&gpu=1` });
  const authentication = globalThis.MocopDashboardAuth.create(browser);
  assert.equal(authentication.token, VALID_TOKEN);
  assert.equal(browser.sessionStorage.getItem(storageKey), VALID_TOKEN);
  assert.deepEqual(browser.replacements, ["/dashboard?host=gpu-01#gpu=1"]);
}

{
  const browser = makeBrowser({
    hash: "#access_token=short",
    stored: [[storageKey, VALID_TOKEN]],
  });
  const authentication = globalThis.MocopDashboardAuth.create(browser);
  assert.equal(authentication.token, "");
  assert.equal(browser.sessionStorage.getItem(storageKey), null);
  assert.equal(authentication.consumeInvalidFragment(), true);
  assert.equal(authentication.consumeInvalidFragment(), false);
}

{
  const browser = makeBrowser();
  const authentication = globalThis.MocopDashboardAuth.create(browser);
  const dialog = new FakeElement();
  const form = new FakeElement();
  const input = new FakeElement();
  const submit = new FakeElement();
  const status = new FakeElement();
  let required = false;
  const request = authentication.bindPrompt({
    dialog,
    form,
    input,
    submit,
    status,
    authenticate: async () => {
      assert.equal(authentication.token, VALID_TOKEN);
      assert.equal(browser.sessionStorage.getItem(storageKey), null);
      return { started: true, rejected: false };
    },
    onRequired: () => { required = true; },
  });
  request("需要访问令牌");
  assert.equal(required, true);
  assert.equal(dialog.open, true);
  assert.equal(input.focused, true);

  input.value = VALID_TOKEN;
  await form.emit("submit");
  assert.equal(dialog.open, false);
  assert.equal(browser.sessionStorage.getItem(storageKey), VALID_TOKEN);
  authentication.forget();
  assert.equal(authentication.token, "");
  assert.equal(browser.sessionStorage.getItem(storageKey), null);
}

console.log("dashboard auth contract passed");
