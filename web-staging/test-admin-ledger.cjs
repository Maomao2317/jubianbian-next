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
const api = {
  getAdminOverview: async () => ({ users: {}, tasks: {} }),
  getAdminUsers: async () => ({ total: 0, items: [] }),
  getAdminRecharges: async query => { calls.push(query); return { total: 1, items: [
    { entry_type: "consume", points_amount: -10, balance_after: 90, reason: "test", created_at: "2026-10-27" },
  ] }; },
};
const context = vm.createContext({ window: { AppAPI: api, addEventListener() {} },
  document: { querySelector: node, querySelectorAll: () => [],
    addEventListener: (event, handler) => { (handlers[event] ||= []).push(handler); } },
  location: { protocol: "http:" }, console, Intl, Date, setTimeout, clearTimeout });
vm.runInContext(fs.readFileSync(path.join(__dirname, "js/app.js"), "utf8").replace("  boot();",
  "  window.testUI = { adminLedgerRow, ledgerBadge, renderAdmin, state };"), context);
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
  console.log("Admin ledger types, signs, escaping, filtering and reset passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
