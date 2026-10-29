import assert from "node:assert/strict";
import "../src/mocop/static/training_overview.js";
const snapshot = {sampled_at:1,overview:{known_running_cards:8,unknown_card_jobs:1,running_jobs:2,models:{Example:8},known_active_cards:12,known_queued_cards:0,states:{starting:{cards:4,jobs:1,models:{Example:4}},startup_failed:{cards:4,jobs:1}}},platform:{card_hours:{personal:{raw_used_card_hours:20,used_weighted_card_hours:5,remaining_card_hours:0}},capacity:{rows:[{kind:"training",model:"Example",total:64,starting:null,running:null,allocated:48,free:16}]}}};
const stats=globalThis.MocopTrainingOverview.stats(snapshot);
assert.equal(stats[0][1],"≥ 8 张");
assert.match(stats[0][2],/Example × 8/);
assert.deepEqual(stats.map(s=>s[1]),["≥ 8 张","4 张","4 张"]);
assert.match(stats[2][2],/不计入运行占用/);
const unknown = globalThis.MocopTrainingOverview.stats({sampled_at:1,complete:true,overview:{states:{running:{cards:0,jobs:8,unknown_card_jobs:8},starting:{cards:0},startup_failed:{cards:0}}}});
assert.equal(unknown[0][1],"卡数待同步");
assert.equal(unknown[1][1],"0 张");
assert.equal(unknown.some(row=>row[1].includes("0+")),false);
class Element {
  constructor(tag) {this.tag=tag;this.children=[];}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
  get text() {return `${this.textContent || ""} ${this.children.map(c=>c.text || "").join(" ")}`;}
}
globalThis.document={createElement:tag=>new Element(tag)};
const container=new Element("div");
globalThis.MocopTrainingOverview.render(snapshot,container);
assert.match(container.text,/我的运行中/);
assert.match(container.text,/我的启动中/);
assert.match(container.text,/启动失败 · 近 24 小时/);
assert.match(container.text,/活动申请合计 12/);
assert.match(container.text,/原始卡时 20/);
assert.match(container.text,/剩余额度 0/);
assert.match(container.text,/已分配合计为总卡减剩余/);
console.log("Training overview contracts passed");
