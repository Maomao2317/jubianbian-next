const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "js/adaptation.js"), "utf8");

test("preview acknowledges clicks, saves edits, and recovers polling without resubmitting", async () => {
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
    dataset: { adapt: "preview" }, isConnected: true,
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
  const result = { ...saved, revision: 2, preview: {
    source_id: "S1", cast: [{ id: "C1", name: "掌柜" }],
    scene: { setting: "账房", blocks: [{ speaker_id: "C1", text: "账册留在这里。" }] },
    decisions: [], questions: []
  } };
  let progressRequests = 0;
  let focused = false;
  const heading = { focus: () => { focused = true; }, scrollIntoView() {} };
  const main = {
    innerHTML: "",
    querySelector(selector) {
      if (selector.includes("textarea")) return source;
      if (selector === "#adapt-preview h2") return heading;
      if (selector.includes("data-adapt")) return button;
      return null;
    }
  };
  const window = {
    innerHeight: 720,
    AppAPI: {
      async adaptation(route, method, body) {
        requests.push({ route, method, body });
        if (route.endsWith("/progress")) {
          if (++progressRequests === 1) throw new Error("network");
          return { job: null };
        }
        if (method === "PATCH") return saved;
        if (method === "POST") return { ...saved, job: "preview" };
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
  assert.match(main.innerHTML, /正在试改/);

  await timers.shift()();
  assert.match(status.textContent, /正在重查/);
  await timers.shift()();

  assert.equal(requests.filter(request => request.method === "POST").length, 1);
  assert.match(main.innerHTML, /账册留在这里。/);
  assert.equal(focused, true);
});
