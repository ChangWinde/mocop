// Node settings forms: a host's group and its maintenance window. Builds
// forms through the injected factory and reports the operator's input through
// callbacks; app.js owns the writes and the pending state.
(() => {
  "use strict";

  const MAINTENANCE_DURATIONS = Object.freeze([[3600, "1 小时"], [14400, "4 小时"], [86400, "24 小时"], [604800, "7 天"]]);
  const DEFAULT_DURATION = "14400";

  function create({ create: element }) {
    function input(attributes) {
      const field = element("input");
      Object.assign(field, { type: "text", ...attributes });
      return field;
    }

    function groupForm({ host, currentGroup, knownGroups, onSave, onClear }) {
      const form = element("form", "host-group-editor");
      const group = input({ maxLength: 48, placeholder: "例如：训练集群 / 推理集群", value: currentGroup || "" });
      group.setAttribute("aria-label", `${host} 所属分组`);
      form.append(group);
      if (knownGroups.length) {
        const suggestions = element("datalist");
        suggestions.id = `host-groups-${host}`;
        knownGroups.forEach((name) => {
          const option = element("option");
          option.value = name;
          suggestions.append(option);
        });
        group.setAttribute("list", suggestions.id);
        form.append(suggestions);
      }
      const save = element("button", "primary-action", currentGroup ? "保存分组" : "加入分组");
      save.type = "submit";
      form.append(save);
      if (currentGroup) {
        const clear = element("button", "inline-action danger-action", "移出分组");
        clear.type = "button";
        clear.addEventListener("click", onClear);
        form.append(clear);
      }
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!form.reportValidity() || !group.value.trim()) return;
        onSave(group.value);
      });
      return form;
    }

    // A pending or failed save rebuilds the list; the draft carries unsaved
    // input and the focused field across those rebuilds.
    function maintenanceForm({ host, maintenance, draft, onDraft, onSave, onEnd }) {
      let current = draft;
      const form = element("form", "maintenance-editor");
      const reason = input({
        required: true, maxLength: 120, placeholder: "维护原因，例如：驱动升级",
        value: draft ? draft.reason : (maintenance?.reason || ""),
      });
      reason.setAttribute("aria-label", `${host} 维护原因`);
      const duration = element("select");
      duration.setAttribute("aria-label", `${host} 维护时长`);
      const selected = draft?.duration || DEFAULT_DURATION;
      MAINTENANCE_DURATIONS.forEach(([seconds, label]) => {
        const option = element("option", "", label);
        option.value = String(seconds);
        option.selected = String(seconds) === selected;
        duration.append(option);
      });
      duration.value = selected;
      const updateDraft = (focusField) => {
        current = { host, reason: reason.value, duration: duration.value, focusField: focusField || current?.focusField || null };
        onDraft(current);
      };
      reason.addEventListener("input", () => updateDraft("reason"));
      reason.addEventListener("focus", () => updateDraft("reason"));
      duration.addEventListener("change", () => updateDraft("duration"));
      duration.addEventListener("focus", () => updateDraft("duration"));
      const save = element("button", "primary-action", maintenance ? "延长维护" : "开始维护");
      save.type = "submit";
      form.append(reason, duration, save);
      if (maintenance) {
        const end = element("button", "inline-action danger-action", "立即结束");
        end.type = "button";
        end.addEventListener("click", onEnd);
        form.append(end);
      }
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const trimmed = reason.value.trim();
        if (!trimmed) {
          reason.setCustomValidity("请输入有效的维护原因");
          form.reportValidity();
          reason.setCustomValidity("");
          return;
        }
        if (!form.reportValidity()) return;
        updateDraft();
        onSave(Number(duration.value), trimmed);
      });
      return form;
    }

    return Object.freeze({ MAINTENANCE_DURATIONS, groupForm, maintenanceForm });
  }

  globalThis.MocopInventoryForms = Object.freeze({ create });
})();
