import assert from "node:assert/strict";

await import("../src/mocop/static/dashboard-auth.js");

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

class UnavailableStorage extends MemoryStorage {
  getItem() {
    throw new Error("storage unavailable");
  }

  setItem() {
    throw new Error("storage unavailable");
  }

  removeItem() {
    throw new Error("storage unavailable");
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

function makeBrowser({ hash = "", persistent = [], session = [] } = {}) {
  const replacements = [];
  return {
    history: { replaceState: (_state, _unused, value) => replacements.push(value) },
    localStorage: persistent instanceof MemoryStorage
      ? persistent
      : new MemoryStorage(persistent),
    location: { hash, pathname: "/dashboard", search: "?host=gpu-01" },
    replacements,
    requestAnimationFrame: (callback) => callback(),
    sessionStorage: new MemoryStorage(session),
  };
}

const storageKey = "mocop.dashboardAccessToken.v1";

{
  const browser = makeBrowser({ hash: `#access_token=${VALID_TOKEN}&gpu=1` });
  const authentication = globalThis.MocopDashboardAuth.create(browser);
  assert.equal(authentication.token, VALID_TOKEN);
  assert.equal(browser.localStorage.getItem(storageKey), VALID_TOKEN);
  assert.equal(browser.sessionStorage.getItem(storageKey), null);
  assert.deepEqual(browser.replacements, ["/dashboard?host=gpu-01#gpu=1"]);
}

{
  const browser = makeBrowser({
    hash: "#access_token=short",
    persistent: [[storageKey, VALID_TOKEN]],
  });
  const authentication = globalThis.MocopDashboardAuth.create(browser);
  assert.equal(authentication.token, "");
  assert.equal(browser.localStorage.getItem(storageKey), null);
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
      assert.equal(browser.localStorage.getItem(storageKey), null);
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
  assert.equal(browser.localStorage.getItem(storageKey), VALID_TOKEN);
  authentication.forget();
  assert.equal(authentication.token, "");
  assert.equal(browser.localStorage.getItem(storageKey), null);
}

{
  // A capability ingested from the fragment survives a browser restart: the
  // second "browser" shares only the persistent storage, not the URL.
  const persistent = new MemoryStorage();
  const firstBrowser = makeBrowser({ persistent, hash: `#access_token=${VALID_TOKEN}` });
  const firstAuthentication = globalThis.MocopDashboardAuth.create(firstBrowser);
  assert.equal(firstAuthentication.token, VALID_TOKEN);
  assert.equal(persistent.getItem(storageKey), VALID_TOKEN);

  const restartedBrowser = makeBrowser({ persistent });
  const restartedAuthentication = globalThis.MocopDashboardAuth.create(restartedBrowser);
  assert.equal(restartedAuthentication.token, VALID_TOKEN);
}

{
  const staleToken = "B".repeat(43);
  const browser = makeBrowser({
    persistent: [[storageKey, VALID_TOKEN]],
    session: [[storageKey, staleToken]],
  });
  const authentication = globalThis.MocopDashboardAuth.create(browser);
  assert.equal(authentication.token, VALID_TOKEN);
  assert.equal(browser.sessionStorage.getItem(storageKey), null);
}

{
  const browser = makeBrowser({
    persistent: new UnavailableStorage(),
    session: [[storageKey, VALID_TOKEN]],
  });
  const authentication = globalThis.MocopDashboardAuth.create(browser);
  assert.equal(authentication.token, VALID_TOKEN);
  assert.equal(browser.sessionStorage.getItem(storageKey), VALID_TOKEN);
}

console.log("dashboard auth contract passed");
