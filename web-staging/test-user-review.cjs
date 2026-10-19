const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const path = require("node:path");
const handlers = {};
const nodes = new Map();
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", textContent: "", dataset: {},
    classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, querySelectorAll() { return []; } });
  return nodes.get(selector);
};
const calls = [];
const script = { title: "第1集", scenes: [{ heading: "客厅", blocks: [{ type: "dialogue", speaker: "林长风", text: "我们出发吧。" }] }] };
const task = { id: "one", status: "review", stage: "review", title: "第1集", fileName: "1.mp4", batchId: "batch", createdAt: new Date().toISOString(), result: script,
  quality: { issues: [{ severity: "重点核对", description: "00:32–00:38：这段画面中的字幕或文字可能有遗漏，建议回看原视频核对。" }] } };
const api = {
  getTask: async id => ({ ...task, id }),
  getAdminTask: async id => ({ ...task, id, quality: { blockingIssues: [{ severity: "P0", description: "独立 OCR 覆盖率 0%" }] }, episodes: [task, { ...task, id: "two" }] }),
  approveTask: async id => { calls.push(["single", id]); task.status = "done"; },
  approveTasks: async ids => { calls.push(["batch", ...ids]); },
  listTasks: async () => [], getQueueSummary: async () => ({}),
  getAdminOverview: async () => ({ users: {}, tasks: {} }),
  getAdminUsers: async () => ({ total: 1, items: [{ id: "user", name: "test", credits: 1330, totalUsed: 73, taskCount: 999 }] }),
};
const context = vm.createContext({
  window: { addEventListener() {}, AppAPI: api },
  document: { addEventListener: (event, handler) => { (handlers[event] ||= []).push(handler); },
    querySelector: node, querySelectorAll: () => [] },
  location: { protocol: "http:" }, Intl, Date, Map, Set, console, setTimeout: () => 1,
  clearTimeout() {}, clearInterval() {}, requestAnimationFrame() {},
});
context.window.location = context.location;
vm.runInContext(fs.readFileSync(path.join(__dirname, "js/config.js"), "utf8"), context);
const source = fs.readFileSync(path.join(__dirname, "js/app.js"), "utf8");
vm.runInContext(source.replace("  boot();", "  window.testUI = { normalizeUserTask, statusBadge, taskRow, batchRow, renderDetail, openUserReview, scriptPreview, renderAdmin, renderHeader, state };"), context);
const ui = context.window.testUI;
const click = async (action, id) => {
  const button = { dataset: { action, id }, textContent: "操作", closest: () => ({ querySelectorAll: () => [] }) };
  for (const handler of handlers.click) {
    await handler({ target: { closest: selector => selector === "[data-action]" ? button : null } });
  }
};
(async () => {
  const shells = { title: "合集", scenes: [
    { heading: "1-1 日 内 客厅", blocks: [{ type: "dialogue", speaker: "甲", text: "我们出发。" }] },
    { heading: "1-2 日 外 空标题", blocks: [{ type: "screen_text", text: "我们出发。", suppressInOutput: true }] },
    { heading: "1-3 日 外 街道", blocks: [{ type: "screen_text", text: "三年后" }] },
    { heading: "2-1 日 外 空标题", blocks: [] },
    { heading: "2-2 日 内 客厅", blocks: [{ type: "transition", transitionType: "flashback" }] },
  ] };
  const savedShells = JSON.stringify(shells);
  const preview = ui.scriptPreview(shells);
  assert.doesNotMatch(preview, /空标题|1-3|2-2/);
  assert.match(preview, /1-2 日 外 街道/);
  assert.match(preview, /2-1 日 内 客厅/);
  assert.match(preview, /三年后/);
  assert.match(preview, /闪回/);
  assert.equal((preview.match(/<section class="scene">/g) || []).length, 3);
  assert.equal(JSON.stringify(shells), savedShells);
  assert.equal(ui.normalizeUserTask(task), task);
  assert.match(ui.statusBadge(task), /待复核/);
  assert.match(ui.taskRow(task), /查看并复核/);
  assert.doesNotMatch(ui.taskRow(task), /data-action="download"/);
  ui.state.current = task;
  ui.state.view = "detail";
  ui.state.taskId = "one";
  ui.renderDetail();
  assert.match(node("#main").innerHTML, /我们出发吧/);
  assert.match(node("#main").innerHTML, /重点核对.*00:32–00:38/);
  assert.doesNotMatch(node("#main").innerHTML, /ASR|OCR|覆盖率|P0/);
  assert.deepEqual(Array.from(context.window.APP_CONFIG.statusFilters, item => item.id), ["all", "queued", "running", "done"]);
  assert.match(node("#main").innerHTML, /确认复核并开放下载/);
  assert.doesNotMatch(node("#main").innerHTML, /data-action="download"/);
  const group = { id: "batch", title: "测试剧", tasks: [task, { ...task, id: "two" }] };
  assert.match(ui.batchRow(group), /批量复核（2集）/);
  assert.match(ui.batchRow(group), /等待用户复核/);
  ui.state.tasks = group.tasks;
  await click("review-batch", "batch");
  assert.match(node("#modalRoot").innerHTML, /确认通过并开放下载（2集）/, node("#toast").textContent);
  assert.deepEqual(calls, []); // Opening the preview never approves anything.
  await click("confirm-user-review");
  assert.deepEqual(calls[0], ["batch", "one", "two"]);
  await click("review-task", "one");
  await click("confirm-user-review");
  assert.deepEqual(calls[1], ["single", "one"]);
  assert.match(node("#main").innerHTML, /data-action="download"/);
  await click("admin-review", "one");
  assert.match(node("#modalRoot").innerHTML, /任务详情/);
  assert.match(node("#modalRoot").innerHTML, /我们出发吧/);
  assert.match(node("#modalRoot").innerHTML, /选择剧集/);
  assert.match(node("#modalRoot").innerHTML, /P0.*独立 OCR 覆盖率 0%/);
  assert.match(node("#modalRoot").innerHTML, /消耗 0.0 分钟/);
  assert.doesNotMatch(node("#modalRoot").innerHTML, /data-action="admin-approve"|免费重试|免费重新复核/);
  assert.doesNotMatch(source, /admin-done-recheck|admin-review-retry/);
  ui.renderHeader();
  assert.doesNotMatch(node("#header").innerHTML, /核心识别测试版/);
  ui.state.view = "admin";
  ui.state.adminTab = "users";
  ui.state.profile = { role: "admin" };
  await ui.renderAdmin();
  assert.match(node("#main").innerHTML, /1330.0<\/strong> 积分/);
  assert.match(node("#main").innerHTML, /73.0 \/ 266.0 分钟/);
  assert.doesNotMatch(node("#main").innerHTML, /999 \/|调整分钟额度/);
  console.log("User review UI checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
