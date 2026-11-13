// Execute the real API/metadata functions with deterministic, non-network media.
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");

const apiSource = fs.readFileSync(path.join(__dirname, "js/api.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "js/app.js"), "utf8");

async function uploadBatch(count, behavior = {}) {
  const files = Array.from({ length: count }, (_, index) => ({ name: `${index + 1}.mp4`, size: 1024, type: "video/mp4" }));
  const puts = new Map(), completes = new Map(), aborts = [];
  let reservationCalls = 0, reservationPayload;
  let active = 0, peak = 0;
  const delays = [];
  const context = vm.createContext({
    window: { APP_CONFIG: { billing: { freeMinutes: 1000 }, api: { useMock: false, baseUrl: "/api", timeoutMs: 1000, batchTimeoutMs: 10000 }, upload: { parallelUploads: 4 } } },
    AbortController, URLSearchParams, console,
    setTimeout(fn, ms) { delays.push(ms); return setTimeout(fn, ms >= 1000 ? ms : 0); }, clearTimeout,
    async fetch(url) {
      const index = Number(url.split("/").at(-1));
      puts.set(index, (puts.get(index) || 0) + 1);
      active++; peak = Math.max(peak, active);
      await new Promise(resolve => setImmediate(resolve));
      active--;
      const status = behavior.putStatus && behavior.putStatus(index, puts.get(index)) || 200;
      return { ok: status === 200, status, headers: { get() { return null; } }, async json() { return {}; } };
    },
  });
  vm.runInContext(apiSource, context);
  const api = context.window.AppAPI;
  api.request = async (url, options = {}) => {
    if (url.endsWith("/uploads/batch")) {
      reservationCalls += 1;
      reservationPayload = JSON.parse(options.body || "{}");
      return { uploads: files.map((file, index) => ({
        inputIndex: index, url: `/object/${index}`, mode: "object", completeUrl: `/complete/${index}`, abortUrl: `/abort/${index}`,
      })) };
    }
    const index = Number(url.split("/").at(-1));
    if (url.startsWith("/abort/")) { aborts.push(index); return { status: "failed" }; }
    completes.set(index, (completes.get(index) || 0) + 1);
    const status = behavior.completeStatus && behavior.completeStatus(index, completes.get(index)) || 200;
    if (status !== 200) {
      const error = new Error(`complete ${status}`);
      error.status = status;
      error.retryAfter = behavior.noRetryAfter ? 0 : status === 409 ? 0.001 : 0;
      throw error;
    }
    return { status: "running" };
  };
  let error;
  const progress = [];
  try {
    await api.createTask({ files, durations: files.map(() => 10), onProgress: n => progress.push(n),
      sourceLanguage: Object.hasOwn(behavior, "sourceLanguage") ? behavior.sourceLanguage : "zh",
      outputLanguage: Object.hasOwn(behavior, "outputLanguage") ? behavior.outputLanguage : "en" });
  }
  catch (failure) { error = failure; }
  return { puts, completes, aborts, peak, progress, error, delays, reservationCalls, reservationPayload };
}

async function metadata() {
  let active = 0, peak = 0, reads = 0, revoked = 0;
  const listeners = {};
  const context = vm.createContext({
    window: { APP_CONFIG: {}, AppAPI: {}, addEventListener(name, listener) { listeners[name] = listener; } },
    document: { addEventListener() {}, createElement() {
      return {
        removeAttribute() {}, load() {},
        set src(file) {
          reads++; active++; peak = Math.max(peak, active);
          if (file.timeout) { active--; return; }
          setImmediate(() => { active--; this.duration = file.duration; this.onloadedmetadata(); });
        },
      };
    } },
    URL: { createObjectURL(file) { return file; }, revokeObjectURL() { revoked++; } },
    setTimeout(fn, ms) { return setTimeout(fn, ms === 30000 ? 100 : ms); }, clearTimeout, console,
  });
  vm.runInContext(appSource.replace("  boot();", "  window.testUI = { readVideoDuration, readVideoDurations, state, protectActiveUpload };"), context);
  const files = Array.from({ length: 999 }, (_, index) => ({ name: `${index}.mp4`, duration: index + 1 }));
  const ui = context.window.testUI;
  const durations = await ui.readVideoDurations(files);
  assert.equal(peak, 4);
  assert.equal(reads, 999);
  assert.equal(revoked, 999);
  assert.deepEqual(Array.from(durations), files.map(file => file.duration));
  await ui.readVideoDurations(files);
  assert.equal(reads, 999, "reselecting a cached file must not create another video");
  const invalid = { name: "bad.mp4", duration: NaN };
  await assert.rejects(ui.readVideoDuration(invalid));
  invalid.duration = 10;
  assert.equal(await ui.readVideoDuration(invalid), 10, "failed metadata reads must not poison cache");
  const stalled = { name: "stalled.mp4", duration: 10, timeout: true };
  await assert.rejects(ui.readVideoDuration(stalled), /stalled.mp4/);
  stalled.timeout = false;
  assert.equal(await ui.readVideoDuration(stalled), 10, "metadata timeout must clear cache and release its object URL");
  assert.equal(revoked, reads);
  let prevented = false;
  const navigation = { preventDefault() { prevented = true; }, returnValue: null };
  listeners.beforeunload(navigation);
  assert.equal(prevented, false);
  ui.state.upload.active = true;
  assert.equal(ui.protectActiveUpload(navigation), "");
  assert.equal(prevented, true);
  assert.equal(navigation.returnValue, "");
}

(async () => {
  for (const count of [100, 999]) {
    const batch = await uploadBatch(count, { completeStatus: (_, attempt) => attempt === 1 ? 503 : 200 });
    assert.equal(batch.error, undefined);
    assert.equal(batch.peak, 4);
    assert.equal(batch.progress.length, count);
    assert.equal(batch.progress.at(-1), count);
    assert.equal(batch.aborts.length, 0);
    assert.ok([...batch.puts.values()].every(n => n === 1), "complete retries must never re-PUT an episode");
    assert.ok([...batch.completes.values()].every(n => n === 2));
  }
  const visibleLater = await uploadBatch(1, { completeStatus: (_, attempt) => attempt === 1 ? 409 : 200 });
  assert.equal(visibleLater.error, undefined);
  assert.equal(visibleLater.puts.get(0), 1);
  const cancelled = await uploadBatch(1, { completeStatus: () => 409, noRetryAfter: true });
  assert.equal(cancelled.completes.get(0), 1, "ordinary conflict must not be retried");
  assert.equal(cancelled.aborts.length, 1);
  const transientPut = await uploadBatch(1, { putStatus: (_, attempt) => attempt === 1 ? 503 : 200 });
  assert.equal(transientPut.error, undefined);
  assert.equal(transientPut.puts.get(0), 2);
  const forbidden = await uploadBatch(1, { putStatus: () => 403 });
  assert.equal(forbidden.puts.get(0), 1, "deterministic 4xx must not waste bandwidth retrying");
  assert.equal(forbidden.aborts.length, 1);
  assert.equal(forbidden.error.failedCount, 1);
  assert.equal(forbidden.reservationPayload.sourceLanguage, "zh");
  assert.equal(forbidden.reservationPayload.outputLanguage, "en");
  const missingLanguages = await uploadBatch(1, { sourceLanguage: "", outputLanguage: "" });
  assert.match(missingLanguages.error.message, /选择原剧语言和生成语言/);
  assert.equal(missingLanguages.reservationCalls, 0, "missing languages must be rejected before reserving uploads");
  await metadata();
  console.log("100/999 batch upload, retry isolation, metadata bounds/cache checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
