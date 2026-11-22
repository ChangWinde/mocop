import assert from "node:assert/strict";
import { create as fakeCreate } from "./fake_dom.mjs";

await import("../src/mocop/static/inventory-forms.js");

// Forms and fields need the validity API the leaf calls.
const create = (tag, className, text) => {
  const element = fakeCreate(tag, className, text);
  element.validity = true;
  element.customValidity = "";
  element.reportValidity = () => element.validity;
  element.setCustomValidity = (message) => { element.customValidity = message; };
  return element;
};
const forms = globalThis.MocopInventoryForms.create({ create });
const submit = (form) => {
  let prevented = false;
  form.listeners.get("submit")({ preventDefault() { prevented = true; } });
  return prevented;
};

const saved = [];
let cleared = 0;
const group = forms.groupForm({
  host: "atlas-01", currentGroup: "训练", knownGroups: ["推理", "训练"],
  onSave: (value) => saved.push(value), onClear: () => { cleared += 1; },
});
const [field, suggestions, save, clear] = group.children;
assert.equal(field.value, "训练");
assert.equal(field.getAttribute("list"), "host-groups-atlas-01");
assert.deepEqual(suggestions.children.map((option) => option.value), ["推理", "训练"]);
assert.equal(save.textContent, "保存分组");
clear.click();
assert.equal(cleared, 1);
field.value = "  ";
assert.equal(submit(group), true, "submit never navigates");
assert.deepEqual(saved, [], "a blank group is not saved");
field.value = "推理";
submit(group);
assert.deepEqual(saved, ["推理"]);
assert.equal(forms.groupForm({ host: "h", currentGroup: "", knownGroups: [], onSave() {}, onClear() {} }).children.length, 2, "no suggestions and no clear without a group");

const drafts = [];
const started = [];
let ended = 0;
const maintenance = forms.maintenanceForm({
  host: "atlas-01", maintenance: { reason: "换卡" }, draft: null,
  onDraft: (draft) => drafts.push(draft), onSave: (seconds, reason) => started.push([seconds, reason]), onEnd: () => { ended += 1; },
});
const [reason, duration, start, end] = maintenance.children;
assert.equal(reason.value, "换卡");
assert.equal(duration.value, "14400", "four hours by default");
assert.deepEqual(duration.children.map((option) => option.value), ["3600", "14400", "86400", "604800"]);
assert.equal(start.textContent, "延长维护");
end.click();
assert.equal(ended, 1);
reason.value = "驱动升级";
reason.listeners.get("input")();
assert.deepEqual(drafts.at(-1), { host: "atlas-01", reason: "驱动升级", duration: "14400", focusField: "reason" });
duration.value = "86400";
duration.listeners.get("change")();
assert.equal(drafts.at(-1).focusField, "duration");
submit(maintenance);
assert.deepEqual(started, [[86400, "驱动升级"]]);
assert.equal(drafts.at(-1).focusField, "duration", "saving keeps the last focused field");

reason.value = "   ";
submit(maintenance);
assert.equal(started.length, 1, "a blank reason is refused");
assert.equal(reason.customValidity, "", "the custom message is cleared after it is shown");

const resumed = forms.maintenanceForm({
  host: "b", maintenance: null, draft: { host: "b", reason: "未保存", duration: "3600", focusField: "reason" },
  onDraft() {}, onSave() {}, onEnd() {},
});
assert.equal(resumed.children[0].value, "未保存", "a draft survives a rebuild");
assert.equal(resumed.children[1].value, "3600");
assert.equal(resumed.children.length, 3, "no end button without an active window");

console.log("inventory-forms tests passed");
