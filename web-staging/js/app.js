/**
 * Hash-routed workspace UI. The page renders from the API contract only; mock
 * mode and the future FastAPI implementation share the same view code.
 */
(function () {
  const cfg = window.APP_CONFIG;
  const api = window.AppAPI;
  const $ = (selector, root = document) => root.querySelector(selector);

  const state = {
    view: "list",
    taskId: "",
    keyword: "",
    status: "all",
    tasks: [],
    current: null,
    profile: null,
    authMode: "login",
    authBusy: false,
    authCooldown: 0,
    authCooldownTimer: null,
    authEmail: "",
    menuTaskId: "",
    upload: { file: null, durationSec: 0, reading: false, error: "" },
    pollTimer: null,
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toast(text, tone = "default") {
    const element = $("#toast");
    element.textContent = text;
    element.dataset.tone = tone;
    element.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => element.classList.remove("show"), 2400);
  }

  function formatDuration(seconds) {
    if (!seconds) return "时长待读取";
    const total = Math.round(seconds);
    const minutes = Math.floor(total / 60);
    const rest = String(total % 60).padStart(2, "0");
    return `${minutes}:${rest}`;
  }

  function formatSize(bytes) {
    if (!bytes) return "";
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function formatDate(iso) {
    const date = new Date(iso);
    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function timeAgo(iso) {
    const minutes = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.round(hours / 24)} 天前`;
  }

  function estimatedMinutes(seconds) {
    return Math.max(1, Math.ceil(seconds / 60));
  }

  function parseHash() {
    const parts = (location.hash || "#/tasks").replace(/^#\/?/, "").split("/");
    state.view = parts[0] === "task" && parts[1] ? "detail" : "list";
    state.taskId = state.view === "detail" ? parts[1] : "";
  }

  function go(path) {
    location.hash = "#/" + path.replace(/^#\/?/, "");
  }

  function statusBadge(task) {
    const text = task.status === "running"
      ? cfg.stageText[task.stage]
      : cfg.statusText[task.status] || task.status;
    return `<span class="badge ${task.status}"><span class="badge-dot"></span>${escapeHtml(text)}</span>`;
  }

  function renderHeader() {
    const credits = state.profile ? state.profile.credits : "--";
    const initial = state.profile && state.profile.name ? state.profile.name.charAt(0) : "用";
    $("#header").innerHTML = `
      <div class="header-inner">
        <a class="brand" href="#/tasks" aria-label="返回任务列表">
          <img class="brand-logo" src="${cfg.brand.logo}" alt="" />
          <span>${cfg.brand.name}</span>
        </a>
        <span class="edition">核心识别测试版</span>
        <div class="header-right">
          <div class="credit-pill" title="每识别 1 分钟视频消耗 1 分钟额度">
            <span class="credit-label">可用额度</span>
            <strong>${credits}</strong><span>分钟</span>
          </div>
          <button class="text-btn" type="button" data-action="recharge">充值</button>
          <button class="avatar" type="button" data-action="account" title="个人信息">${escapeHtml(initial)}</button>
        </div>
      </div>`;
  }

  function renderAuth() {
    const register = state.authMode === "register";
    const cooldown = state.authCooldown > 0 ? `${state.authCooldown}s 后重发` : "获取验证码";
    $("#header").innerHTML = `
      <div class="auth-brand"><a class="brand" href="#"><img class="brand-logo" src="${cfg.brand.logo}" alt="" /><span>${cfg.brand.name}</span></a><span>AI 视频剧本工作台</span></div>`;
    $("#main").innerHTML = `
      <section class="auth-page">
        <div class="auth-card">
          <div class="auth-kicker">欢迎使用剧编编</div>
          <h1>${register ? "创建账号" : "登录"}</h1>
          <p class="auth-subtitle">${register ? "注册后即可开始整理你的短视频剧本" : "登录后继续你的剧本创作"}</p>
          <form id="authForm" novalidate>
            ${register ? `<div class="auth-field"><label for="authName">昵称</label><input id="authName" autocomplete="name" placeholder="怎么称呼你？" maxlength="40" /></div>` : ""}
            <div class="auth-field"><label for="authEmail">邮箱</label><input id="authEmail" type="email" autocomplete="email" placeholder="name@example.com" value="${escapeHtml(state.authEmail)}" required /></div>
            ${register ? `<div class="auth-field"><label for="authCode">邮箱验证码</label><div class="code-row"><input id="authCode" inputmode="numeric" maxlength="6" placeholder="6 位验证码" required /><button class="code-btn" type="button" data-action="request-code" ${state.authCooldown ? "disabled" : ""}>${cooldown}</button></div><small class="auth-hint">验证码有效期 10 分钟</small></div>` : ""}
            <div class="auth-field"><label for="authPassword">密码</label><div class="password-row"><input id="authPassword" type="password" autocomplete="${register ? "new-password" : "current-password"}" placeholder="至少 8 位，含字母和数字" required /><button class="password-toggle" type="button" data-action="toggle-password" data-target="authPassword" aria-label="显示密码" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"></path><circle cx="12" cy="12" r="2.5"></circle></svg></button></div>${register ? `<small class="auth-hint">至少 8 位，且必须同时包含字母和数字</small>` : ""}</div>
            ${register ? `<div class="auth-field"><label for="authPassword2">确认密码</label><div class="password-row"><input id="authPassword2" type="password" autocomplete="new-password" placeholder="再次输入密码" required /><button class="password-toggle" type="button" data-action="toggle-password" data-target="authPassword2" aria-label="显示确认密码" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"></path><circle cx="12" cy="12" r="2.5"></circle></svg></button></div></div>` : ""}
            <button class="auth-submit" type="submit" data-action="auth-submit" ${state.authBusy ? "disabled" : ""}>${state.authBusy ? "处理中…" : register ? "注册并进入工作台" : "登录"}</button>
          </form>
          <div class="auth-switch">${register ? "已有账号？" : "没有账号？"}<button type="button" data-action="auth-switch">${register ? "立即登录" : "注册"}</button></div>
        </div>
      </section>`;
    const email = $("#authEmail");
    if (email) email.focus();
  }

  function openAccountModal() {
    const profile = state.profile || {};
    $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask">
      <div class="modal account-modal" role="dialog" aria-modal="true" aria-labelledby="accountTitle">
        <div class="modal-head"><div><span class="modal-kicker">账号中心</span><h2 id="accountTitle">个人信息</h2></div><button class="close-btn" type="button" data-action="close-modal" aria-label="关闭">×</button></div>
        <div class="account-profile"><div class="account-avatar">${escapeHtml((profile.name || "用").charAt(0))}</div><div><strong>${escapeHtml(profile.name || "剧编编用户")}</strong><span>${escapeHtml(profile.email || "")}</span></div></div>
        <dl class="account-details"><div><dt>注册邮箱</dt><dd>${escapeHtml(profile.email || "")}</dd></div><div><dt>当前方案</dt><dd>${escapeHtml(profile.plan || "体验版")}</dd></div><div><dt>可用额度</dt><dd>${escapeHtml(profile.credits ?? "0")} 分钟</dd></div><div><dt>注册时间</dt><dd>${profile.createdAt ? escapeHtml(formatDate(profile.createdAt)) : "-"}</dd></div></dl>
        <div class="modal-actions"><button class="cancel-btn" type="button" data-action="close-modal">返回</button><button class="danger-btn" type="button" data-action="logout">退出登录</button></div>
      </div></div>`;
  }

  function taskMenu(task) {
    if (state.menuTaskId !== task.id) return "";
    const download = task.status === "done"
      ? `<button type="button" data-action="download" data-id="${task.id}" data-format="md">下载 Markdown</button>`
      : "";
    const retry = task.status === "failed"
      ? `<button type="button" data-action="retry" data-id="${task.id}">重新识别</button>`
      : "";
    return `<div class="task-menu" role="menu">
      <button type="button" data-action="open-task" data-id="${task.id}">查看详情</button>
      ${download}${retry}
      <button class="danger-item" type="button" data-action="ask-delete" data-id="${task.id}">删除任务</button>
    </div>`;
  }

  function taskRow(task) {
    const meta = [
      task.fileName || "未命名视频",
      formatDuration(task.durationSec),
      `${task.estimatedMinutes || 0} 分钟额度`,
      timeAgo(task.createdAt),
    ];
    const progress = task.status === "running" || task.status === "queued"
      ? `<div class="row-progress" aria-label="处理进度 ${task.progressPercent || 0}%"><span style="width:${task.progressPercent || 0}%"></span></div>`
      : "";
    const quickAction = task.status === "done"
      ? `<button class="row-link" type="button" data-action="open-task" data-id="${task.id}">查看剧本</button>`
      : task.status === "failed"
        ? `<button class="row-link" type="button" data-action="retry" data-id="${task.id}">重试</button>`
        : "";
    return `<article class="task-row" data-open="${task.id}">
      <div class="file-mark" aria-hidden="true"><span></span></div>
      <div class="task-main">
        <div class="task-title">${escapeHtml(task.title)}</div>
        <div class="task-meta">${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
        ${progress}
      </div>
      <div class="task-state">${statusBadge(task)}${quickAction}</div>
      <div class="task-actions">
        <button class="more-btn" type="button" data-action="toggle-menu" data-id="${task.id}" aria-label="任务操作" aria-expanded="${state.menuTaskId === task.id}">•••</button>
        ${taskMenu(task)}
      </div>
    </article>`;
  }

  function renderList() {
    const filters = cfg.statusFilters.map((item) => `
      <button class="chip ${state.status === item.id ? "active" : ""}" type="button" data-filter="${item.id}">${item.label}</button>`).join("");
    const rows = state.tasks.length
      ? state.tasks.map(taskRow).join("")
      : `<div class="empty">
          <div class="empty-icon">＋</div>
          <h3>${state.keyword || state.status !== "all" ? "没有匹配的任务" : "还没有识别任务"}</h3>
          <p>${state.keyword || state.status !== "all" ? "换个关键词或筛选条件试试" : "上传第一个视频，生成可编辑、可下载的剧本"}</p>
          ${state.keyword || state.status !== "all" ? "" : `<button class="primary-btn" type="button" data-action="create">新建任务</button>`}
        </div>`;
    $("#main").innerHTML = `
      <section class="page">
        <div class="page-heading">
          <div>
            <div class="eyebrow">创作工作台</div>
            <h1>视频转剧本</h1>
            <p>${cfg.brand.slogan}</p>
          </div>
          <button class="primary-btn create-btn" type="button" data-action="create"><span>＋</span> 新建任务</button>
        </div>
        <div class="workspace-panel">
          <div class="toolbar">
            <label class="search-wrap">
              <span aria-hidden="true"></span>
              <input class="search" id="keyword" placeholder="搜索任务或文件名" value="${escapeHtml(state.keyword)}" />
            </label>
            <div class="filters" aria-label="任务状态筛选">${filters}</div>
            <div class="task-count">${state.tasks.length} 个任务</div>
          </div>
          <div class="task-list">${rows}</div>
        </div>
      </section>`;
  }

  function pipeline(task) {
    const currentIndex = cfg.pipeline.findIndex((item) => item.id === task.stage);
    return `<div class="progress-panel">
      <div class="progress-head">
        <div><strong>${escapeHtml(cfg.stageText[task.stage] || "正在处理")}</strong><span>后台处理中，可以离开本页</span></div>
        <b>${task.progressPercent || 0}%</b>
      </div>
      <div class="main-progress"><span style="width:${task.progressPercent || 0}%"></span></div>
      <div class="pipeline">${cfg.pipeline.map((step, index) => {
        const className = index < currentIndex ? "done" : index === currentIndex ? "active" : "";
        return `<div class="pipeline-step ${className}"><i>${index < currentIndex ? "✓" : index + 1}</i><span>${step.label}</span></div>`;
      }).join("")}</div>
    </div>`;
  }

  function scriptPreview(script) {
    if (!script) return "";
    function blockTime(block) {
      if (!Number.isFinite(Number(block.startSec))) return "";
      const start = Math.max(0, Math.round(Number(block.startSec)));
      const end = Number.isFinite(Number(block.endSec)) ? Math.max(start, Math.round(Number(block.endSec))) : null;
      const clock = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      return `<small class="block-time">${clock(start)}${end !== null ? `–${clock(end)}` : ""}</small>`;
    }
    function renderBlock(block) {
      const time = blockTime(block);
      if (block.type === "dialogue") {
        const warning = block.uncertain ? `<small class="uncertain">需核对</small>` : "";
        const performance = block.performance ? `<em>${escapeHtml(block.performance)}</em>` : "";
        return `<p class="dialogue">${time}<strong>${escapeHtml(block.speaker || "未知说话人")}</strong>${warning}${performance}<span>：${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "vo" || block.type === "os") {
        const isOs = block.type === "os" || block.voKind === "os" || block.isInnerMonologue;
        const speaker = block.speaker && !["旁白", "未知说话人", "OS", "内心独白"].includes(block.speaker)
          ? `${escapeHtml(block.speaker)} ${isOs ? "OS" : "VO"}`
          : (isOs ? "OS" : "VO");
        return `<p class="os-line">${time}<strong>${speaker}${block.inferred ? "（推断）" : ""}</strong><span>：${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "sound") {
        const label = block.category === "music" ? "背景音乐" : block.category === "ambience" ? "环境声" : "音效";
        const source = block.source === "inferred" ? "推断" : "听到";
        return `<p class="sound-line">${time}<strong>【${label} · ${source}】</strong><span>${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "emotion") {
        return `<p class="emotion-line">${time}<strong>【情绪】</strong><span>${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "screen_text") {
        return `<p class="sound-line">${time}<strong>【字幕】</strong><span>${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "transition") {
        return `<p class="sound-line">${time}<strong>【${escapeHtml(block.transitionType || "转场")}】</strong><span>${escapeHtml(block.text || "")}</span></p>`;
      }
      const actionMeta = [block.object && `对象：${block.object}`, block.result && `结果：${block.result}`].filter(Boolean).join("；");
      return `<p class="action-line">${time}<i>▲</i>${block.emotion ? `<em>${escapeHtml(block.emotion)}</em>` : ""}${escapeHtml(block.text || "")}${actionMeta ? `<small>${escapeHtml(actionMeta)}</small>` : ""}</p>`;
    }
    return `<div class="script-paper">
      <div class="script-title">
        <span>AI 结构化剧本</span>
        <h2>${escapeHtml(script.title)}</h2>
      </div>
      ${Array.isArray(script.characterProfiles) && script.characterProfiles.length ? `<section class="character-profiles">
        <h3>人物表</h3>
        ${script.characterProfiles.map((profile) => `<p><strong>${escapeHtml(profile.name || "人物")}</strong><span>${escapeHtml([profile.firstAppearance, profile.appearance, profile.clothing].filter(Boolean).filter((value, index, values) => values.indexOf(value) === index).join("；"))}</span></p>`).join("")}
      </section>` : ""}
      ${script.scenes.map((scene) => `<section class="scene">
        <h3>${escapeHtml(scene.heading)}</h3>
        ${scene.location ? `<p class="scene-location"><b>地点：</b>${escapeHtml(scene.location)}</p>` : ""}
        ${scene.characters && scene.characters.length ? `<p class="scene-cast"><b>人物：</b>${escapeHtml(scene.characters.join("、"))}</p>` : ""}
        ${scene.summary ? `<p class="scene-summary"><b>${scene.summaryGenerated ? "剧情衔接（系统补全）" : "剧情衔接"}：</b>${escapeHtml(scene.summary)}</p>` : ""}
        ${(scene.goal || scene.obstacle || scene.result) ? `<p class="scene-summary"><b>场次任务：</b>${escapeHtml([scene.goal && `目标：${scene.goal}`, scene.obstacle && `阻力：${scene.obstacle}`, scene.result && `结果：${scene.result}`].filter(Boolean).join("；"))}</p>` : ""}
        ${scene.environment ? `<p class="environment"><b>环境</b>${escapeHtml(scene.environment)}</p>` : ""}
        <div class="scene-blocks">${(scene.blocks || []).map(renderBlock).join("")}</div>
      </section>`).join("")}
    </div>`;
  }

  function renderResult(task) {
    const quality = task.quality || {};
    const severity = quality.severityCounts || {};
    const issues = Array.isArray(quality.issues) ? quality.issues.slice(0, 5) : [];
    const metric = (value) => value === null || value === undefined ? "--" : value;
    return `<div class="result-layout">
      <aside class="result-aside">
        <div class="aside-section">
          <span class="aside-label">原始视频</span>
          <strong class="aside-file">${escapeHtml(task.fileName)}</strong>
          <dl class="meta-list">
            <div><dt>视频时长</dt><dd>${formatDuration(task.durationSec)}</dd></div>
            <div><dt>消耗额度</dt><dd>${task.creditsUsed || task.estimatedMinutes} 分钟</dd></div>
            <div><dt>完成时间</dt><dd>${formatDate(task.completedAt || task.createdAt)}</dd></div>
          </dl>
        </div>
        <div class="aside-section quality-section">
          <span class="aside-label">识别概况</span>
          <div class="quality-row"><span>台词覆盖</span><strong>${metric(quality.dialogueCoverage)}%</strong></div>
          <div class="quality-row"><span>人物区分</span><strong>${metric(quality.speakerConfidence)}%</strong></div>
          <div class="quality-row"><span>待核对问题</span><strong>${metric(quality.warnings)}</strong></div>
          <p class="quality-severity"><span>P0 ${metric(severity.P0 || 0)}</span><span>P1 ${metric(severity.P1 || 0)}</span><span>P2 ${metric(severity.P2 || 0)}</span></p>
          <p class="quality-note">结果由 AI 生成，建议导出前快速核对人名与专有名词。</p>
          ${Array.isArray(quality.issueTags) && quality.issueTags.length ? `<p class="quality-note quality-warning">待核对：${escapeHtml(quality.issueTags.join("、"))}</p>` : ""}
          ${issues.length ? `<ul class="quality-issues">${issues.map((issue) => `<li><b>${escapeHtml(issue.severity || "提示")}</b>${escapeHtml(issue.description || issue.tag || "请核对该项")}</li>`).join("")}</ul>` : ""}
          ${quality.warning ? `<p class="quality-note quality-warning">${escapeHtml(quality.warning)}</p>` : ""}
        </div>
      </aside>
      <div class="result-main">${scriptPreview(task.result)}</div>
    </div>`;
  }

  function renderTaskTimeline(task) {
    const events = Array.isArray(task.events) ? task.events : [];
    if (!events.length) return "";
    return `<section class="task-timeline">
      <div class="timeline-head"><span class="aside-label">任务状态记录</span><span>${events.length} 条事件</span></div>
      <div class="timeline-list">${events.map((event) => {
        const label = event.stage ? (cfg.stageText[event.stage] || event.stage) : (cfg.statusText[event.status] || event.type);
        const duration = Number.isFinite(Number(event.durationMs)) ? ` · ${Math.round(Number(event.durationMs))} ms` : "";
        return `<div class="timeline-item"><i></i><div><strong>${escapeHtml(label)}</strong><span>${escapeHtml(event.message || "状态更新")}${duration}</span><small>${formatDate(event.createdAt)}</small></div></div>`;
      }).join("")}</div>
    </section>`;
  }

  function renderDetail() {
    const task = state.current;
    if (!task) {
      $("#main").innerHTML = `<section class="page"><div class="empty"><h3>任务不存在</h3></div></section>`;
      return;
    }
    const downloadActions = task.status === "done" ? `
      <div class="download-actions">
        <button class="secondary-btn" type="button" data-action="download" data-id="${task.id}" data-format="md">下载 MD</button>
        <button class="secondary-btn" type="button" data-action="download" data-id="${task.id}" data-format="txt">下载 TXT</button>
        <button class="secondary-btn disabled" type="button" disabled title="正式版开放">Word <small>稍后</small></button>
      </div>` : "";
    let content = "";
    if (task.status === "done") content = renderResult(task);
    if (task.status === "queued" || task.status === "running") content = pipeline(task);
    if (task.status === "failed") content = `<div class="error-panel">
      <div class="error-symbol">!</div>
      <div><h2>这次没有识别成功</h2><p>${escapeHtml(task.error || "识别服务暂时不可用，已自动退回本次额度。")}</p></div>
      <button class="primary-btn" type="button" data-action="retry" data-id="${task.id}">重新识别</button>
    </div>`;
    $("#main").innerHTML = `
      <section class="page detail-page">
        <button class="back-btn" type="button" data-action="back">← 返回任务列表</button>
        <div class="detail-heading">
          <div>
            <div class="detail-title-line"><h1>${escapeHtml(task.title)}</h1>${statusBadge(task)}</div>
            <p>${escapeHtml(task.fileName)} · ${formatDuration(task.durationSec)} · 创建于 ${formatDate(task.createdAt)}</p>
          </div>
          ${downloadActions}
        </div>
        ${content}
        ${renderTaskTimeline(task)}
      </section>`;
  }

  function uploadEstimate() {
    if (!state.upload.durationSec) return 0;
    return estimatedMinutes(state.upload.durationSec);
  }

  function createModalMarkup() {
    const upload = state.upload;
    const estimate = uploadEstimate();
    const credits = state.profile ? state.profile.credits : 0;
    const overDuration = upload.durationSec > cfg.upload.maxDurationMinutes * 60;
    const insufficient = estimate > credits;
    const canSubmit = upload.file && upload.durationSec && !upload.reading && !upload.error && !overDuration && !insufficient;
    const fileBlock = upload.file ? `<div class="selected-file">
      <div class="file-thumb"><span></span></div>
      <div><strong>${escapeHtml(upload.file.name)}</strong><p>${formatSize(upload.file.size)} · ${upload.reading ? "正在读取时长..." : formatDuration(upload.durationSec)}</p></div>
      <button type="button" data-action="remove-file" aria-label="移除视频">×</button>
    </div>` : `<div class="drop-content">
      <div class="upload-icon">↑</div>
      <strong>拖入视频，或点击选择文件</strong>
      <span>${cfg.upload.hint}</span>
    </div>`;
    let billing = "选择视频后自动读取时长并计算额度";
    if (upload.error) billing = upload.error;
    else if (overDuration) billing = `视频超过 ${cfg.upload.maxDurationMinutes} 分钟，请更换文件`;
    else if (estimate) billing = `预计消耗 ${estimate} 分钟额度，当前可用 ${credits} 分钟`;
    return `<div class="modal-mask" id="modalMask">
      <div class="modal create-modal" role="dialog" aria-modal="true" aria-labelledby="createTitle">
        <div class="modal-head">
          <div><span class="modal-kicker">创建识别任务</span><h2 id="createTitle">上传短视频</h2></div>
          <button class="close-btn" type="button" data-action="close-modal" aria-label="关闭">×</button>
        </div>
        <div class="field">
          <label for="taskTitle">任务名称 <span>可选</span></label>
          <input type="text" id="taskTitle" placeholder="默认使用视频文件名" />
        </div>
        <div class="field">
          <label>视频文件</label>
          <div class="drop ${upload.file ? "has-file" : ""}" id="dropzone">
            ${fileBlock}
            <input type="file" id="fileInput" accept="${cfg.upload.accept}" hidden />
          </div>
        </div>
        <div class="billing-note ${upload.error || overDuration || insufficient ? "warning" : ""}">
          <span>${upload.error || overDuration || insufficient ? "!" : "i"}</span>
          <div><strong>${billing}</strong><p>按视频实际时长向上取整预扣；任务失败自动退回。</p></div>
        </div>
        <div class="modal-actions">
          <button class="cancel-btn" type="button" data-action="close-modal">取消</button>
          <button class="primary-btn" type="button" data-action="submit-task" ${canSubmit ? "" : "disabled"}>开始识别</button>
        </div>
      </div>
    </div>`;
  }

  function openCreateModal() {
    state.upload = { file: null, durationSec: 0, reading: false, error: "" };
    $("#modalRoot").innerHTML = createModalMarkup();
  }

  function rerenderCreateModal(titleValue = "") {
    $("#modalRoot").innerHTML = createModalMarkup();
    const title = $("#taskTitle");
    if (title) title.value = titleValue;
  }

  function openDeleteModal(taskId) {
    const task = state.tasks.find((item) => item.id === taskId) || state.current;
    $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask">
      <div class="modal confirm-modal" role="alertdialog" aria-modal="true">
        <div class="confirm-icon">!</div>
        <h2>删除这个任务？</h2>
        <p>“${escapeHtml(task ? task.title : "该任务")}”及其剧本记录将从列表中移除，此操作无法撤销。</p>
        <div class="modal-actions">
          <button class="cancel-btn" type="button" data-action="close-modal">取消</button>
          <button class="danger-btn" type="button" data-action="confirm-delete" data-id="${taskId}">确认删除</button>
        </div>
      </div>
    </div>`;
  }

  function readVideoDuration(file) {
    return new Promise((resolve, reject) => {
      const video = document.createElement("video");
      const url = URL.createObjectURL(file);
      video.preload = "metadata";
      video.onloadedmetadata = () => {
        URL.revokeObjectURL(url);
        Number.isFinite(video.duration) && video.duration > 0 ? resolve(video.duration) : reject(new Error("无法读取视频时长"));
      };
      video.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("视频无法读取，请确认文件未损坏"));
      };
      video.src = url;
    });
  }

  async function setUploadFile(file) {
    if (!file) return;
    const titleValue = $("#taskTitle") ? $("#taskTitle").value : "";
    const maxBytes = cfg.upload.maxSizeMB * 1024 * 1024;
    if (!/\.mp4$/i.test(file.name)) {
      state.upload = { file: null, durationSec: 0, reading: false, error: "目前只支持 MP4 视频" };
      rerenderCreateModal(titleValue);
      return;
    }
    if (file.size > maxBytes) {
      state.upload = { file: null, durationSec: 0, reading: false, error: `文件超过 ${cfg.upload.maxSizeMB} MB` };
      rerenderCreateModal(titleValue);
      return;
    }
    state.upload = { file, durationSec: 0, reading: true, error: "" };
    rerenderCreateModal(titleValue);
    try {
      state.upload.durationSec = await readVideoDuration(file);
    } catch (error) {
      state.upload.error = error.message;
    }
    state.upload.reading = false;
    rerenderCreateModal(titleValue);
  }

  async function refreshList() {
    state.tasks = await api.listTasks({ keyword: state.keyword, status: state.status });
    renderList();
    schedulePoll(state.tasks.some((task) => task.status === "queued" || task.status === "running"));
  }

  async function refreshDetail() {
    try {
      state.current = await api.getTask(state.taskId);
      renderDetail();
      const active = state.current.status === "queued" || state.current.status === "running";
      schedulePoll(active);
    } catch (error) {
      toast(error.message || "任务加载失败", "error");
      go("tasks");
    }
  }

  function schedulePoll(enabled) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (!enabled) return;
    state.pollTimer = setTimeout(() => {
      if (state.view === "detail") refreshDetail();
      else refreshList();
    }, 2500);
  }

  async function render() {
    parseHash();
    if (!state.profile) {
      renderAuth();
      return;
    }
    state.menuTaskId = "";
    renderHeader();
    if (state.view === "detail") await refreshDetail();
    else await refreshList();
  }

  async function downloadTask(taskId, format) {
    try {
      const payload = await api.getExport(taskId, format);
      const blob = payload.blob || new Blob([payload.content], { type: payload.mimeType });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = payload.fileName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      toast(`${format.toUpperCase()} 已生成`);
    } catch (error) {
      toast(error.message || "下载失败", "error");
    }
  }

  function startAuthCooldown(seconds) {
    clearInterval(state.authCooldownTimer);
    state.authCooldown = Math.max(0, Math.ceil(Number(seconds) || 0));
    if (!state.authCooldown) return;
    renderAuth();
    state.authCooldownTimer = setInterval(() => {
      state.authCooldown -= 1;
      if (state.authCooldown <= 0) {
        state.authCooldown = 0;
        clearInterval(state.authCooldownTimer);
        state.authCooldownTimer = null;
      }
      if (!state.profile && state.authMode === "register") renderAuth();
    }, 1000);
  }

  async function requestCode() {
    const email = $("#authEmail") ? $("#authEmail").value.trim() : "";
    if (!email || !email.includes("@")) { toast("请先输入正确的邮箱地址", "error"); return; }
    state.authEmail = email;
    try {
      const result = await api.requestCode(email, "register");
      toast(result.devCode ? `${result.message}：${result.devCode}` : result.message);
      startAuthCooldown(result.resendAfter || 60);
    } catch (error) {
      if (error.retryAfter) startAuthCooldown(error.retryAfter);
      toast(error.message || "验证码发送失败", "error");
    }
  }

  async function submitAuth() {
    const email = $("#authEmail") ? $("#authEmail").value.trim() : "";
    const password = $("#authPassword") ? $("#authPassword").value : "";
    state.authEmail = email;
    if (!email || !password) { toast("请填写邮箱和密码", "error"); return; }
    if (state.authMode === "register") {
      const name = $("#authName") ? $("#authName").value.trim() : "";
      const code = $("#authCode") ? $("#authCode").value.trim() : "";
      const password2 = $("#authPassword2") ? $("#authPassword2").value : "";
      if (!/^\d{6}$/.test(code)) { toast("请输入 6 位邮箱验证码", "error"); return; }
      if (password.length < 8 || !/[A-Za-z]/.test(password) || !/\d/.test(password)) {
        toast("密码至少 8 位，且必须同时包含字母和数字", "error");
        return;
      }
      if (password !== password2) { toast("两次输入的密码不一致", "error"); return; }
      state.authBusy = true; renderAuth();
      try { state.profile = await api.register({ email, password, name, code }); toast("注册成功，欢迎来到剧编编"); await render(); }
      catch (error) { state.authBusy = false; renderAuth(); toast(error.message || "注册失败", "error"); }
      return;
    }
    state.authBusy = true; renderAuth();
    try { state.profile = await api.login(email, password); toast("登录成功"); await render(); }
    catch (error) { state.authBusy = false; renderAuth(); toast(error.message || "登录失败", "error"); }
  }

  async function logout() {
    try { await api.logout(); } catch (_) {}
    clearInterval(state.authCooldownTimer);
    state.authCooldownTimer = null;
    state.authCooldown = 0;
    state.profile = null; state.authBusy = false; state.authMode = "login"; state.authEmail = "";
    $("#modalRoot").innerHTML = ""; renderAuth(); toast("已退出登录");
  }

  document.addEventListener("click", async (event) => {
    const actionElement = event.target.closest("[data-action]");
    const row = event.target.closest("[data-open]");
    const filter = event.target.closest("[data-filter]");

    if (filter) {
      state.status = filter.dataset.filter;
      await refreshList();
      return;
    }
    if (row && !actionElement) {
      go("task/" + row.dataset.open);
      return;
    }
    if (!actionElement) {
      if (!event.target.closest(".task-menu")) {
        state.menuTaskId = "";
        document.querySelectorAll(".task-menu").forEach((menu) => menu.remove());
      }
      return;
    }

    const action = actionElement.dataset.action;
    const id = actionElement.dataset.id;
    if (action === "toggle-password") {
      const input = document.getElementById(actionElement.dataset.target);
      if (!input) return;
      const visible = input.type === "password";
      input.type = visible ? "text" : "password";
      actionElement.setAttribute("aria-pressed", String(visible));
      actionElement.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
      return;
    }
    if (action === "auth-switch") {
      clearInterval(state.authCooldownTimer);
      state.authCooldownTimer = null;
      state.authCooldown = 0;
      state.authMode = state.authMode === "login" ? "register" : "login";
      state.authBusy = false;
      renderAuth();
      return;
    }
    if (action === "request-code") { await requestCode(); return; }
    if (action === "auth-submit") { event.preventDefault(); await submitAuth(); return; }
    if (action === "logout") { await logout(); return; }
    if (action === "create") openCreateModal();
    if (action === "close-modal") $("#modalRoot").innerHTML = "";
    if (action === "back") go("tasks");
    if (action === "account") openAccountModal();
    if (action === "recharge") toast("测试阶段由管理员手动增加额度");
    if (action === "open-task") go("task/" + id);
    if (action === "toggle-menu") {
      event.stopPropagation();
      state.menuTaskId = state.menuTaskId === id ? "" : id;
      renderList();
    }
    if (action === "ask-delete") openDeleteModal(id);
    if (action === "confirm-delete") {
      await api.deleteTask(id);
      $("#modalRoot").innerHTML = "";
      toast("任务已删除");
      if (state.view === "detail") go("tasks");
      else await refreshList();
    }
    if (action === "retry") {
      await api.retryTask(id);
      toast("任务已重新进入队列");
      if (state.view === "detail") await refreshDetail();
      else await refreshList();
    }
    if (action === "download") await downloadTask(id, actionElement.dataset.format);
    if (action === "remove-file") {
      const titleValue = $("#taskTitle") ? $("#taskTitle").value : "";
      state.upload = { file: null, durationSec: 0, reading: false, error: "" };
      rerenderCreateModal(titleValue);
    }
    if (action === "submit-task") {
      const file = state.upload.file;
      const title = $("#taskTitle") ? $("#taskTitle").value.trim() : "";
      if (!file || !state.upload.durationSec) return;
      actionElement.disabled = true;
      actionElement.textContent = "正在创建...";
      try {
        const task = await api.createTask({
          title,
          file,
          durationSec: Math.round(state.upload.durationSec),
          estimatedMinutes: uploadEstimate(),
        });
        state.profile = await api.getProfile();
        renderHeader();
        $("#modalRoot").innerHTML = "";
        toast("任务已创建，正在后台识别");
        go("task/" + task.id);
      } catch (error) {
        actionElement.disabled = false;
        actionElement.textContent = "开始识别";
        toast(error.message || "创建失败", "error");
      }
    }
  });

  document.addEventListener("submit", async (event) => {
    if (event.target && event.target.id === "authForm") {
      event.preventDefault();
      await submitAuth();
    }
  });

  let searchTimer;
  document.addEventListener("input", (event) => {
    if (event.target.id !== "keyword") return;
    state.keyword = event.target.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(refreshList, 180);
  });

  document.addEventListener("change", (event) => {
    if (event.target.id === "fileInput") setUploadFile(event.target.files[0]);
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest("#dropzone") && !event.target.closest("[data-action]")) {
      const input = $("#fileInput");
      if (input && event.target.id !== "fileInput") input.click();
    }
    if (event.target.id === "modalMask") $("#modalRoot").innerHTML = "";
  });

  document.addEventListener("dragover", (event) => {
    if (event.target.closest("#dropzone")) event.preventDefault();
  });

  document.addEventListener("drop", (event) => {
    if (!event.target.closest("#dropzone")) return;
    event.preventDefault();
    setUploadFile(event.dataTransfer.files[0]);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("#modalRoot").innerHTML) $("#modalRoot").innerHTML = "";
  });

  window.addEventListener("hashchange", render);

  async function boot() {
    try {
      state.profile = await api.getProfile();
    } catch (_) {
      state.profile = null;
    }
    await render();
  }

  boot();
})();
