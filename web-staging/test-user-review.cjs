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
  quality: { blockingIssues: [{ severity: "P0", description: "请核对缺失画面" }] } };
const api = {
  getTask: async id => ({ ...task, id }),
  getAdminTask: async id => ({ ...task, id, episodes: [task, { ...task, id: "two" }] }),
  approveTask: async id => { calls.push(["single", id]); task.status = "done"; },
  approveTasks: async ids => { calls.push(["batch", ...ids]); },
  listTasks: async () => [], getQueueSummary: async () => ({}),
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
vm.runInContext(source.replace("  boot();", "  window.testUI = { normalizeUserTask, statusBadge, taskRow, batchRow, renderDetail, openUserReview, state };"), context);
const ui = context.window.testUI;
const click = async (action, id) => {
  const button = { dataset: { action, id }, textContent: "操作", closest: () => ({ querySelectorAll: () => [] }) };
  for (const handler of handlers.click) {
    await handler({ target: { closest: selector => selector === "[data-action]" ? button : null } });
  }
};
(async () => {
  assert.equal(ui.normalizeUserTask(task), task);
  assert.match(ui.statusBadge(task), /待复核/);
  assert.match(ui.taskRow(task), /查看并复核/);
  assert.doesNotMatch(ui.taskRow(task), /data-action="download"/);
  ui.state.current = task;
  ui.state.view = "detail";
  ui.state.taskId = "one";
  ui.renderDetail();
  assert.match(node("#main").innerHTML, /我们出发吧/);
  assert.match(node("#main").innerHTML, /P0.*请核对缺失画面/);
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
  assert.doesNotMatch(node("#modalRoot").innerHTML, /data-action="admin-approve"|免费重试|免费重新复核/);
  assert.doesNotMatch(source, /admin-done-recheck|admin-review-retry/);
  console.log("User review UI checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
