/**
 * 产品与前端配置（后续改这里，尽量别在页面里写死文案/开关/地址）
 *
 * 后面接后端时：
 * 1. 把 api.useMock 改成 false
 * 2. 填好 API.baseUrl
 * 3. 打开 features 里需要的能力（登录、额度）
 */
window.APP_CONFIG = {
  brand: {
    name: "剧编编",
    mark: "编",
    logo: "./assets/logo.svg",
    slogan: "上传短视频，自动整理台词、人物、情绪与画面信息。",
  },

  /** 当前工作台。第一版只有转剧本 */
  defaultTool: "pullfilm",

  /**
   * 顶栏只渲染 enabled=true 的项。
   * 其它工具以后要加，在这里补一条并打开 enabled，不要抄别人产品名。
   */
  nav: [
    { id: "pullfilm", label: "视频转剧本", enabled: true, route: "#/pullfilm" },
  ],

  features: {
    auth: true,
    credits: true,
    recharge: false,
    notify: false,
    multiEpisode: false,
    extraToolsPlaceholder: false,
  },

  statusFilters: [
    { id: "all", label: "全部" },
    { id: "queued", label: "待开始" },
    { id: "running", label: "进行中" },
    { id: "done", label: "已完成" },
    { id: "failed", label: "失败" },
  ],

  statusText: {
    queued: "待开始",
    running: "进行中",
    done: "已完成",
    failed: "失败",
  },

  stageText: {
    queued: "等待处理",
    probing: "读取视频信息",
    transcribing: "提取台词",
    vision: "分析画面与情绪",
    merging: "整理人物与剧本",
    exporting: "生成导出文件",
    done: "识别完成",
    failed: "识别失败",
  },

  pipeline: [
    { id: "probing", label: "读取视频" },
    { id: "transcribing", label: "提取台词" },
    { id: "vision", label: "分析画面" },
    { id: "merging", label: "整理剧本" },
    { id: "exporting", label: "生成文件" },
  ],

  exportFormats: [
    { id: "md", label: "Markdown", ext: ".md" },
    { id: "txt", label: "TXT", ext: ".txt" },
    { id: "docx", label: "Word", ext: ".docx", enabled: false },
  ],

  upload: {
    accept: ".mp4,video/mp4",
    maxSizeMB: 500,
    maxDurationMinutes: 6,
    maxFiles: 1,
    hint: "支持 MP4，单个视频最长 6 分钟、最大 500MB",
  },

  billing: {
    unitLabel: "分钟",
    freeMinutes: 5,
    rounding: "ceil",
  },

  api: {
    useMock: false,
    // 生产环境同源走 /api；直接双击 index.html 时指向本机后端，避免 file:// 请求落回本地文件系统。
    baseUrl: window.location.protocol === "file:" ? "http://127.0.0.1:8000/api" : "/api",
    timeoutMs: 30000,
    endpoints: {
      me: "/me",
      tasks: "/tasks",
      task: "/tasks/:id",
      createTask: "/tasks",
      uploadCredential: "/uploads/credential",
      download: "/tasks/:id/download",
    },
  },
};
