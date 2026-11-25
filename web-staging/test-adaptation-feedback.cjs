const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "js/adaptation.js"), "utf8");

test("the project list returns to the workspace and shows the full update timestamp", async () => {
  const main = { innerHTML: "" };
  const timestamp = "2026-11-25T08:44:15+00:00";
  const window = { AppAPI: { adaptation: async () => [{ id: "p1", title: "样稿", updatedAt: timestamp }] } };
  vm.runInNewContext(script, {
    window, document: { getElementById: () => main, addEventListener() {} },
  });
  await window.AdaptationStudio.open("", iso => {
    assert.equal(iso, timestamp);
    return "2026-11-25 16:44:15";
  });
  assert.match(main.innerHTML, /href="#\/workspace"[^>]*>.*返回工作台/);
  assert.match(main.innerHTML, /<time datetime="2026-11-25T08:44:15\+00:00">2026-11-25 16:44:15<\/time>/);
});

test("analysis acknowledges clicks, saves edits, and recovers polling without resubmitting", async () => {
  const listeners = {};
  const requests = [];
  const timers = [];
  const alert = { hidden: true, textContent: "" };
  const status = { hidden: true, textContent: "" };
  const feedback = {
    querySelectorAll: () => [alert, status],
    querySelector: selector => selector.includes("error") ? alert : status
  };
  const panel = { querySelector: () => feedback };
  const button = {
    dataset: { adapt: "analyze" }, isConnected: true,
    closest: () => panel,
    getBoundingClientRect: () => ({ top: 0, bottom: 100 })
  };
  const original = "记录还在柜子里，今天不能离开。";
  const source = { value: original + "去开门。" };
  const done = {
    id: "p1", title: "样稿", goal: "改成古装漫剧", warnings: [], usage: {}, outputs: {},
    revision: 0, sources: [{ id: "S1", label: "第一场", text: original }]
  };
  const saved = { ...done, revision: 1, sources: [{ ...done.sources[0], text: source.value }] };
  const analysis = {
    title: "账房旧案", summary: "账册失踪后，账房開始查找线索。", characters: ["掌柜"],
    directions: [{ id: "D1", title: "悬疑翻案", description: "围绕账册查案。", reason: "原稿出现了账册。", goal: "改成悬疑短剧。" }],
    keep_options: [], questions: [], preview_source_id: "S1"
  };
  const result = { ...saved, revision: 2, analysis, usage: { analyze: { model: "private-model", apiCostRmb: 0.0851 } } };
  let progressRequests = 0;
  let focused = false;
  const heading = { focus: () => { focused = true; }, scrollIntoView() {} };
  const main = {
    innerHTML: "",
    querySelector(selector) {
      if (selector.includes("textarea")) return source;
      if (selector === ".adapt-analysis-panel h2") return heading;
      if (selector.includes("data-adapt")) return button;
      return null;
    }
  };
  const window = {
    innerHeight: 720,
    confirm: () => true,
    AppAPI: {
      async adaptation(route, method, body) {
        requests.push({ route, method, body });
        if (route.endsWith("/progress")) {
          if (++progressRequests === 1) throw new Error("network");
          return { job: null };
        }
        if (method === "PATCH") return saved;
        if (method === "POST") return { ...saved, job: "analyze" };
        return requests.length === 1 ? done : result;
      }
    }
  };
  vm.runInNewContext(script, {
    window,
    document: { getElementById: () => main, addEventListener: (name, handler) => { listeners[name] = handler; } },
    setTimeout: callback => timers.push(callback),
    clearTimeout() {}
  });
  window.AdaptationStudio.setAvailability({ enabled: true, ready: true, model: "private-model" });
  await window.AdaptationStudio.open("/p1");
  assert.doesNotMatch(main.innerHTML, /private-model|可用模型/);
  assert.match(main.innerHTML, /分析原稿 · 20 积分/);

  const click = { target: { closest: () => button } };
  listeners.click(click);
  assert.equal(status.textContent, "正在提交…");
  assert.equal(button.disabled, true);
  listeners.click(click);
  await new Promise(setImmediate);

  assert.equal(requests[1].method, "PATCH");
  assert.equal(requests[1].body.text, source.value);
  assert.equal(requests[2].body.revision, 1);
  assert.equal(requests[2].body.kind, "analyze");
  assert.equal(requests[2].body.expected_points, 20);
  assert.match(main.innerHTML, /正在分析原稿/);

  await timers.shift()();
  assert.match(status.textContent, /正在重查/);
  await timers.shift()();

  assert.equal(requests.filter(request => request.method === "POST").length, 1);
  assert.match(main.innerHTML, /悬疑翻案/);
  assert.match(main.innerHTML, /没有找到可以直接引用的保留项/);
  assert.doesNotMatch(main.innerHTML, /private-model|最近模型调用|0\.0851/);
  assert.equal(focused, true);
});

test("episode revisions send user notes and confirm eight points from the first revision", async () => {
  const listeners = {};
  const requests = [];
  const confirmations = [];
  const button = { dataset: { adapt: "episode", number: "1" }, closest: () => null };
  const draft = { scenes: [{ setting: "商号", blocks: [{ text: "沈青按住账册。" }] }], continuity: "回头查账" };
  const project = {
    id: "p1", title: "样稿", goal: "突出商号查账", warnings: [], usage: {}, revision: 1,
    episodeCount: 2, confirmed: true, locks: [],
    sources: [{ id: "S1", label: "第一场", text: "沈青按住账册。" }],
    outputs: { 1: { draft, version: 1, locked: false, stale: false } },
    plan: { title: "样稿", premise: "查账", cast: [], episodes: [
      { number: 1, beats: "账册被动过", hook: "掌柜来了", source_ids: ["S1"] }
    ] }
  };
  const main = { innerHTML: "", querySelector: () => null };
  const window = {
    confirm: message => { confirmations.push(message); return true; },
    AppAPI: {
      adaptationDownloadUrl: (_, format = "md") => "/download?fmt=" + format,
      async adaptation(route, method, body) {
        if (method === "POST") { requests.push(body); return null; }
        return project;
      }
    }
  };
  vm.runInNewContext(script, {
    window,
    document: { getElementById: () => main, addEventListener: (name, handler) => { listeners[name] = handler; } },
    setTimeout, clearTimeout
  });
  const click = { target: { closest: () => button } };
  const input = value => listeners.input({ target: {
    value, dataset: { adaptRevisionNotes: "1" },
    matches: selector => selector === "[data-adapt-revision-notes]"
  } });

  await window.AdaptationStudio.open("/p1");
  assert.match(main.innerHTML, /用 AI 修改本集 · 8 积分/);
  assert.match(main.innerHTML, /写第 2 集 · 20 积分/);
  assert.match(main.innerHTML, /href="\/download\?fmt=md">导出已写正文 · Markdown/);
  assert.match(main.innerHTML, /href="\/download\?fmt=txt">导出已写正文 · TXT/);
  input("缩短开场");
  listeners.click(click);
  await new Promise(setImmediate);
  assert.equal(requests[0].notes, "缩短开场");
  assert.equal(requests[0].expected_points, 8);
  assert.equal(confirmations.length, 1);
  assert.match(confirmations[0], /8 积分/);

  project.outputs[1].version = 2;
  await window.AdaptationStudio.open("/p1");
  assert.match(main.innerHTML, /用 AI 修改本集 · 8 积分/);
  assert.match(main.innerHTML, /查看历史稿 · 当前第 2 版/);
  input("加强当面对质");
  listeners.click(click);
  await new Promise(setImmediate);
  assert.equal(requests[1].notes, "加强当面对质");
  assert.equal(requests[1].expected_points, 8);
  assert.equal(confirmations.length, 2);
  assert.match(confirmations[1], /8 积分/);
});
