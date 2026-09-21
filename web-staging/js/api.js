/**
 * Data access layer. Views only call AppAPI and never fetch directly.
 * MockApi mirrors the future FastAPI contract so switching environments does
 * not require changing page rendering or business states.
 */
(function (global) {
  const cfg = () => global.APP_CONFIG;
  const STORAGE_KEY = "jubianbian-mock-v2";

  const sampleScript = {
    version: "1.0",
    title: "雨夜来客",
    characters: [
      { id: "character_001", name: "管家", fallbackName: "人物A", confidence: 0.94 },
      { id: "character_002", name: "小女孩", fallbackName: "人物B", confidence: 0.91 },
    ],
    scenes: [
      {
        id: "scene_001",
        heading: "1-1 夜 外 老宅门廊",
        location: "傅家别墅门外",
        characters: ["管家", "小女孩"],
        environment: "密集雨声。石材台阶被雨水打湿，远处闪电映亮门廊。",
        blocks: [
          { type: "action", text: "门铃响起。管家撑伞快步走到铁门前，隔着栏杆望向门外。" },
          { type: "dialogue", speaker: "管家", emotion: "疑惑", text: "小朋友，你找谁？" },
          { type: "action", text: "小女孩抬起头，雨水顺着发梢落下。她攥紧手中的旧钥匙。" },
          { type: "dialogue", speaker: "小女孩", emotion: "紧张", text: "我找傅临川。" },
          { type: "dialogue", speaker: "管家", emotion: "震惊", text: "你怎么会有这把钥匙？" },
        ],
      },
      {
        id: "scene_002",
        heading: "1-2 夜 内 傅家客厅",
        location: "傅家客厅",
        characters: ["管家", "小女孩", "傅临川"],
        environment: "壁炉轻响。客厅暖光与窗外冷雨形成强烈反差。",
        blocks: [
          { type: "action", text: "管家领着小女孩进入客厅。傅临川放下手中的文件，目光停在她的钥匙上。" },
          { type: "dialogue", speaker: "傅临川", emotion: "克制", text: "这把钥匙是谁给你的？" },
          { type: "dialogue", speaker: "小女孩", emotion: "迟疑", text: "妈妈说，见到你就把它交给你。" },
        ],
      },
    ],
  };

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function createDefaultStore() {
    return {
      profile: {
        id: "local-user",
        name: "测试账号",
        credits: cfg().billing.freeMinutes,
        plan: "体验版",
      },
      tasks: [
        {
          id: "demo-001",
          title: "雨夜来客 · 第 1 集",
          fileName: "雨夜来客_第01集.mp4",
          status: "done",
          stage: "done",
          progressPercent: 100,
          durationSec: 261,
          estimatedMinutes: 5,
          creditsUsed: 5,
          createdAt: new Date(Date.now() - 18 * 60 * 1000).toISOString(),
          completedAt: new Date(Date.now() - 9 * 60 * 1000).toISOString(),
          result: sampleScript,
          quality: { dialogueCoverage: 98, speakerConfidence: 95, warnings: 1 },
        },
      ],
    };
  }

  function loadStore() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (saved && saved.profile && Array.isArray(saved.tasks)) return saved;
    } catch (_) {
      // A fresh mock store is enough when localStorage is unavailable or stale.
    }
    return createDefaultStore();
  }

  let mockStore = loadStore();

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(mockStore));
    } catch (_) {}
  }

  function syncTask(task) {
    if (task.status !== "queued" && task.status !== "running") return task;
    const elapsed = Math.max(0, (Date.now() - new Date(task.createdAt).getTime()) / 1000);
    const states = [
      { until: 2, status: "queued", stage: "queued", progress: 4 },
      { until: 6, status: "running", stage: "probing", progress: 12 },
      { until: 12, status: "running", stage: "transcribing", progress: 34 },
      { until: 18, status: "running", stage: "vision", progress: 62 },
      { until: 23, status: "running", stage: "merging", progress: 82 },
      { until: 27, status: "running", stage: "exporting", progress: 94 },
    ];
    const current = states.find((item) => elapsed < item.until);
    if (current) {
      task.status = current.status;
      task.stage = current.stage;
      task.progressPercent = current.progress;
      return task;
    }
    task.status = "done";
    task.stage = "done";
    task.progressPercent = 100;
    task.completedAt = new Date().toISOString();
    task.creditsUsed = task.estimatedMinutes;
    task.result = clone(sampleScript);
    task.result.title = task.title;
    task.quality = { dialogueCoverage: 98, speakerConfidence: 95, warnings: 1 };
    persist();
    return task;
  }

  function matchStatus(task, status) {
    return !status || status === "all" || task.status === status;
  }

  function scriptToMarkdown(task) {
    const script = task.result;
    const lines = [`# ${script.title}`, ""];
    if (Array.isArray(script.characterProfiles) && script.characterProfiles.length) {
      lines.push("## 人物表", "");
      script.characterProfiles.forEach((profile) => {
        const details = [profile.appearance, profile.clothing].filter(Boolean).join("；");
        lines.push(`- ${profile.name}${details ? `：${details}` : ""}`);
      });
      lines.push("");
    }
    script.scenes.forEach((scene) => {
      lines.push(`## ${scene.heading}`, "");
      if (scene.summary) lines.push(`【剧情衔接】${scene.summary}`, "");
      lines.push(`出场人物：${scene.characters.join("、")}`, "");
      if (scene.environment) lines.push(`【环境】${scene.environment}`, "");
      scene.blocks.forEach((block) => {
        if (block.type === "dialogue") {
          lines.push(`${block.speaker}${block.uncertain ? "【需核对】" : ""}：${block.text}`);
        } else if (block.type === "vo" || block.type === "os") {
          const label = block.speaker && !["旁白", "未知说话人", "OS"].includes(block.speaker)
            ? `${block.speaker} VO`
            : "VO";
          lines.push(`${label}${block.inferred ? "（推断）" : ""}：${block.text}`);
        } else if (block.type === "sound") {
          lines.push(`【${block.category || "音效"}】${block.text}`);
        } else if (block.type === "emotion") {
          lines.push(`【情绪】${block.text}`);
        } else {
          lines.push(`▲ ${block.text}`);
        }
      });
      lines.push("");
    });
    return lines.join("\n");
  }

  const MockApi = {
    async getProfile() {
      await delay(90);
      return clone(mockStore.profile);
    },

    async listTasks({ keyword = "", status = "all" } = {}) {
      await delay(120);
      mockStore.tasks.forEach(syncTask);
      const query = keyword.toLocaleLowerCase();
      return clone(mockStore.tasks.filter((task) => {
        const haystack = `${task.title} ${task.fileName || ""}`.toLocaleLowerCase();
        return (!query || haystack.includes(query)) && matchStatus(task, status);
      }));
    },

    async getTask(id) {
      await delay(100);
      const task = mockStore.tasks.find((item) => item.id === id);
      if (!task) throw new Error("任务不存在或已被删除");
      syncTask(task);
      return clone(task);
    },

    async createTask(payload) {
      await delay(180);
      const { title, files, estimatedMinutes, durationSec } = payload;
      const file = files && files[0] ? files[0] : payload.file;
      if (!file) throw new Error("请选择视频文件");
      if (estimatedMinutes > mockStore.profile.credits) {
        throw new Error(`额度不足，当前剩余 ${mockStore.profile.credits} 分钟`);
      }
      const id = "task-" + Date.now();
      const task = {
        id,
        title: title || file.name.replace(/\.[^.]+$/, ""),
        fileName: file.name,
        fileSize: file.size,
        status: "queued",
        stage: "queued",
        progressPercent: 4,
        durationSec,
        estimatedMinutes,
        creditsUsed: 0,
        createdAt: new Date().toISOString(),
        result: null,
      };
      mockStore.profile.credits -= estimatedMinutes;
      mockStore.tasks.unshift(task);
      persist();
      return clone(task);
    },

    async retryTask(id) {
      await delay(140);
      const task = mockStore.tasks.find((item) => item.id === id);
      if (!task) throw new Error("任务不存在");
      task.status = "queued";
      task.stage = "queued";
      task.progressPercent = 4;
      task.createdAt = new Date().toISOString();
      task.error = null;
      persist();
      return clone(task);
    },

    async deleteTask(id) {
      await delay(90);
      mockStore.tasks = mockStore.tasks.filter((item) => item.id !== id);
      persist();
    },

    async getExport(taskId, format) {
      await delay(80);
      const task = mockStore.tasks.find((item) => item.id === taskId);
      if (!task || task.status !== "done" || !task.result) throw new Error("剧本还不能下载");
      const markdown = scriptToMarkdown(task);
      const content = format === "txt"
        ? markdown.replace(/^#{1,6}\s+/gm, "").replace(/\*\*/g, "")
        : markdown;
      return {
        fileName: `${task.title}.${format}`,
        mimeType: format === "txt" ? "text/plain;charset=utf-8" : "text/markdown;charset=utf-8",
        content,
      };
    },

    async resetDemo() {
      mockStore = createDefaultStore();
      persist();
      return clone(mockStore);
    },
  };

  const HttpApi = {
    url(path, params) {
      let value = cfg().api.baseUrl + path;
      Object.entries(params || {}).forEach(([key, item]) => {
        value = value.replace(":" + key, encodeURIComponent(item));
      });
      return value;
    },

    async request(path, options = {}) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), cfg().api.timeoutMs);
      try {
        const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
        const response = await fetch(path, {
          headers: isFormData ? { ...(options.headers || {}) } : { "Content-Type": "application/json", ...(options.headers || {}) },
          signal: controller.signal,
          ...options,
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.message || payload.detail || `请求失败（${response.status}）`);
        }
        return response.status === 204 ? null : response.json();
      } catch (error) {
        if (error.name === "AbortError") throw new Error("请求超时，请稍后重试");
        throw error;
      } finally {
        clearTimeout(timer);
      }
    },

    getProfile() {
      return this.request(this.url(cfg().api.endpoints.me));
    },
    listTasks(query) {
      const search = new URLSearchParams(query || {}).toString();
      return this.request(this.url(cfg().api.endpoints.tasks) + (search ? "?" + search : ""));
    },
    getTask(id) {
      return this.request(this.url(cfg().api.endpoints.task, { id }));
    },
    createTask(payload) {
      const form = new FormData();
      form.append("file", payload.file);
      form.append("title", payload.title || "");
      form.append("durationSec", String(payload.durationSec || 0));
      return this.request(this.url(cfg().api.endpoints.createTask), { method: "POST", body: form });
    },
    retryTask(id) {
      return this.request(this.url(cfg().api.endpoints.task, { id }) + "/retry", { method: "POST" });
    },
    deleteTask(id) {
      return this.request(this.url(cfg().api.endpoints.task, { id }), { method: "DELETE" });
    },
    async getExport(taskId, format) {
      const url = this.url(cfg().api.endpoints.download, { id: taskId });
      const response = await fetch(`${url}?fmt=${encodeURIComponent(format)}`);
      if (!response.ok) throw new Error("下载失败，请重试");
      const rawName = response.headers.get("X-Filename") || `script.${format}`;
      let fileName = rawName;
      try { fileName = decodeURIComponent(rawName); } catch (_) {}
      return { fileName, blob: await response.blob() };
    },
  };

  global.AppAPI = cfg().api.useMock ? MockApi : HttpApi;
})(window);
