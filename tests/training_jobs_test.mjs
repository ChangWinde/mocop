import assert from "node:assert/strict";
import "../src/mocop/static/training_jobs.js";
const {latest,errorText}=globalThis.MocopTrainingJobs;
assert.deepEqual(latest([{ts:20,value:null},{ts:10,value:30}]),{ts:20,value:null});
assert.equal(latest([]),undefined);
assert.match(errorText(429),/频率/);
assert.match(errorText(403),/权限/);
console.log("Training job metric ordering contracts passed");
