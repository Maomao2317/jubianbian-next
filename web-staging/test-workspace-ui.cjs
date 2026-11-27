const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");

const handlers = {};
const nodes = new Map();
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {
    innerHTML: "", textContent: "", value: "", dataset: {}, disabled: false,
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, getBoundingClientRect() { return { height: 0, top: 0, bottom: 0 }; },
  });
  return nodes.get(selector);
};

const api = {
  listTasks: async () => [],
  getQueueSummary: async () => ({ waitMinutes: 0, blocked: false, workerConcurrency: 8 }),
};
const location = { hash: "#/workspace", protocol: "http:", href: "http://localhost/#/workspace" };
const context = vm.createContext({
  window: {
    AppAPI: api,
    location,
    addEventListener() {},
    matchMedia: () => ({ matches: false }),
    Node: null,
  },
  location,
  document: {
    addEventListener(event, handler) { (handlers[event] ||= []).push(handler); },
    querySelector(selector) { return ["#main", "#modalRoot", "#toast", "#taskTitle", "#taskFrom", "#taskTo"].includes(selector) ? node(selector) : null; },
    querySelectorAll() { return []; },
    activeElement: null,
  },
  Intl, Date, Map, Set, URL, Blob, console,
  setTimeout: () => 1, clearTimeout() {}, clearInterval() {}, requestAnimationFrame(fn) { fn(); },
});
context.window.window = context.window;
context.window.document = context.document;
vm.runInContext(fs.readFileSync(path.join(__dirname, "js/config.js"), "utf8"), context);
const source = fs.readFileSync(path.join(__dirname, "js/app.js"), "utf8");
vm.runInContext(source.replace("  boot();", "  window.testUI = { state, formatDate, parseHash, renderWorkspaceHome, renderList, renderDetail, createModalMarkup, openCreateModal, openComingSoonModal, filteredUserTasks, renderAdmin };"), context);
const ui = context.window.testUI;

const click = async dataset => {
  const actionElement = { dataset, disabled: false, textContent: "", isConnected: true, setAttribute() {} };
  const target = { closest(selector) { return selector === "[data-action]" ? actionElement : null; } };
  for (const handler of handlers.click || []) await handler({ target, preventDefault() {}, stopPropagation() {} });
};

const task = (id, createdAt, status = "done", extra = {}) => ({
  id, createdAt, status, stage: status, title: `任务 ${id}`, fileName: `${id}.mp4`, durationSec: 62,
  estimatedMinutes: 2, result: status === "done" ? { title: id, scenes: [] } : null, ...extra,
});

(async () => {
  ui.state.profile = { name: "测试用户", credits: 1000 };
  ui.state.tasks = [
    task("one", "2026-11-01T00:00:00+08:00"),
    task("two", "2026-11-07T23:59:59+08:00", "running", { sourceLanguage: "en", outputLanguage: "zh" }),
    task("three", "2026-11-08T00:00:00+08:00"),
  ];

  ui.renderWorkspaceHome();
  const home = node("#main").innerHTML;
  assert.match(home, /今天想创作什么/);
  assert.match(home, /视频转剧本/);
  assert.match(home, /多语言视频转剧本/);
  assert.match(home, /剧本优化改编/);
  assert.match(home, /更多精彩[\s\S]*即将发生/);
  assert.match(home, /data-action="create-standard"/);
  assert.match(home, /data-action="create-multilingual"/);

  ui.state.view = "list";
  ui.state.taskDateRange = "custom";
  ui.state.taskFrom = "2026-11-01";
  ui.state.taskTo = "2026-11-07";
  assert.deepEqual(Array.from(ui.filteredUserTasks(), item => item.id), ["one", "two"], "custom date bounds must be inclusive in Asia/Shanghai");
  ui.renderList();
  const list = node("#main").innerHTML;
  assert.match(list, /全部任务/);
  assert.match(list, /创建日期/);
  assert.match(list, /全部时间/);
  assert.match(list, /全部类型/);
  assert.match(list, /多语言剧本/);
  assert.match(list, /近 7 天/);
  assert.match(list, /自定义/);
  assert.match(list, /2026-11-01/);
  assert.doesNotMatch(list, /任务 three/);
  ui.state.adaptationEnabled = true;
  ui.state.tasks[0] = { ...ui.state.tasks[0], result: null };
  ui.renderList();
  assert.match(node("#main").innerHTML, /href="#\/adaptation\/new\/one">改编剧本/);
  assert.doesNotMatch(node("#main").innerHTML, /href="#\/adaptation\/new\/two"/);
  const adaptLink = {};
  const target = { closest: selector => selector === "[data-open]" ? { dataset: { open: "one" } }
    : selector === "a[href]" ? adaptLink : null };
  for (const handler of handlers.click || []) await handler({ target });
  assert.equal(location.hash, "#/workspace", "the row handler must leave the adaptation link to native navigation");
  ui.state.current = task("one", "2026-11-01T00:00:00+08:00");
  ui.renderDetail();
  assert.match(node("#main").innerHTML, /class="primary-btn detail-adapt-link" href="#\/adaptation\/new\/one"/);
  assert.equal(ui.formatDate("2026-11-25T08:44:15+00:00", true), "2026-11-25 16:44:15");
  ui.state.adaptationEnabled = false;
  await click({ action: "task-type-filter", type: "multilingual" });
  assert.deepEqual(Array.from(ui.filteredUserTasks(), item => item.id), ["two"], "type filtering must use the task language metadata");
  assert.doesNotMatch(node("#main").innerHTML, /任务 one/);
  await click({ action: "task-type-filter", type: "all" });
  assert.equal(ui.state.taskType, "all");

  ui.openCreateModal("multilingual");
  let modal = node("#modalRoot").innerHTML;
  assert.match(modal, /创建多语言视频剧本/);
  assert.match(modal, /中文原片/);
  assert.match(modal, /English Source/);
  assert.match(modal, /中英双语/);
  assert.doesNotMatch(modal, /<select[^>]+(?:sourceLanguage|outputLanguage)/);
  assert.match(modal, /data-language-field="outputLanguage"[\s\S]*disabled/);

  await click({ action: "create-standard" });
  assert.equal(ui.state.upload.mode, "standard");
  assert.match(node("#modalRoot").innerHTML, /上传视频生成剧本/);
  await click({ action: "close-modal" });
  await click({ action: "create-multilingual" });
  assert.equal(ui.state.upload.mode, "multilingual");
  assert.match(node("#modalRoot").innerHTML, /创建多语言视频剧本/);

  await click({ action: "select-language", languageField: "sourceLanguage", value: "en" });
  assert.equal(ui.state.upload.sourceLanguage, "en");
  modal = node("#modalRoot").innerHTML;
  assert.match(modal, /data-language-field="sourceLanguage" data-value="en" aria-pressed="true"/);
  await click({ action: "select-language", languageField: "outputLanguage", value: "both" });
  assert.equal(ui.state.upload.outputLanguage, "both");
  assert.match(node("#modalRoot").innerHTML, /data-language-field="outputLanguage" data-value="both" aria-pressed="true"/);

  await click({ action: "show-coming-soon" });
  assert.match(node("#modalRoot").innerHTML, /创作引擎正在精心打磨/);
  assert.match(node("#modalRoot").innerHTML, /节奏优化/);
  assert.match(node("#modalRoot").innerHTML, /台词润色/);

  location.hash = "#/tasks";
  ui.parseHash();
  assert.equal(ui.state.view, "list");
  location.hash = "#/workspace";
  ui.parseHash();
  assert.equal(ui.state.view, "workspace");
  ui.state.profile = { id: "admin", role: "admin", credits: 100 };
  ui.state.view = "admin";
  ui.state.adminTab = "adaptations";
  ui.state.adminOverviewCache = { users: {}, tasks: {}, timestamp: "2026-11-26T00:00:00Z" };
  ui.state.adminOverviewCachedAt = Date.now();
  api.getAdminAdaptations = async query => {
    assert.equal(query.view, "episodes");
    return { total: 1, categories: [{ kind: "episode", credits_charged: 8, api_cost_rmb: 0.04, jobs: 22, estimated_jobs: 2 }],
      items: [{ project_title: "样稿", user_id: "user", user_name: "测试用户", episode: 1,
        credits_charged: 8, api_cost_rmb: 0.04, estimated_jobs: 2, status: "success" }] };
  };
  await ui.renderAdmin();
  assert.match(node("#main").innerHTML, /扣费折合金额/);
  assert.match(node("#main").innerHTML, /¥0\.63/);
  assert.match(node("#main").innerHTML, /第 1 集/);
  assert.match(node("#main").innerHTML, /估算/);
  assert.doesNotMatch(node("#main").innerHTML, /调用明细|实扣 8 积分|模型 Tokens/);
  const adaptationHtml = node("#main").innerHTML;
  assert.match(adaptationHtml, /用户使用积分/);
  assert.match(adaptationHtml, /API 使用成本/);
  const adaptationHeaders = adaptationHtml.match(/<table class="adapt-admin-table"><thead><tr>(.*?)<\/tr><\/thead>/)?.[1];
  assert.ok(adaptationHeaders, "adaptation usage table should render its headers");
  assert.deepEqual([...adaptationHeaders.matchAll(/<th>(.*?)<\/th>/g)].map(match => match[1]), [
    "项目 / 用户", "集数", "当前状态", "用户使用积分", "扣费折合金额", "API 使用成本", "最近更新",
  ]);
  assert.match(adaptationHtml, /用户使用 8\.0 积分/);
  api.getAdminUserDetail = async id => {
    assert.equal(id, "user");
    return { user: { id, name: "测试用户", email: "user@example.test", credits: 92, created_at: "2026-11-26", is_active: true },
      totals: { consumed: 8, recharged: 100 }, tasks: { total: 0 }, adaptationCount: 1,
      ledger: { total: 0, items: [] }, recharges: { total: 0, items: [] }, recentTasks: { total: 0, items: [] } };
  };
  await click({ action: "admin-user-detail", id: "user" });
  assert.match(node("#modalRoot").innerHTML, /注册时间/);
  assert.match(node("#modalRoot").innerHTML, /剩余积分/);
  assert.match(node("#modalRoot").innerHTML, /积分流水/);
  assert.match(node("#modalRoot").innerHTML, /充值流水/);
  assert.match(node("#modalRoot").innerHTML, /92\.0/);
  console.log("Workspace, adaptation billing and administrator user detail UI checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
