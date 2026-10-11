import assert from "node:assert/strict";

export async function verifyTrainingJourney(cdp, waitForEvaluation, anonymousMode) {
  if (anonymousMode) {
    assert.equal(await cdp.evaluate('document.querySelector("#training-jobs").hidden'), true);
    return;
  }
  await cdp.evaluate('document.querySelector("#settings-dialog").close()');
  await waitForEvaluation(cdp, 'document.querySelectorAll("#training-job-cards .tj-card").length === 2');
  const summary = await cdp.evaluate(`(() => ({
    text: document.querySelector("#training-jobs-summary").textContent,
    status: document.querySelector("#training-jobs-status").textContent,
    provider: document.querySelector(".tj-card").textContent,
    stored: document.querySelectorAll(".tj-card")[1].textContent,
    allocated: document.querySelector(".tj-card .tj-gpu").textContent,
    observed: document.querySelectorAll(".tj-card .tj-gpu")[1].textContent,
    submitted: document.querySelector(".tj-submitted").textContent,
    runtime: document.querySelector(".tj-runtime").textContent,
    rowHeight: document.querySelector(".tj-card").getBoundingClientRect().height,
    listCanvases: document.querySelectorAll("#training-job-cards canvas").length,
    images: document.querySelectorAll("#training-jobs img").length,
    injected: Boolean(globalThis.trainingInjected),
  }))()`);
  assert.match(summary.text, /2 个活跃 · 3 条记录/);
  assert.match(summary.status, /60 秒采集/);
  assert.match(summary.provider, /GPU 0/);
  assert.match(summary.provider, /显存 未知/);
  assert.match(summary.provider, /厂商原生值/);
  assert.match(summary.allocated, /8 张/);
  assert.match(summary.observed, /4 张观测 \/ 副本/);
  assert.match(summary.submitted, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.match(summary.runtime, /1小时 30分已运行/);
  assert(summary.rowHeight <= 64, `training row is too tall: ${summary.rowHeight}`);
  assert.equal(summary.listCanvases, 0);
  assert.match(summary.stored, /1 个副本/);
  assert.equal(summary.images, 0);
  assert.equal(summary.injected, false);

  await cdp.evaluate(`(() => {
    const filter = document.querySelector("#training-jobs-filter");
    filter.value = "failed";
    filter.dispatchEvent(new Event("change"));
  })()`);
  assert.equal(await cdp.evaluate('document.querySelectorAll("#training-job-cards .tj-card").length'), 1);
  await cdp.evaluate(`(() => {
    const filter = document.querySelector("#training-jobs-filter");
    filter.value = "all";
    filter.dispatchEvent(new Event("change"));
    const search = document.querySelector("#training-jobs-search");
    search.value = "demo-1";
    search.dispatchEvent(new Event("input"));
  })()`);
  assert.equal(await cdp.evaluate('document.querySelectorAll("#training-job-cards .tj-card").length'), 1);
  await cdp.evaluate('document.querySelector(".tj-detail-button").click()');
  await waitForEvaluation(cdp, 'document.querySelector(".tj-logs") !== null');
  const storedDetail = await cdp.evaluate('document.querySelector("#training-job-detail-content").textContent');
  assert.match(storedDetail, /GPU 3 · 利用率 0%/);
  assert.match(storedDetail, /输出已截断/);
  assert.equal(await cdp.evaluate('document.querySelector("#training-job-detail-content script") !== null'), false);

  await cdp.evaluate(`(() => {
    document.querySelector("#training-job-dialog").close();
    const search = document.querySelector("#training-jobs-search");
    search.value = "";
    search.dispatchEvent(new Event("input"));
    const filter = document.querySelector("#training-jobs-filter");
    filter.value = "active";
    filter.dispatchEvent(new Event("change"));
  })()`);
  // Closing queues a close event. Let that finish before testing a new open.
  await new Promise(resolve => setTimeout(resolve, 50));
  await cdp.evaluate('document.querySelector(".tj-detail-button").click()');
  await waitForEvaluation(cdp, 'document.querySelector(".tj-logs") !== null');
  assert.match(await cdp.evaluate('document.querySelector("#training-job-detail-content").textContent'), /厂商原生单位/);
  const safe = await cdp.evaluate(`(() => ({
    script: document.querySelector("#training-job-detail-content script") !== null,
    injected: Boolean(globalThis.trainingInjected),
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    dialogWidth: document.querySelector("#training-job-dialog").getBoundingClientRect().width,
  }))()`);
  assert.equal(safe.script, false);
  assert.equal(safe.injected, false);
  assert.equal(safe.overflow, false);
  assert(safe.dialogWidth <= 390, `training dialog overflows mobile: ${safe.dialogWidth}`);
  await cdp.evaluate('document.querySelector("#training-job-dialog").close()');
  console.log("training browser journey passed: filters, multi-GPU detail, native units, safe text, mobile");
}
