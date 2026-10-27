const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const path = require("node:path");
const nodes = new Map();
const handlers = {};
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", value: "", dataset: {},
    setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} } });
  return nodes.get(selector);
};
const calls = [];
const userCalls = [], taskCalls = [];
let overviewCalls = 0;
const user = { id: "u", name: "测试用户<名称>", email: "user@example.test", credits: 128.4, totalUsed: 2, isActive: true };
const api = {
  getAdminOverview: async () => { overviewCalls++; return { users: { total: 1, active: 1, credits: 128.4 }, tasks: { total: 3, running: 1, queued: 1 }, recentTasks: [{ ...user, status: "done" }] }; },
  getAdminUsers: async query => { userCalls.push(query); return { total: 1, items: [user] }; },
  getAdminTasks: async query => { taskCalls.push(query); return { total: 1, items: [{ ...user,
    title: "测试批次", status: "failed", task_count: 3, failed_count: 1, running_count: 1, queued_count: 1,
    file_name: "3 集", credits_used: 4, created_at: "2026-10-27" }] }; },
  getAdminRecharges: async query => { calls.push(query); return { total: 1, items: [
    { entry_type: "consume", points_amount: -10, balance_after: 90, reason: "test", created_at: "2026-10-27" },
  ] }; },
};
const context = vm.createContext({ window: { AppAPI: api, addEventListener() {} },
  document: { querySelector: node, querySelectorAll: () => [],
    addEventListener: (event, handler) => { (handlers[event] ||= []).push(handler); } },
  location: { protocol: "http:" }, console, Intl, Date, setTimeout, clearTimeout });
vm.runInContext(fs.readFileSync(path.join(__dirname, "js/app.js"), "utf8").replace("  boot();",
  "  window.testUI = { adminLedgerRow, ledgerBadge, adminUserIdentity, renderAdmin, state };"), context);
const ui = context.window.testUI;
const row = (kind, amount, extra = {}) => ui.adminLedgerRow({ entry_type: kind, points_amount: amount,
  balance_after: 100, created_at: "2026-10-27", ...extra });
assert.match(row("recharge", 1651.2, { rmb_amount: 129 }), /ledger-recharge.*充值.*¥ 129.00.*\+1651.2/s);
assert.match(row("refund", 10), /↩.*退费.*ledger-income.*\+10.0/s);
assert.match(row("consume", -10), /−.*扣款.*ledger-expense.*−10.0/s);
assert.doesNotMatch(row("consume", -10), /\+-|¥ 0.00/);
assert.match(row("grant", 25), /★.*赠送.*\+25.0/s);
assert.match(row("adjustment", -2), /⇄.*人工调整.*−2.0/s);
assert.match(row("adjustment", 2), /ledger-income.*\+2.0/s);
assert.match(row("adjustment", 0), /ledger-neutral">0.0/);
assert.match(row("recharge", 10), /未记录/);
assert.match(row("refund", 10, { reason: "task recognition failed" }), /视频识别失败，积分已退回/);
const unsafe = row('<img src=x onerror=alert(1)>', 1, { name: "<script>", reason: "<img>" });
assert.doesNotMatch(unsafe, /<img|<script/);
assert.match(unsafe, /其他变动.*&lt;img&gt;/s);
assert.match(row("__proto__", 1), /其他变动/);
const click = async action => {
  for (const handler of handlers.click) await handler({ target: { closest: selector => selector === "[data-action]" ? { dataset: { action } } : null } });
};
(async () => {
  ui.state.profile = { role: "admin" }; ui.state.view = "admin"; ui.state.adminTab = "credits";
  await ui.renderAdmin();
  assert.match(node("#main").innerHTML, /充值与积分流水.*1 笔流水.*积分变动.*变动后余额/s);
  node("#adminRechargeType").value = "refund";
  ui.state.adminRechargePage = 3;
  await click("admin-recharge-search");
  assert.equal(calls.at(-1).entry_type, "refund");
  assert.equal(calls.at(-1).offset, 0);
  await click("admin-recharge-reset");
  assert.equal(calls.at(-1).entry_type, "all");
  assert.doesNotMatch(node("#main").innerHTML, /<h2>用户管理<\/h2>|后续）/);
  const beforeInvalid = calls.length;
  node("#adminRechargeFrom").value = "2026-10-28";
  node("#adminRechargeTo").value = "2026-10-27";
  await click("admin-recharge-search");
  assert.equal(calls.length, beforeInvalid);
  assert.match(node("#toast").textContent, /开始日期不能晚于结束日期/);
  for (const tab of ["tasks", "usage"]) {
    ui.state.adminTab = tab;
    await ui.renderAdmin();
    const html = node("#main").innerHTML;
    assert.match(html, /<strong>测试用户&lt;名称&gt;<\/strong><small>user@example.test<\/small>/);
    assert.match(html, /测试用户&lt;名称&gt; · user@example.test/);
    assert.match(html, /1 个批次.*部分失败.*进行中 1 · 排队 1 · 失败 1/s);
    assert.match(html, /计费时长/);
    assert.doesNotMatch(html, /测试用户<名称>/);
  }
  ui.state.adminTaskPage = 9;
  await ui.renderAdmin();
  assert.equal(ui.state.adminTaskPage, 0);
  assert.equal(taskCalls.at(-1).offset, 0);
  node("#adminTaskKeyword").value = "测试用户";
  await click("admin-search");
  assert.equal(taskCalls.at(-1).keyword, "测试用户");
  await click("admin-task-reset");
  assert.equal(taskCalls.at(-1).keyword, "");
  assert.equal(taskCalls.at(-1).status, "all");
  const beforeRefresh = overviewCalls;
  await click("admin-refresh");
  assert.equal(overviewCalls, beforeRefresh + 1);
  ui.state.adminTab = "users"; ui.state.profile.id = "u";
  await ui.renderAdmin();
  assert.match(node("#main").innerHTML, /人民币充值.*disabled title="不能停用当前登录账号"/s);
  node("#adminUserKeyword").value = "Test";
  await click("admin-user-search");
  assert.equal(userCalls.at(-1).keyword, "Test");
  await click("admin-user-reset");
  assert.equal(userCalls.at(-1).keyword, "");
  assert.match(ui.adminUserIdentity({ email: "orphan@example.test" }), /未命名用户.*orphan@example.test/);
  console.log("Admin ledger and console identity, filtering, pagination and refresh passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
