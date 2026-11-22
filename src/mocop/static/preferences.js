// Display preferences: one versioned, validated browser-local record. Values
// never reach the server or another viewer. Every field is checked against its
// allowed set on load, so a hand-edited or outdated record degrades to the
// defaults field by field instead of breaking the dashboard.
//
// Version 2 introduced the carbon visual system. Migrating a version 1 record
// keeps every layout and view choice but restarts the visual style and accent,
// because version 1 stored its defaults explicitly and would otherwise pin
// every existing viewer to the retired look.
(() => {
  "use strict";

  const STORAGE_KEY = "mocop.preferences.v2";
  const LEGACY_STORAGE_KEY = "mocop.preferences.v1";
  const MAX_OWNER_LENGTH = 64;

  const DEFAULTS = Object.freeze({
    serverSort: "custom",
    serverOrder: Object.freeze([]),
    gpuSort: "host",
    gpuTaskSort: "memory",
    heatMetric: "utilization",
    visualStyle: "carbon",
    accent: "auto",
    density: "comfortable",
    textSize: "standard",
    motion: "normal",
    backgroundVisibility: 38,
    serverFilter: "all",
    showTemperature: true,
    showPower: true,
    me: "",
  });

  const VALUES = Object.freeze({
    serverSort: new Set(["custom", "group", "host", "status", "gpu", "cpu"]),
    gpuSort: new Set(["host", "utilization", "memory", "temperature", "power", "processes"]),
    gpuTaskSort: new Set(["memory", "duration", "name"]),
    heatMetric: new Set(["utilization", "memory", "bandwidth", "temperature"]),
    // "auto" follows the operating system between carbon and paper.
    visualStyle: new Set(["carbon", "paper", "auto", "precision", "glass", "terminal", "ledger"]),
    // "auto" keeps the style's own accent.
    accent: new Set(["auto", "cobalt", "cyan", "violet", "emerald", "amber", "rose"]),
    density: new Set(["comfortable", "compact"]),
    textSize: new Set(["standard", "large"]),
    motion: new Set(["normal", "reduce"]),
    serverFilter: new Set(["all", "issues", "busy", "available", "stale"]),
  });

  function create({ storage, safeStoredHosts }) {
    const pick = (stored, key) => (VALUES[key].has(stored[key]) ? stored[key] : DEFAULTS[key]);
    const flag = (stored, key) => (typeof stored[key] === "boolean" ? stored[key] : DEFAULTS[key]);

    function backgroundVisibility(value) {
      return Number.isInteger(value) && value >= 15 && value <= 70 ? value : DEFAULTS.backgroundVisibility;
    }

    // An owner name is display text only; control characters are refused.
    function owner(value) {
      return typeof value === "string" && value.length <= MAX_OWNER_LENGTH && !/[\u0000-\u001f\u007f]/.test(value)
        ? value : DEFAULTS.me;
    }

    function validate(stored) {
      if (!stored || typeof stored !== "object" || Array.isArray(stored)) return { ...DEFAULTS, serverOrder: [] };
      return {
        serverSort: pick(stored, "serverSort"),
        serverOrder: safeStoredHosts(stored.serverOrder),
        gpuSort: pick(stored, "gpuSort"),
        gpuTaskSort: pick(stored, "gpuTaskSort"),
        heatMetric: pick(stored, "heatMetric"),
        visualStyle: pick(stored, "visualStyle"),
        accent: pick(stored, "accent"),
        density: pick(stored, "density"),
        textSize: pick(stored, "textSize"),
        motion: pick(stored, "motion"),
        backgroundVisibility: backgroundVisibility(stored.backgroundVisibility),
        serverFilter: pick(stored, "serverFilter"),
        showTemperature: flag(stored, "showTemperature"),
        showPower: flag(stored, "showPower"),
        me: owner(stored.me),
      };
    }

    function read(key) {
      try {
        return JSON.parse(storage.getItem(key) || "null");
      } catch (_error) {
        return null;
      }
    }

    function save(values) {
      try {
        storage.setItem(STORAGE_KEY, JSON.stringify(validate(values)));
        return true;
      } catch (_error) {
        // Rendering stays available when storage is disabled or full.
        return false;
      }
    }

    function load() {
      const current = read(STORAGE_KEY);
      if (current) return validate(current);
      const legacy = read(LEGACY_STORAGE_KEY);
      if (!legacy) return validate(null);
      const migrated = validate({ ...legacy, visualStyle: DEFAULTS.visualStyle, accent: DEFAULTS.accent });
      if (save(migrated)) {
        try {
          storage.removeItem(LEGACY_STORAGE_KEY);
        } catch (_error) {
          // The migrated record already wins on the next load.
        }
      }
      return migrated;
    }

    return Object.freeze({ DEFAULTS, VALUES, STORAGE_KEY, LEGACY_STORAGE_KEY, validate, load, save });
  }

  globalThis.MocopPreferences = Object.freeze({ create });
})();
