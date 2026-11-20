const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");

const context = vm.createContext({
  window: { APP_CONFIG: { upload: { maxDurationMinutes: 6 }, statusText: {} }, AppAPI: {}, addEventListener() {} },
  document: { addEventListener() {} }, console, Intl, Date, URL,
});
const source = fs.readFileSync(path.join(__dirname, "js/app.js"), "utf8");
vm.runInContext(source.replace("  boot();", "  window.testPricing = { state, createModalMarkup, taskPoints, homeTaskRow };"), context);
const ui = context.window.testPricing;
ui.state.profile = { credits: 100 };
Object.assign(ui.state.upload, {
  files: [{ name: "1.mp4", size: 7 }], file: { name: "1.mp4", size: 7 },
  durations: [61], durationSec: 61, estimatedMinutes: 2,
});
for (const sourceLanguage of ["zh", "en"]) {
  for (const outputLanguage of ["zh", "en", "both"]) {
    Object.assign(ui.state.upload, { sourceLanguage, outputLanguage });
    const rate = sourceLanguage === "zh" && outputLanguage === "zh" ? 5 : 8;
    const markup = ui.createModalMarkup();
    assert.ok(markup.includes(`预计消耗 ${(2 * rate).toFixed(1)} 积分`));
    assert.ok(markup.includes(`每分钟 ${rate} 积分`));
    ui.state.profile.credits = 12;
    assert.equal(/data-action="submit-task"[^>]*disabled/.test(ui.createModalMarkup()), rate === 8);
    ui.state.profile.credits = 100;
  }
}
const oldEnglish = { id: "old", status: "done", creditsUsed: 2, sourceLanguage: "en", outputLanguage: "zh" };
assert.equal(ui.taskPoints(oldEnglish), 10);
assert.equal(ui.taskPoints({ ...oldEnglish, pointsPerMinute: 8 }), 16);
assert.equal(ui.taskPoints({ ...oldEnglish, pointsPerMinute: 8, creditsUsed: 0, pointsUsed: 0 }), 0);
assert.ok(ui.homeTaskRow({ ...oldEnglish, pointsPerMinute: 8 }).includes("16.0 积分"));

(async () => {
  const mockContext = vm.createContext({
    window: { APP_CONFIG: { api: { useMock: true }, billing: { freeMinutes: 100 } } },
    localStorage: { getItem() { return null; }, setItem() {} },
    setTimeout(fn) { fn(); }, Date, Intl, URLSearchParams, console,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "js/api.js"), "utf8"), mockContext);
  const api = mockContext.window.AppAPI;
  for (const sourceLanguage of ["zh", "en"]) {
    for (const outputLanguage of ["zh", "en", "both"]) {
      const before = await api.getProfile();
      const result = await api.createTask({ files: [{ name: "1.mp4", size: 7 }],
        durationSec: 61, estimatedMinutes: 2, sourceLanguage, outputLanguage });
      const rate = sourceLanguage === "zh" && outputLanguage === "zh" ? 5 : 8;
      assert.equal(result.tasks[0].pointsPerMinute, rate);
      assert.equal(result.tasks[0].pointsUsed, 2 * rate);
      assert.equal((await api.getProfile()).credits, before.credits - 2 * rate);
    }
  }
  console.log("Language pricing: all language pairs, estimates, history and mock billing passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
