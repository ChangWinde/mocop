// Alert noise: how many conditions opened per hour over the situation brief's
// window, judged against alarm-management practice (ANSI/ISA-18.2, EEMUA 191):
// about one alarm per ten minutes is manageable for one person, more than one
// per five minutes is not. Pure projection of GET /api/brief; no DOM, no network.
(() => {
  "use strict";

  const MANAGEABLE_PER_HOUR = 6;
  const OVERLOADED_PER_HOUR = 12;
  const MIN_WINDOW_HOURS = 1 / 60;
  const TOP_RECURRING = 3;

  function create() {
    function summarize(brief) {
      const changes = brief?.changes;
      const end = Date.parse(brief?.generatedAt);
      const start = Date.parse(changes?.coveredFromAt);
      if (!changes || !Number.isFinite(end) || !Number.isFinite(start)) return null;
      const hours = Math.max(MIN_WINDOW_HOURS, (end - start) / 3_600_000);
      const opened = Math.max(0, Number(changes.opened) || 0);
      const perHour = opened / hours;
      const recurring = (changes.recurring || []).slice(0, TOP_RECURRING).map((item) => Object.freeze({
        host: item.host,
        category: item.category,
        resource: item.resource || null,
        openings: item.openings,
        share: opened ? item.openings / opened : 0,
      }));
      let state = "";
      if (perHour > OVERLOADED_PER_HOUR) state = "critical";
      else if (perHour > MANAGEABLE_PER_HOUR) state = "warning";
      return Object.freeze({
        hours, opened, perHour, state, target: MANAGEABLE_PER_HOUR, recurring: Object.freeze(recurring),
      });
    }

    return Object.freeze({ MANAGEABLE_PER_HOUR, OVERLOADED_PER_HOUR, summarize });
  }

  globalThis.MocopAlertNoise = Object.freeze({ create });
})();
