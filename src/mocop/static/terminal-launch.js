// Local terminal launcher: one button beside "probe now" that asks the
// service to open the operator's terminal application with an SSH session to
// the selected host (the remote shell shows `df -h` first). The service only
// advertises the capability where it can act (macOS desktop), so the control
// stays hidden everywhere else. app.js supplies the two controls, the
// authenticated request function and the selected host; this leaf owns the
// capability lookup, the remembered application choice and the feedback text.
(() => {
  "use strict";

  const STORAGE_KEY = "mocop.terminalApp.v1";
  const FEEDBACK_MS = 2500;
  const IDLE_LABEL = "\u6253\u5f00\u7ec8\u7aef";
  const BUSY_LABEL = "\u6b63\u5728\u6253\u5f00\u2026";
  const STATUS_TEXT = Object.freeze({
    opened: "\u5df2\u6253\u5f00\u7ec8\u7aef",
    unknown_host: "\u8282\u70b9\u4e0d\u5728\u76d1\u63a7\u5217\u8868",
    unknown_app: "\u7ec8\u7aef\u5e94\u7528\u4e0d\u53ef\u7528",
    rate_limited: "\u64cd\u4f5c\u8fc7\u5feb\uff0c\u7a0d\u540e\u518d\u8bd5",
    failed: "\u6253\u5f00\u5931\u8d25",
  });

  function validApps(value) {
    if (!Array.isArray(value)) return [];
    return value.filter((app) => (
      app && typeof app.id === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(app.id)
      && typeof app.label === "string" && app.label.length > 0 && app.label.length <= 40
    ));
  }

  function create({ button, picker, request, storage = null, delay = setTimeout }) {
    if (!button || !picker || typeof request !== "function") {
      throw new TypeError("terminal launcher requires a button, a picker and a request function");
    }
    let apps = [];
    let host = null;
    let pending = false;
    let feedback = false;

    function readChoice() {
      try {
        return storage?.getItem(STORAGE_KEY) || "";
      } catch (_error) {
        return "";
      }
    }

    function rememberChoice(value) {
      try {
        storage?.setItem(STORAGE_KEY, value);
      } catch (_error) {
        // A read-only storage only loses the preference, not the action.
      }
    }

    function selectedApp() {
      const remembered = readChoice();
      const chosen = apps.find((app) => app.id === picker.value)
        || apps.find((app) => app.id === remembered)
        || apps[0];
      return chosen ? chosen.id : "";
    }

    function renderPicker() {
      const remembered = readChoice();
      picker.replaceChildren(...apps.map((app) => {
        const option = picker.ownerDocument.createElement("option");
        option.value = app.id;
        option.textContent = app.label;
        return option;
      }));
      picker.value = apps.some((app) => app.id === remembered) ? remembered : (apps[0]?.id ?? "");
    }

    function render() {
      const available = apps.length > 0 && host !== null;
      button.hidden = !available;
      picker.hidden = !available || apps.length < 2;
      button.disabled = !available || pending;
      if (!pending && !feedback) button.textContent = IDLE_LABEL;
    }

    function showFeedback(text) {
      feedback = true;
      button.textContent = text;
      delay(() => {
        feedback = false;
        render();
      }, FEEDBACK_MS);
    }

    async function loadCapability() {
      try {
        const response = await request("/api/meta", { cache: "no-store" });
        const meta = response.ok ? await response.json() : null;
        apps = validApps(meta?.capabilities?.terminalApps);
      } catch (_error) {
        apps = [];
      }
      renderPicker();
      render();
    }

    async function openTerminal() {
      if (pending || host === null || !apps.length) return;
      const target = host;
      pending = true;
      feedback = false;
      button.disabled = true;
      button.textContent = BUSY_LABEL;
      let text = STATUS_TEXT.failed;
      try {
        const response = await request("/api/terminal", {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ host: target, app: selectedApp() }),
        });
        const result = await response.json().catch(() => ({}));
        text = STATUS_TEXT[result.status] || (response.ok ? STATUS_TEXT.opened : STATUS_TEXT.failed);
      } catch (_error) {
        // Transport failure: the generic failure text already applies.
      }
      pending = false;
      render();
      showFeedback(text);
    }

    picker.addEventListener("change", () => rememberChoice(picker.value));
    button.addEventListener("click", openTerminal);
    render();
    const ready = loadCapability();

    return Object.freeze({
      ready,
      setHost(next) {
        const value = typeof next === "string" && next ? next : null;
        if (value === host) return;
        host = value;
        render();
      },
      get apps() {
        return apps.map((app) => ({ ...app }));
      },
      get app() {
        return selectedApp();
      },
    });
  }

  globalThis.MocopTerminalLaunch = Object.freeze({ create, validApps });
})();
