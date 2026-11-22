// Command palette widget: the result list, arrow-key selection, and Enter to
// run. Ranking lives in command-palette.js; what a command does is the
// caller's run(). Nodes are built through the injected factory.
(() => {
  "use strict";

  const KIND_LABELS = Object.freeze({ demand: "找卡", page: "页面", host: "主机", owner: "用户", program: "程序" });

  function create({ create: element, palette, input, results, run }) {
    let entries = [];
    let matches = [];
    let selected = 0;

    function render() {
      results.replaceChildren(...(matches.length ? matches.map((entry, index) => {
        const item = element("button", "palette-item");
        item.type = "button";
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", String(index === selected));
        item.append(element("span", "palette-kind", KIND_LABELS[entry.kind]), element("strong", "", entry.label));
        if (entry.detail) item.append(element("small", "", entry.detail));
        item.addEventListener("click", () => run(entry));
        return item;
      }) : [element("div", "palette-empty", "没有匹配")]));
      results.children[selected]?.scrollIntoView?.({ block: "nearest" });
    }

    function search() {
      selected = 0;
      matches = palette.search(entries, input.value);
      render();
    }

    function open(nextEntries) {
      entries = nextEntries;
      input.value = "";
      search();
    }

    input.addEventListener("input", search);
    input.addEventListener("keydown", (event) => {
      const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
      if (step && matches.length) {
        event.preventDefault();
        selected = (selected + step + matches.length) % matches.length;
        render();
      } else if (event.key === "Enter" && matches[selected]) {
        event.preventDefault();
        run(matches[selected]);
      }
    });

    return Object.freeze({ open, matches: () => matches, selected: () => selected });
  }

  globalThis.MocopPaletteView = Object.freeze({ create });
})();
