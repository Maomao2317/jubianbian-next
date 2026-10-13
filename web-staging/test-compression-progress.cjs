// Exercise the real rendering functions without a browser or network calls.
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const path = require("node:path");
const context = vm.createContext({
  window: { addEventListener() {}, AppAPI: {} },
  document: { addEventListener() {} },
  location: { protocol: "http:" },
  Intl, Date, Map, Set, console,
});
context.window.location = context.location;
vm.runInContext(fs.readFileSync(path.join(__dirname, "js/config.js"), "utf8"), context);
const source = fs.readFileSync(path.join(__dirname, "js/app.js"), "utf8");
assert.match(source, /  boot\(\);\r?\n\}\)\(\)/);
vm.runInContext(source.replace("  boot();", "  window.testUI = { statusBadge, taskRow, batchRow, pipeline, listDataSignature, state };"), context);
const ui = context.window.testUI;
const task = { id: "task", title: "第1集", fileName: "1.mp4", status: "running", stage: "compressing",
  stageProgressPercent: 12, progressPercent: 17, createdAt: new Date().toISOString() };
assert.match(ui.statusBadge(task), /服务器压缩中 12%/);
assert.match(ui.taskRow(task), /width:12%/);
assert.match(ui.taskRow(task), /aria-valuenow="12"/);
assert.match(ui.pipeline(task), /压缩 12%/);
assert.match(ui.pipeline(task), /可以离开本页/);
assert.doesNotMatch(ui.pipeline(task), /勿关闭|本地压缩/);
const batch = { id: "batch", title: "测试短剧", tasks: [task, { ...task, id: "queued", stage: "compression_queued" }] };
assert.match(ui.batchRow(batch), /压缩中 1 集/);
assert.match(ui.batchRow(batch), /等待压缩 1 集/);
ui.state.expandedBatches.batch = true;
assert.match(ui.batchRow(batch), /服务器压缩中 12%/);
assert.match(ui.taskRow({ ...task, stage: "vision" }), /width:17%/);
assert.match(ui.taskRow({ ...task, status: "done", stage: "done" }), /已完成/);
assert.doesNotMatch(ui.taskRow({ ...task, status: "failed", stage: "failed" }), /role="progressbar"/);
assert.notEqual(ui.listDataSignature([task], {}), ui.listDataSignature([{ ...task, stageProgressPercent: 13 }], {}));
assert.match(ui.statusBadge({ ...task, stageProgressPercent: null }), /0%/);
for (const [stage, label] of [["video_upload_retry", "识别视频上传重试中"], ["evidence_wait", "采集语音和字幕中"]]) {
  const waiting = { ...task, stage, stageProgressPercent: null };
  assert.ok(ui.statusBadge(waiting).includes(label));
  assert.ok(ui.pipeline(waiting).includes(label));
  assert.ok(ui.pipeline(waiting).includes("原片已保留"));
}
console.log("Compression progress UI checks passed");
