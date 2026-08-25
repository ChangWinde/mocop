// The release-currency pill (ADR-0026): one header control that shows
// whether the running release is current, offers the one-click apply when
// the service allows it, and reloads the page after the service restarts
// into the new version. The leaf owns polling cadence and state projection;
// app.js only supplies the button and its authenticated request function.
// The browser never names a version: apply is one fixed empty POST.
(() => {
  "use strict";

  const IDLE_POLL_MS = 60_000;
  const BUSY_POLL_MS = 2_000;
  const RESTART_TIMEOUT_MS = 90_000;

  function create({ button, request, reload = () => window.location.reload() }) {
    if (!button || typeof request !== "function") {
      throw new TypeError("update pill requires a button and a request function");
    }
    let status = null;
    let timer = null;
    let awaitingRestart = false;

    function render() {
      if (!status || status.mode === "off") {
        button.hidden = true;
        return;
      }
      button.hidden = false;
      button.title = status.detail || "";
      if (awaitingRestart || status.state === "restarting") {
        set("busy", "重启到新版本…", true);
      } else if (status.state === "updating") {
        set("busy", status.detail === "installing" ? "安装更新中…" : "下载更新中…", true);
      } else if (status.state === "failed") {
        set("failed", "更新失败 · 点击重试", status.mode !== "self-update");
      } else if (status.updateAvailable) {
        const action = status.mode === "self-update" ? "更新到" : "有新版本";
        set("available", `${action} v${status.latestVersion}`, status.mode !== "self-update");
      } else {
        set("current", `v${status.currentVersion} · 已是最新`, true);
      }
    }

    function set(kind, text, disabled) {
      button.className = `update-pill ${kind}`;
      button.textContent = text;
      button.disabled = disabled;
    }

    async function poll() {
      try {
        const response = await request("/api/update", { cache: "no-store" });
        if (response.ok) status = await response.json();
      } catch (_error) {
        // Keep the last known state; the next tick retries.
      }
      render();
      if (!awaitingRestart && status?.state === "restarting") {
        awaitingRestart = true;
        awaitRestart();
      }
      schedule();
    }

    function schedule() {
      if (timer !== null) clearTimeout(timer);
      const busy = awaitingRestart
        || status?.state === "updating"
        || status?.state === "restarting";
      timer = setTimeout(poll, busy ? BUSY_POLL_MS : IDLE_POLL_MS);
    }

    // The old process acknowledged the restart; the page reloads once the
    // replacement answers with a different version so the browser picks up
    // the new static assets. A timeout falls back to a plain reload.
    async function awaitRestart() {
      const previous = status?.currentVersion;
      const deadline = Date.now() + RESTART_TIMEOUT_MS;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, BUSY_POLL_MS));
        try {
          const response = await request("/api/meta", { cache: "no-store" });
          if (response.ok) {
            const meta = await response.json();
            if (typeof meta.appVersion === "string" && meta.appVersion !== previous) {
              reload();
              return;
            }
          }
        } catch (_error) {
          // The expected gap while the old process exits.
        }
      }
      reload();
    }

    async function applyNow() {
      try {
        const response = await request("/api/update/apply", {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        if (response.status === 202 && status) {
          status = { ...status, state: "updating", detail: "downloading" };
        }
      } catch (_error) {
        // The status poll surfaces the outcome either way.
      }
      render();
      schedule();
    }

    button.addEventListener("click", () => {
      if (button.disabled || !status) return;
      if (status.state === "failed" || status.updateAvailable) applyNow();
    });

    return Object.freeze({
      start() {
        poll();
      },
      stop() {
        if (timer !== null) clearTimeout(timer);
        timer = null;
      },
    });
  }

  globalThis.MocopUpdatePill = Object.freeze({ create });
})();
