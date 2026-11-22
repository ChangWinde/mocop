// Command palette: ranks pages, hosts, owners, and programs for one typed query
// and recognizes a GPU demand such as "找 2 张 80G" or "4x24". Pure functions;
// app.js owns the dialog and runs the chosen command.
(() => {
  "use strict";

  const KIND_ORDER = Object.freeze({ demand: 0, page: 1, host: 2, owner: 3, program: 4 });
  const MAX_GPUS = 256;
  const MAX_VRAM_GIB = 512;
  const DEMAND_PATTERNS = Object.freeze([
    // "2x80", "2 张 ≥ 80G", "2*24gib"
    /(\d{1,3})\s*(?:张|x|×|\*)\s*(?:≥|>=)?\s*(\d{1,3})\s*(?:g|gb|gib)?\b/i,
    // "找 2 张", "找2张80G", "找 2 张 ≥ 24 GiB"
    /找\s*(\d{1,3})\s*张?\s*(?:(?:≥|>=)?\s*(\d{1,3})\s*(?:g|gb|gib)?)?/i,
  ]);

  const normalize = (value) => String(value ?? "").normalize("NFKC").toLowerCase().trim();

  function create() {
    function parseDemand(query) {
      const text = normalize(query);
      for (const pattern of DEMAND_PATTERNS) {
        const match = text.match(pattern);
        if (!match) continue;
        const gpuCount = Number(match[1]);
        const minVramGiB = match[2] == null ? 0 : Number(match[2]);
        if (gpuCount < 1 || gpuCount > MAX_GPUS || minVramGiB > MAX_VRAM_GIB) return null;
        return Object.freeze({ gpuCount, minVramGiB });
      }
      return null;
    }

    function entries({ pages = [], hosts = [], owners = [], programs = [] }) {
      return Object.freeze([
        ...pages.map((page) => ({ kind: "page", id: page.id, label: page.label, detail: page.detail || "", keywords: page.keywords || "" })),
        ...hosts.map((host) => ({ kind: "host", id: host.host, label: host.label || host.host, detail: host.detail || "", keywords: host.host })),
        ...owners.map((owner) => ({ kind: "owner", id: owner, label: owner, detail: "", keywords: "" })),
        ...programs.map((program) => ({ kind: "program", id: program, label: program, detail: "", keywords: "" })),
      ].map((entry) => Object.freeze(entry)));
    }

    function score(entry, query) {
      const label = normalize(entry.label);
      if (label === query) return 0;
      if (label.startsWith(query)) return 1;
      if (label.split(/[\s/._-]+/).some((word) => word.startsWith(query))) return 2;
      if (label.includes(query)) return 3;
      if (normalize(`${entry.keywords} ${entry.detail}`).includes(query)) return 4;
      return Infinity;
    }

    function search(list, query, limit = 12) {
      const text = normalize(query);
      const results = [];
      const demand = parseDemand(text);
      if (demand) {
        results.push(Object.freeze({
          kind: "demand", id: `${demand.gpuCount}:${demand.minVramGiB}`,
          label: `找 ${demand.gpuCount} 张${demand.minVramGiB ? ` · ≥ ${demand.minVramGiB} GiB` : ""}`,
          detail: "", demand,
        }));
      }
      const ranked = list
        .map((entry) => ({ entry, rank: text ? score(entry, text) : (entry.kind === "page" ? 0 : 5) }))
        .filter((item) => Number.isFinite(item.rank))
        .sort((first, second) => (
          first.rank - second.rank
          || KIND_ORDER[first.entry.kind] - KIND_ORDER[second.entry.kind]
          || first.entry.label.localeCompare(second.entry.label)
        ))
        .map((item) => item.entry);
      return Object.freeze([...results, ...ranked].slice(0, limit));
    }

    return Object.freeze({ parseDemand, entries, search });
  }

  globalThis.MocopCommandPalette = Object.freeze({ create });
})();
