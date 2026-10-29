import assert from "node:assert/strict";
import "../src/mocop/static/training_overview.js";
const stats=globalThis.MocopTrainingOverview.stats({overview:{known_running_cards:8,unknown_card_jobs:1,running_jobs:2,models:{Example:8}},platform:{card_hours:{personal:{raw_used_card_hours:20,used_weighted_card_hours:5,remaining_card_hours:0}}}});
assert.equal(stats[0][1],"8+ 张");
assert.match(stats[1][1],/Example × 8/);
assert.equal(stats[2][1],"20 卡·h");
assert.equal(stats[3][1],"5 / 0");
class Element {
  constructor(tag) {this.tag=tag;this.children=[];}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
  get text() {return `${this.textContent || ""} ${this.children.map(c=>c.text || "").join(" ")}`;}
}
globalThis.document={createElement:tag=>new Element(tag)};
const container=new Element("div");
globalThis.MocopTrainingOverview.render({sampled_at:1,overview:{known_running_cards:8,known_active_cards:12,known_starting_cards:4,known_queued_cards:0},platform:{capacity:{rows:[{kind:"training",model:"Example",total:64,starting:null,running:null,allocated:48,free:16}]}}},container);
assert.match(container.text,/运行中总卡数/);
assert.match(container.text,/活动任务申请合计 12/);
assert.match(container.text,/启动中/);
assert.match(container.text,/已在用/);
assert.match(container.text,/已分配合计为总卡减剩余/);
console.log("Training overview contracts passed");
