(() => {
  "use strict";

  const STORAGE_KEY = "mocop.dashboardAccessToken.v1";
  const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,192}$/;

  function create(browser) {
    if (!browser?.location || !browser?.history) {
      throw new TypeError("dashboard authentication requires a browser window");
    }

    const fragmentParameters = new URLSearchParams(browser.location.hash.slice(1));
    const fragmentToken = fragmentParameters.get("access_token");
    const validFragmentToken = isValid(fragmentToken) ? fragmentToken : "";
    let storedToken = "";
    let invalidFragment = fragmentToken != null && !validFragmentToken;

    try {
      if (validFragmentToken) {
        browser.sessionStorage.setItem(STORAGE_KEY, validFragmentToken);
      } else if (fragmentToken == null) {
        storedToken = browser.sessionStorage.getItem(STORAGE_KEY) || "";
      } else {
        browser.sessionStorage.removeItem(STORAGE_KEY);
      }
    } catch (_error) {
      // The fragment still authenticates this document when privacy settings
      // disable sessionStorage; a reload then requires the capability again.
    }

    let token = validFragmentToken || (isValid(storedToken) ? storedToken : "");
    const forget = () => {
      token = "";
      try {
        browser.sessionStorage.removeItem(STORAGE_KEY);
      } catch (_error) {
        // There is no persistent browser state to clear in this mode.
      }
    };
    const remember = (candidate) => {
      if (!isValid(candidate)) return false;
      token = candidate;
      try {
        browser.sessionStorage.setItem(STORAGE_KEY, candidate);
      } catch (_error) {
        // The current document remains authenticated without persistence.
      }
      return true;
    };
    if (fragmentToken != null) {
      fragmentParameters.delete("access_token");
      const retainedFragment = fragmentParameters.toString();
      browser.history.replaceState(
        null,
        "",
        `${browser.location.pathname}${browser.location.search}${retainedFragment ? `#${retainedFragment}` : ""}`,
      );
    }

    return Object.freeze({
      bindPrompt({ dialog, form, input, submit, status, authenticate, onRequired }) {
        if (!dialog || !form || !input || !submit || !status) {
          throw new TypeError("dashboard authentication prompt is incomplete");
        }
        dialog.addEventListener("cancel", (event) => event.preventDefault());
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          const candidate = input.value.trim();
          if (!isValid(candidate)) {
            status.className = "authentication-status";
            status.textContent = "令牌格式无效：应为 32–192 位字母、数字、连字符或下划线";
            input.focus();
            return;
          }

          input.disabled = true;
          submit.disabled = true;
          status.className = "authentication-status pending";
          status.textContent = "正在验证…";
          token = candidate;
          try {
            const result = await authenticate();
            if (result.started) {
              remember(candidate);
              status.textContent = "";
              input.value = "";
              dialog.close();
            } else if (!result.rejected) {
              forget();
              status.className = "authentication-status";
              status.textContent = "暂时无法连接 Mocop，请检查转发链路后重试";
            }
          } catch (_error) {
            forget();
            status.className = "authentication-status";
            status.textContent = "暂时无法连接 Mocop，请检查转发链路后重试";
          } finally {
            input.disabled = false;
            submit.disabled = false;
            if (dialog.open) input.focus();
          }
        });
        return (message) => {
          onRequired();
          status.className = "authentication-status";
          status.textContent = message;
          if (!dialog.open) dialog.showModal();
          browser.requestAnimationFrame(() => input.focus());
        };
      },
      get token() {
        return token;
      },
      consumeInvalidFragment() {
        const value = invalidFragment;
        invalidFragment = false;
        return value;
      },
      forget,
    });
  }

  function isValid(token) {
    return typeof token === "string" && TOKEN_PATTERN.test(token);
  }

  globalThis.MocopDashboardAuth = Object.freeze({ create });
})();
