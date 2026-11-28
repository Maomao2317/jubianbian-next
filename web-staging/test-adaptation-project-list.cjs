const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const path = require("node:path");

const handlers = {};
const rows = [
  { dataset: { projectTitle: "Moon Harbor" }, hidden: false },
  { dataset: { projectTitle: "River House" }, hidden: false },
];
const noMatches = { hidden: true };
const main = {
  innerHTML: "",
  querySelectorAll(selector) { return selector === "[data-adapt-project-row]" ? rows : []; },
  querySelector(selector) { return selector === "[data-adapt-filter-empty]" ? noMatches : null; },
};
let mobile = false;
let projects = [
  { id: "moon", title: "Moon Harbor", updatedAt: "2026-11-27T00:00:00Z" },
  { id: "river", title: "River House", updatedAt: "2026-11-26T00:00:00Z" },
];
const deletions = [];
const previewProject = {
  id: "pilot",
  title: "账册疑云",
  revision: 1,
  episodeCount: 1,
  sources: [{ id: "scene-1", label: "第一场", text: "内景：账房\n沈砚查验账册。" }],
  warnings: [],
  billing: { projectPoints: 20, episodePoints: 20, rewritePoints: 8 },
  preview: {
    source_id: "scene-1",
    cast: [{ id: "C1", original_name: "沈砚", name: "沈砚", role: "户部账房", biography: "精于查账，追查账册下落。", relationships: "", motivation: "查明账册去向。" }],
    scene: { setting: "内景：账房", blocks: [{ speaker_id: "C1", text: "账目有误。" }] },
    decisions: ["强化查账线索"],
    questions: [],
  },
  outputs: {},
  locks: [],
};
const context = vm.createContext({
  window: {
    AppAPI: {
      async adaptation(path = "", method = "GET") {
        if (method === "DELETE") {
          deletions.push(path);
          projects = projects.filter(item => "/" + item.id !== path);
          return;
        }
        if (path === "/pilot") return previewProject;
        assert.equal(path, "");
        return projects;
      },
    },
    matchMedia: () => ({ matches: mobile, addEventListener() {} }),
    confirm: () => true,
  },
  document: {
    getElementById: id => id === "main" ? main : null,
    addEventListener: (name, handler) => { (handlers[name] ||= []).push(handler); },
  },
  location: { hash: "#/adaptation" },
  setTimeout: () => 1,
  clearTimeout() {},
  console,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "js/adaptation.js"), "utf8"), context);

(async () => {
  await context.window.AdaptationStudio.open("", value => value);
  assert.match(main.innerHTML, /aria-label="按项目标题筛选"/);
  assert.match(main.innerHTML, /data-adapt-project-delete="moon"/);
  assert.match(main.innerHTML, /data-adapt-project-delete="river"/);

  const filter = {
    value: "moon",
    matches: selector => selector === "[data-adapt-project-filter]",
  };
  for (const handler of handlers.input) handler({ target: filter });
  assert.deepEqual(rows.map(row => row.hidden), [false, true]);
  assert.equal(noMatches.hidden, true);
  filter.value = "no such title";
  for (const handler of handlers.input) handler({ target: filter });
  assert.equal(noMatches.hidden, false);

  const button = {
    dataset: { adaptProjectDelete: "moon", projectTitle: "Moon Harbor" },
    disabled: false,
    isConnected: true,
    closest: selector => selector === "[data-adapt-project-delete]" ? button : null,
  };
  const event = {
    preventDefault() {},
    target: { closest: selector => selector === "[data-adapt-project-delete]" ? button : null },
  };
  for (const handler of handlers.click) handler(event);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(deletions, ["/moon"]);
  assert.doesNotMatch(main.innerHTML, /data-adapt-project-delete="moon"/);

  mobile = true;
  await context.window.AdaptationStudio.open("", value => value);
  assert.match(main.innerHTML, /按项目标题筛选/);
  assert.doesNotMatch(main.innerHTML, /data-adapt-project-delete=/);

  mobile = false;
  await context.window.AdaptationStudio.open("pilot", value => value);
  assert.match(main.innerHTML, /对照试改/);
  assert.match(main.innerHTML, /出场人物：沈砚/);
  assert.match(main.innerHTML, /人物介绍/);
  assert.match(main.innerHTML, /户部账房/);
  assert.match(main.innerHTML, /精于查账，追查账册下落。/);
  console.log("Adaptation project list checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
