const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "js/adaptation.js"), "utf8");

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
  const result = { ...saved, revision: 2, analysis };
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
  await window.AdaptationStudio.open("/p1");

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
  assert.match(main.innerHTML, /正在分析原稿/);

  await timers.shift()();
  assert.match(status.textContent, /正在重查/);
  await timers.shift()();

  assert.equal(requests.filter(request => request.method === "POST").length, 1);
  assert.match(main.innerHTML, /悬疑翻案/);
  assert.equal(focused, true);
});
