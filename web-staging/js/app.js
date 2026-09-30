/**
 * Hash-routed workspace UI. The page renders from the API contract only; mock
 * mode and the future FastAPI implementation share the same view code.
 */
(function () {
  const cfg = window.APP_CONFIG;
  const POINTS_PER_MINUTE = 5;
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
    upload: { files: [], file: null, durationSec: 0, reading: false, error: "" },
    pollTimer: null,
    adminTab: "overview",
    adminTaskPage: 0,
    adminTaskKeyword: "",
    adminTaskStatus: "all",
    adminTaskUser: "",
    adminTaskFrom: "",
    adminTaskTo: "",
    adminUserPage: 0,
    adminUserKeyword: "",
    adminUserStatus: "all",
    adminRechargePage: 0,
    adminRechargeKeyword: "",
    adminRechargeFrom: "",
    adminRechargeTo: "",
    taskPage: 0,
    pointsPage: 0,
    pointsKeyword: "",
    pointsType: "all",
    pointsFrom: "",
    pointsTo: "",
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
    state.view = parts[0] === "task" && parts[1] ? "detail" : (parts[0] === "admin" ? "admin" : "list");
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
    const credits = state.profile ? Number(state.profile.credits || 0).toFixed(1) : "--";
    const initial = state.profile && state.profile.name ? state.profile.name.charAt(0) : "用";
    $("#header").innerHTML = `
      <div class="header-inner">
        <a class="brand" href="#/tasks" aria-label="返回任务列表">
          <img class="brand-logo" src="${cfg.brand.logo}" alt="" />
          <span>${cfg.brand.name}</span>
        </a>
        <span class="edition">核心识别测试版</span>
        <div class="header-right">
          <button class="credit-pill" type="button" data-action="points" title="查看积分余额和流水">
            <span class="credit-label">剩余积分</span>
            <strong>${credits}</strong><span>积分</span>
          </button>
          ${state.profile && state.profile.role === "admin" ? `<a class="text-btn admin-link" href="#/admin"><span class="nav-icon">⌘</span>管理后台</a>` : ""}
          <button class="text-btn recharge-btn" type="button" data-action="recharge"><span class="nav-icon">＋</span>充值</button>
          <button class="avatar" type="button" data-action="account" title="个人信息">${escapeHtml(initial)}</button>
        </div>
      </div>`;
  }

  function captureAuthFormState() {
    const form = $("#authForm");
    if (!form) return null;
    const values = {};
    ["authName", "authEmail", "authCode", "authPassword", "authPassword2"].forEach((id) => {
      const input = document.getElementById(id);
      if (input) values[id] = input.value;
    });
    const active = document.activeElement;
    return {
      values,
      focusedId: active && active.id ? active.id : "",
      selectionStart: active && typeof active.selectionStart === "number" ? active.selectionStart : null,
      selectionEnd: active && typeof active.selectionEnd === "number" ? active.selectionEnd : null,
    };
  }

  async function renderAdmin() {
    if (!state.profile || state.profile.role !== "admin") { go("tasks"); return; }
    try {
      const tabs = [{id:"overview",label:"概览"},{id:"users",label:"用户"},{id:"credits",label:"充值"},{id:"tasks",label:"任务"},{id:"usage",label:"用量"},{id:"analytics",label:"分析（后续）"},{id:"funnel",label:"转化漏斗（后续）"},{id:"retention",label:"留存（后续）"},{id:"codes",label:"码管理（后续）"}];
      const overview = await api.getAdminOverview();
      const tab = state.adminTab;
      const nav = tabs.map(item => `<button class="admin-tab ${tab === item.id ? "active" : ""}" data-action="admin-tab" data-tab="${item.id}" ${["analytics","funnel","retention","codes"].includes(item.id) ? "disabled" : ""}>${item.label}</button>`).join("");
      let body = `<div class="admin-stats"><article><span>用户总数</span><strong>${overview.users.total}</strong><small>活跃 ${overview.users.active}</small></article><article><span>剩余积分</span><strong>${Number(overview.users.credits || 0).toFixed(1)}</strong><small>积分</small></article><article><span>任务总数</span><strong>${overview.tasks.total}</strong><small>完成 ${overview.tasks.done} · 失败 ${overview.tasks.failed}</small></article><article><span>API 使用成本</span><strong>¥ ${Number(overview.apiCostRmb || 0).toFixed(2)}</strong><small>人民币</small></article></div>`;
      if (tab === "users" || tab === "credits") {
        const userPageSize = 20;
        const users = await api.getAdminUsers({ limit: userPageSize, offset: state.adminUserPage * userPageSize, keyword: state.adminUserKeyword, status: state.adminUserStatus });
        const userPageCount = Math.max(1, Math.ceil(users.total / userPageSize));
        body += `<div class="admin-panel"><div class="admin-panel-head"><div><h2>用户管理</h2><p>启用、停用账号并调整分钟额度</p></div><span>${users.total} 个账号</span></div><div class="admin-toolbar"><input id="adminUserKeyword" value="${escapeHtml(state.adminUserKeyword)}" placeholder="搜索邮箱或姓名" /><select id="adminUserStatus"><option value="all" ${state.adminUserStatus === "all" ? "selected" : ""}>全部用户</option><option value="active" ${state.adminUserStatus === "active" ? "selected" : ""}>正常</option><option value="disabled" ${state.adminUserStatus === "disabled" ? "selected" : ""}>已停用</option></select><button class="admin-action admin-search-btn" data-action="admin-user-search">筛选</button></div><div class="admin-table-wrap"><table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>剩余额度</th><th>任务/消耗</th><th>操作</th></tr></thead><tbody>${users.items.map(user => `<tr><td><strong>${escapeHtml(user.name || "未命名")}</strong><small>${escapeHtml(user.email)}</small></td><td>${user.role === "admin" ? "管理员" : "用户"}</td><td><span class="admin-status ${user.isActive ? "on" : "off"}">${user.isActive ? "正常" : "已停用"}</span></td><td><strong>${user.credits}</strong> 分钟</td><td>${user.taskCount} / ${user.totalUsed} 分钟</td><td><button class="admin-action" data-action="admin-credit" data-id="${user.id}">调整额度</button><button class="admin-action" data-action="admin-status" data-id="${user.id}" data-active="${user.isActive ? "0" : "1"}">${user.isActive ? "停用" : "启用"}</button></td></tr>`).join("") || `<tr><td colspan="6">暂无用户</td></tr>`}</tbody></table></div><div class="admin-pagination"><button class="admin-action" data-action="admin-user-page" data-page="${Math.max(0, state.adminUserPage - 1)}" ${state.adminUserPage === 0 ? "disabled" : ""}>上一页</button><span>第 ${state.adminUserPage + 1} / ${userPageCount} 页</span><button class="admin-action" data-action="admin-user-page" data-page="${Math.min(userPageCount - 1, state.adminUserPage + 1)}" ${state.adminUserPage >= userPageCount - 1 ? "disabled" : ""}>下一页</button></div></div>`;
        if (tab === "credits") {
          const rechargePageSize = 10;
          const rechargePage = await api.getAdminRecharges({ limit: rechargePageSize, offset: state.adminRechargePage * rechargePageSize, keyword: state.adminRechargeKeyword, date_from: state.adminRechargeFrom, date_to: state.adminRechargeTo });
          const rechargePageCount = Math.max(1, Math.ceil(rechargePage.total / rechargePageSize));
          state.adminRechargePage = Math.min(state.adminRechargePage, rechargePageCount - 1);
          body += `<div class="admin-panel recharge-ledger-panel"><div class="admin-panel-head"><div><h2>人民币充值流水</h2><p>每笔充值按 1 元 = 13.8 积分记录</p></div><span>${rechargePage.total} 笔充值</span></div><div class="admin-toolbar recharge-toolbar"><input id="adminRechargeKeyword" value="${escapeHtml(state.adminRechargeKeyword)}" placeholder="搜索用户账号或备注" /><input id="adminRechargeFrom" type="date" value="${state.adminRechargeFrom}" /><input id="adminRechargeTo" type="date" value="${state.adminRechargeTo}" /><button class="admin-action admin-search-btn" data-action="admin-recharge-search">筛选</button><button class="admin-action" data-action="admin-recharge-reset">重置</button></div><div class="admin-table-wrap"><table><thead><tr><th>用户账号</th><th>人民币</th><th>增加积分</th><th>充值后余额</th><th>备注</th><th>时间</th></tr></thead><tbody>${rechargePage.items.map(item => `<tr><td><strong>${escapeHtml(item.name || item.email || item.user_id)}</strong><small>${escapeHtml(item.email || "")}</small></td><td>¥ ${Number(item.rmb_amount).toFixed(2)}</td><td class="points-positive">+${Number(item.points_amount).toFixed(1)} 积分</td><td>${Number(item.balance_after).toFixed(1)} 积分</td><td>${escapeHtml(item.reason || "-")}</td><td>${formatDate(item.created_at)}</td></tr>`).join("") || `<tr><td colspan="6">暂无匹配的充值流水</td></tr>`}</tbody></table></div><div class="admin-pagination"><button class="admin-action" data-action="admin-recharge-page" data-page="${Math.max(0, state.adminRechargePage - 1)}" ${state.adminRechargePage === 0 ? "disabled" : ""}>上一页</button><span>第 ${state.adminRechargePage + 1} / ${rechargePageCount} 页</span><button class="admin-action" data-action="admin-recharge-page" data-page="${Math.min(rechargePageCount - 1, state.adminRechargePage + 1)}" ${state.adminRechargePage >= rechargePageCount - 1 ? "disabled" : ""}>下一页</button></div></div>`;
        }
      } else if (tab === "tasks" || tab === "usage") {
        const pageSize = 20;
        const users = await api.getAdminUsers({ limit: 200 });
        const tasks = await api.getAdminTasks({ limit: pageSize, offset: state.adminTaskPage * pageSize, keyword: state.adminTaskKeyword, status: state.adminTaskStatus, userId: state.adminTaskUser, date_from: state.adminTaskFrom, date_to: state.adminTaskTo });
        const pageCount = Math.max(1, Math.ceil(tasks.total / pageSize));
        body += `<div class="admin-panel"><div class="admin-panel-head"><div><h2>${tab === "usage" ? "用量记录" : "任务管理"}</h2><p>按用户、状态和时间筛选全平台任务</p></div><span>${tasks.total} 条任务</span></div><div class="admin-toolbar"><input id="adminTaskKeyword" value="${escapeHtml(state.adminTaskKeyword)}" placeholder="搜索任务或账号" /><select id="adminTaskUser"><option value="">全部用户</option>${users.items.map(user => `<option value="${user.id}" ${state.adminTaskUser === user.id ? "selected" : ""}>${escapeHtml(user.email)}</option>`).join("")}</select><select id="adminTaskStatus"><option value="all" ${state.adminTaskStatus === "all" ? "selected" : ""}>全部状态</option><option value="done" ${state.adminTaskStatus === "done" ? "selected" : ""}>成功</option><option value="failed" ${state.adminTaskStatus === "failed" ? "selected" : ""}>失败</option><option value="running" ${state.adminTaskStatus === "running" ? "selected" : ""}>进行中</option><option value="queued" ${state.adminTaskStatus === "queued" ? "selected" : ""}>排队中</option></select><input id="adminTaskFrom" type="date" value="${state.adminTaskFrom}" title="开始日期" /><input id="adminTaskTo" type="date" value="${state.adminTaskTo}" title="结束日期" /><button class="admin-action admin-search-btn" data-action="admin-search">筛选</button></div><div class="admin-table-wrap"><table><thead><tr><th>任务</th><th>用户账号</th><th>状态</th><th>消耗</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${tasks.items.map(item => `<tr><td><strong>${escapeHtml(item.title || item.id)}</strong><small>${escapeHtml(item.file_name || "")}</small></td><td>${escapeHtml(item.email || item.user_id || "-")}</td><td><span class="admin-status ${item.status === "done" ? "on" : item.status === "failed" ? "off" : "wait"}">${item.status === "done" ? "成功" : item.status === "failed" ? "失败" : item.status === "running" ? "进行中" : "排队中"}</span></td><td>${item.credits_used || 0} 分钟</td><td>${formatDate(item.created_at)}</td><td>${item.status === "failed" ? `<button class="admin-action" data-action="admin-retry" data-id="${item.id}">重试</button>` : "-"}</td></tr>`).join("") || `<tr><td colspan="6">暂无任务</td></tr>`}</tbody></table></div><div class="admin-pagination"><button class="admin-action" data-action="admin-page" data-page="${Math.max(0, state.adminTaskPage - 1)}" ${state.adminTaskPage === 0 ? "disabled" : ""}>上一页</button><span>第 ${state.adminTaskPage + 1} / ${pageCount} 页</span><button class="admin-action" data-action="admin-page" data-page="${Math.min(pageCount - 1, state.adminTaskPage + 1)}" ${state.adminTaskPage >= pageCount - 1 ? "disabled" : ""}>下一页</button></div></div>`;
      } else {
        body += `<div class="admin-panel"><div class="admin-panel-head"><div><h2>最近任务</h2><p>平台实时处理概况</p></div><span>实时数据</span></div><div class="admin-table-wrap"><table><thead><tr><th>任务</th><th>用户</th><th>状态</th><th>消耗</th><th>创建时间</th></tr></thead><tbody>${(overview.recentTasks || []).map(item => `<tr><td>${escapeHtml(item.title || item.id)}</td><td>${escapeHtml(item.email || item.user_id || item.name || "-")}</td><td>${escapeHtml(item.status)}</td><td>${item.credits_used || 0} 分钟</td><td>${formatDate(item.created_at)}</td></tr>`).join("") || `<tr><td colspan="5">暂无任务</td></tr>`}</tbody></table></div></div>`;
      }
      $("#main").innerHTML = `<section class="admin-page"><div class="admin-heading"><div><span class="eyebrow">ADMIN CONSOLE</span><h1>管理员后台</h1><p>平台运行、用户额度和任务处理</p></div><a class="secondary-btn admin-back-btn" href="#/tasks"><span aria-hidden="true">←</span>返回工作台</a></div><div class="admin-nav">${nav}</div>${body}<p class="admin-note">分析、转化漏斗、留存、Eval、码管理暂保留入口，后续版本开放。</p></section>`;
    } catch (error) { toast(error.message || "后台数据加载失败", "error"); }
  }

  function restoreAuthFormState(snapshot) {
    if (!snapshot) return;
    Object.entries(snapshot.values).forEach(([id, value]) => {
      const input = document.getElementById(id);
      if (input) input.value = value;
    });
    if (!snapshot.focusedId) return;
    const focused = document.getElementById(snapshot.focusedId);
    if (!focused) return;
    focused.focus();
    if (snapshot.selectionStart !== null && typeof focused.setSelectionRange === "function") {
      focused.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd ?? snapshot.selectionStart);
    }
  }

  function renderAuth() {
    const previous = captureAuthFormState();
    const register = state.authMode === "register";
    const forgot = state.authMode === "forgot";
    const reset = register || forgot;
    const cooldown = state.authCooldown > 0 ? `${state.authCooldown}s 后重发` : "获取验证码";
    $("#header").innerHTML = `
      <div class="auth-brand"><a class="brand" href="#"><img class="brand-logo" src="${cfg.brand.logo}" alt="" /><span>${cfg.brand.name}</span></a><span>AI 视频剧本工作台</span></div>`;
    $("#main").innerHTML = `
      <section class="auth-page">
        <div class="auth-card">
          <div class="auth-kicker">欢迎使用剧编编</div>
          <h1>${register ? "创建账号" : forgot ? "重置密码" : "登录"}</h1>
          <p class="auth-subtitle">${register ? "注册后即可开始整理你的短视频剧本" : forgot ? "通过邮箱验证码设置新的登录密码" : "登录后继续你的剧本创作"}</p>
          <form id="authForm" novalidate>
            ${register ? `<div class="auth-field"><label for="authName">昵称</label><input id="authName" autocomplete="name" placeholder="怎么称呼你？" maxlength="40" /></div>` : ""}
            <div class="auth-field"><label for="authEmail">邮箱</label><input id="authEmail" type="email" autocomplete="email" placeholder="name@example.com" value="${escapeHtml(state.authEmail)}" required /></div>
            ${reset ? `<div class="auth-field"><label for="authCode">邮箱验证码</label><div class="code-row"><input id="authCode" inputmode="numeric" maxlength="6" placeholder="6 位验证码" required /><button class="code-btn" type="button" data-action="request-code" ${state.authCooldown ? "disabled" : ""}>${cooldown}</button></div><small class="auth-hint">验证码有效期 10 分钟</small></div>` : ""}
            <div class="auth-field"><label for="authPassword">${forgot ? "新密码" : "密码"}</label><div class="password-row"><input id="authPassword" type="password" autocomplete="${reset ? "new-password" : "current-password"}" placeholder="至少 8 位，含字母和数字" required /><button class="password-toggle" type="button" data-action="toggle-password" data-target="authPassword" aria-label="显示密码" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"></path><circle cx="12" cy="12" r="2.5"></circle></svg></button></div>${reset ? `<small class="auth-hint">至少 8 位，且必须同时包含字母和数字</small>` : ""}</div>
            ${reset ? `<div class="auth-field"><label for="authPassword2">确认密码</label><div class="password-row"><input id="authPassword2" type="password" autocomplete="new-password" placeholder="再次输入密码" required /><button class="password-toggle" type="button" data-action="toggle-password" data-target="authPassword2" aria-label="显示确认密码" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"></path><circle cx="12" cy="12" r="2.5"></circle></svg></button></div></div>` : ""}
            <button class="auth-submit" type="submit" data-action="auth-submit" ${state.authBusy ? "disabled" : ""}>${state.authBusy ? "处理中…" : register ? "注册并进入工作台" : forgot ? "设置新密码" : "登录"}</button>
          </form>
          <div class="auth-switch">${register ? "已有账号？" : forgot ? "想起密码了？" : "没有账号？"}<button type="button" data-action="auth-switch">${register || forgot ? "立即登录" : "注册"}</button></div>
          ${!register && !forgot ? `<div class="auth-forgot"><button type="button" data-action="auth-forgot">忘记密码？</button></div>` : ""}
        </div>
      </section>`;
    restoreAuthFormState(previous);
    if (!previous) {
      const email = $("#authEmail");
      if (email) email.focus();
    }
  }

  function openAccountModal() {
    const profile = state.profile || {};
    $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask">
      <div class="modal account-modal" role="dialog" aria-modal="true" aria-labelledby="accountTitle">
        <div class="modal-head"><div><span class="modal-kicker">账号中心</span><h2 id="accountTitle">个人信息</h2></div><button class="close-btn" type="button" data-action="close-modal" aria-label="关闭">×</button></div>
        <div class="account-profile"><div class="account-avatar">${escapeHtml((profile.name || "用").charAt(0))}</div><div><strong>${escapeHtml(profile.name || "剧编编用户")}</strong><span>${escapeHtml(profile.email || "")}</span></div></div>
        <dl class="account-details"><div><dt>注册邮箱</dt><dd>${escapeHtml(profile.email || "")}</dd></div><div><dt>当前方案</dt><dd>${escapeHtml(profile.plan || "体验版")}</dd></div><div><dt>剩余积分</dt><dd>${Number(profile.credits || 0).toFixed(1)} 积分</dd></div><div><dt>注册时间</dt><dd>${profile.createdAt ? escapeHtml(formatDate(profile.createdAt)) : "-"}</dd></div></dl>
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
      `${(Number(task.estimatedMinutes || 0) * POINTS_PER_MINUTE).toFixed(1)} 积分`,
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
    const pageSize = 10;
    const pageCount = Math.max(1, Math.ceil(state.tasks.length / pageSize));
    const pageTasks = state.tasks.slice(state.taskPage * pageSize, (state.taskPage + 1) * pageSize);
    const rows = state.tasks.length
      ? pageTasks.map(taskRow).join("")
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
          <div class="batch-download-actions"><button class="secondary-btn" type="button" data-action="download-all" data-format="md" ${state.tasks.some((task) => task.status === "done") ? "" : "disabled"}>下载全部剧本（MD）</button><button class="secondary-btn" type="button" data-action="download-all" data-format="txt" ${state.tasks.some((task) => task.status === "done") ? "" : "disabled"}>下载全部剧本（TXT）</button><small>最多上传 999 个 MP4 视频，单个不超过 6 分钟</small></div>
          <div class="toolbar">
            <label class="search-wrap">
              <span aria-hidden="true"></span>
              <input class="search" id="keyword" placeholder="搜索任务或文件名" value="${escapeHtml(state.keyword)}" />
            </label>
            <div class="filters" aria-label="任务状态筛选">${filters}</div>
            <div class="task-count">${state.tasks.length} 个任务</div>
          </div>
          <div class="task-list">${rows}</div>
          ${state.tasks.length > pageSize ? `<div class="user-pagination"><button class="secondary-btn" type="button" data-action="task-page" data-page="${Math.max(0, state.taskPage - 1)}" ${state.taskPage === 0 ? "disabled" : ""}>上一页</button><span>第 ${state.taskPage + 1} / ${pageCount} 页</span><button class="secondary-btn" type="button" data-action="task-page" data-page="${Math.min(pageCount - 1, state.taskPage + 1)}" ${state.taskPage >= pageCount - 1 ? "disabled" : ""}>下一页</button></div>` : ""}
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
      ${script.scenes.map((scene) => `<section class="scene">
        <h3>${escapeHtml(scene.heading)}</h3>
        ${(scene.characters || []).length ? `<p class="scene-cast">出场人物：${escapeHtml([...new Set(scene.characters || [])].join("、"))}</p>` : ""}
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
            <div><dt>消耗积分</dt><dd>${(Number(task.creditsUsed || task.estimatedMinutes || 0) * POINTS_PER_MINUTE).toFixed(1)} 积分</dd></div>
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
      </div>` : task.status === "review" ? `<div class="quality-note quality-warning">该结果存在 P0/P1 级质量问题，完成复核前不可导出。</div>` : "";
    let content = "";
    if (task.status === "done" || task.status === "review") content = renderResult(task);
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
    const estimatedPoints = estimate * POINTS_PER_MINUTE;
    const insufficient = estimatedPoints > credits;
    const canSubmit = upload.files.length && upload.durationSec && !upload.reading && !upload.error && !overDuration && !insufficient;
    const fileBlock = upload.files.length ? `<div class="selected-file">
      <div class="file-thumb"><span></span></div>
      <div><strong>${escapeHtml(upload.file.name)}</strong><p>${formatSize(upload.file.size)} · ${upload.reading ? "正在读取时长..." : formatDuration(upload.durationSec)}</p></div>
      <button type="button" data-action="remove-file" aria-label="移除视频">×</button>
    </div>` : `<div class="drop-content">
      <div class="upload-icon">↑</div>
      <strong>拖入视频，或点击选择文件</strong>
      <span>${cfg.upload.hint}</span>
    </div>`;
    let billing = "选择视频后自动读取时长并预估积分";
    if (upload.error) billing = upload.error;
    else if (overDuration) billing = `视频超过 ${cfg.upload.maxDurationMinutes} 分钟，请更换文件`;
    else if (estimate) billing = `预计消耗 ${estimatedPoints.toFixed(1)} 积分，当前可用 ${Number(credits).toFixed(1)} 积分`;
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
            <input type="file" id="fileInput" accept=".mp4,video/mp4" multiple hidden />
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
    state.upload = { files: [], file: null, durationSec: 0, reading: false, error: "" };
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

  async function setUploadFiles(fileList) {
    const files = Array.from(fileList || []);
    const titleValue = $("#taskTitle") ? $("#taskTitle").value : "";
    const maxBytes = cfg.upload.maxSizeMB * 1024 * 1024;
    if (!files.length) return;
    if (files.length > cfg.upload.maxFiles) { state.upload = { files: [], file: null, durationSec: 0, reading: false, error: `最多选择 ${cfg.upload.maxFiles} 个视频` }; rerenderCreateModal(titleValue); return; }
    const invalid = files.find((file) => !/\.mp4$/i.test(file.name) || file.size > maxBytes);
    if (invalid) { state.upload = { files: [], file: null, durationSec: 0, reading: false, error: !/\.mp4$/i.test(invalid.name) ? "目前只支持 MP4 视频" : `${invalid.name} 超过 ${cfg.upload.maxSizeMB} MB` }; rerenderCreateModal(titleValue); return; }
    state.upload = { files, file: files[0], durationSec: 0, reading: true, error: "" };
    rerenderCreateModal(titleValue);
    try {
      const durations = await Promise.all(files.map(readVideoDuration));
      state.upload.durationSec = durations.reduce((sum, value) => sum + value, 0);
      if (durations.some((value) => value > cfg.upload.maxDurationMinutes * 60)) state.upload.error = `单个视频不能超过 ${cfg.upload.maxDurationMinutes} 分钟`;
    } catch (error) { state.upload.error = error.message; }
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
    if (state.view === "admin") await renderAdmin();
    else if (state.view === "detail") await refreshDetail();
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

  function openRechargeModal() {
    $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask"><div class="modal recharge-modal" role="dialog" aria-modal="true"><div class="modal-head"><div><span class="modal-kicker">积分充值</span><h2>联系管理员充值</h2></div><button class="close-btn" type="button" data-action="close-modal" aria-label="关闭">×</button></div><div class="recharge-content"><img src="./assets/recharge-wechat.jpg" alt="客服微信二维码" /><p>请扫码添加客服人员微信联系充值额度</p></div><div class="modal-actions"><button class="cancel-btn" type="button" data-action="close-modal">关闭</button></div></div></div>`;
  }

  async function openPointsModal() {
    $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask"><div class="modal points-modal" role="dialog" aria-modal="true"><div class="modal-head"><div><span class="modal-kicker">积分中心</span><h2>积分余额与流水</h2></div><button class="close-btn" type="button" data-action="close-modal" aria-label="关闭">×</button></div><div class="points-loading">正在加载积分流水…</div></div></div>`;
    try {
      const ledger = await api.getCreditLedger();
      const ledgerPageSize = 10;
      const allItems = ledger.items || [];
      const from = state.pointsFrom ? new Date(`${state.pointsFrom}T00:00:00`) : null;
      const to = state.pointsTo ? new Date(`${state.pointsTo}T23:59:59`) : null;
      const filteredItems = allItems.filter((item) => {
        const amount = Number(item.amount || 0);
        const date = new Date(item.created_at);
        const typeMatch = state.pointsType === "all" || (state.pointsType === "income" && amount > 0) || (state.pointsType === "expense" && amount < 0);
        const keywordMatch = !state.pointsKeyword || String(item.reason || "").toLowerCase().includes(state.pointsKeyword.toLowerCase());
        return typeMatch && keywordMatch && (!from || date >= from) && (!to || date <= to);
      });
      const ledgerPageCount = Math.max(1, Math.ceil(filteredItems.length / ledgerPageSize));
      state.pointsPage = Math.min(state.pointsPage, ledgerPageCount - 1);
      const pageItems = filteredItems.slice(state.pointsPage * ledgerPageSize, (state.pointsPage + 1) * ledgerPageSize);
      const rows = pageItems.map(item => `<div class="points-row"><div><strong>${escapeHtml(item.reason || "积分变动")}</strong><small>${formatDate(item.created_at)}</small></div><div class="points-amount ${item.amount >= 0 ? "plus" : "minus"}">${item.amount >= 0 ? "+" : ""}${Number(item.amount).toFixed(1)}<small>余额 ${Number(item.balance_after).toFixed(1)}</small></div></div>`).join("");
      $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask"><div class="modal points-modal" role="dialog" aria-modal="true"><div class="modal-head"><div><span class="modal-kicker">积分中心</span><h2>积分余额与流水</h2></div><button class="close-btn" type="button" data-action="close-modal" aria-label="关闭">×</button></div><div class="points-balance"><span>当前剩余积分</span><strong>${Number(ledger.balance || 0).toFixed(1)}</strong></div><div class="points-filters"><input id="pointsKeyword" class="points-filter-input" placeholder="搜索流水内容" value="${escapeHtml(state.pointsKeyword)}" /><select id="pointsType" class="points-filter-select"><option value="all" ${state.pointsType === "all" ? "selected" : ""}>全部类型</option><option value="income" ${state.pointsType === "income" ? "selected" : ""}>增加</option><option value="expense" ${state.pointsType === "expense" ? "selected" : ""}>扣减</option></select><input id="pointsFrom" class="points-filter-date" type="date" value="${escapeHtml(state.pointsFrom)}" /><span class="points-filter-sep">至</span><input id="pointsTo" class="points-filter-date" type="date" value="${escapeHtml(state.pointsTo)}" /><button class="secondary-btn" type="button" data-action="points-filter">筛选</button><button class="text-btn" type="button" data-action="points-reset">重置</button></div><div class="points-list">${rows || `<div class="points-empty">暂无匹配的积分流水</div>`}</div>${filteredItems.length > ledgerPageSize ? `<div class="user-pagination"><button class="secondary-btn" data-action="points-page" data-page="${Math.max(0, state.pointsPage - 1)}" ${state.pointsPage === 0 ? "disabled" : ""}>上一页</button><span>第 ${state.pointsPage + 1} / ${ledgerPageCount} 页</span><button class="secondary-btn" data-action="points-page" data-page="${Math.min(ledgerPageCount - 1, state.pointsPage + 1)}" ${state.pointsPage >= ledgerPageCount - 1 ? "disabled" : ""}>下一页</button></div>` : ""}<div class="modal-actions"><button class="secondary-btn" type="button" data-action="recharge">联系充值</button><button class="cancel-btn" type="button" data-action="close-modal">关闭</button></div></div></div>`;
    } catch (error) { toast(error.message || "积分流水加载失败", "error"); $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask"><div class="modal points-modal" role="dialog" aria-modal="true"><div class="modal-head"><div><span class="modal-kicker">积分中心</span><h2>积分余额与流水</h2></div><button class="close-btn" type="button" data-action="close-modal" aria-label="关闭">×</button></div><div class="points-empty">积分流水暂时无法加载，请稍后重试。</div></div></div>`; }
  }

  function startAuthCooldown(seconds) {
    clearInterval(state.authCooldownTimer);
    state.authCooldown = Math.max(0, Math.ceil(Number(seconds) || 0));
    if (!state.authCooldown) return;
    updateAuthCooldownUI();
    state.authCooldownTimer = setInterval(() => {
      state.authCooldown -= 1;
      if (state.authCooldown <= 0) {
        state.authCooldown = 0;
        clearInterval(state.authCooldownTimer);
        state.authCooldownTimer = null;
      }
      updateAuthCooldownUI();
    }, 1000);
  }

  function updateAuthCooldownUI() {
    const button = document.querySelector('[data-action="request-code"]');
    if (!button) return;
    const active = state.authCooldown > 0;
    button.disabled = active;
    button.textContent = active ? `${state.authCooldown}s 后重发` : "获取验证码";
  }

  async function requestCode() {
    const email = $("#authEmail") ? $("#authEmail").value.trim() : "";
    if (!email || !email.includes("@")) { toast("请先输入正确的邮箱地址", "error"); return; }
    state.authEmail = email;
    try {
      const result = await api.requestCode(email, state.authMode === "forgot" ? "reset" : "register");
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
    if (state.authMode === "register" || state.authMode === "forgot") {
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
      try {
        if (state.authMode === "forgot") {
          await api.resetPassword({ email, password, code });
          clearInterval(state.authCooldownTimer);
          state.authCooldownTimer = null;
          state.authCooldown = 0;
          ["authCode", "authPassword", "authPassword2"].forEach((id) => {
            const input = document.getElementById(id);
            if (input) input.value = "";
          });
          state.authBusy = false;
          state.authMode = "login";
          state.authEmail = email;
          renderAuth();
          toast("密码已重置，请使用新密码登录");
        } else {
          state.profile = await api.register({ email, password, name, code });
          toast("注册成功，欢迎来到剧编编");
          await render();
        }
      }
      catch (error) { state.authBusy = false; renderAuth(); toast(error.message || (state.authMode === "forgot" ? "密码重置失败" : "注册失败"), "error"); }
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
    if (action === "admin-tab") { state.adminTab = actionElement.dataset.tab; await renderAdmin(); return; }
    if (action === "admin-search") { state.adminTaskKeyword = $("#adminTaskKeyword")?.value.trim() || ""; state.adminTaskStatus = $("#adminTaskStatus")?.value || "all"; state.adminTaskUser = $("#adminTaskUser")?.value || ""; state.adminTaskFrom = $("#adminTaskFrom")?.value || ""; state.adminTaskTo = $("#adminTaskTo")?.value || ""; state.adminTaskPage = 0; await renderAdmin(); return; }
    if (action === "admin-page") { state.adminTaskPage = Number(actionElement.dataset.page) || 0; await renderAdmin(); return; }
    if (action === "admin-user-search") { state.adminUserKeyword = $("#adminUserKeyword")?.value.trim() || ""; state.adminUserStatus = $("#adminUserStatus")?.value || "all"; state.adminUserPage = 0; await renderAdmin(); return; }
    if (action === "admin-user-page") { state.adminUserPage = Number(actionElement.dataset.page) || 0; await renderAdmin(); return; }
    if (action === "admin-recharge-search") { state.adminRechargeKeyword = $("#adminRechargeKeyword")?.value.trim() || ""; state.adminRechargeFrom = $("#adminRechargeFrom")?.value || ""; state.adminRechargeTo = $("#adminRechargeTo")?.value || ""; state.adminRechargePage = 0; await renderAdmin(); return; }
    if (action === "admin-recharge-reset") { state.adminRechargeKeyword = ""; state.adminRechargeFrom = ""; state.adminRechargeTo = ""; state.adminRechargePage = 0; await renderAdmin(); return; }
    if (action === "admin-recharge-page") { state.adminRechargePage = Number(actionElement.dataset.page) || 0; await renderAdmin(); return; }
    if (action === "admin-credit") {
      const rmbText = window.prompt("输入充值人民币金额（按 1 元 = 13.8 积分自动换算）", "100");
      if (rmbText === null) return;
      const rmbAmount = Number(rmbText);
      if (!Number.isFinite(rmbAmount) || rmbAmount <= 0) { toast("请输入有效人民币金额", "error"); return; }
      const pointsAmount = (rmbAmount * 13.8).toFixed(1);
      const rechargeReason = window.prompt(`本次将增加 ${pointsAmount} 积分，填写充值备注`, "管理员人民币充值");
      if (rechargeReason === null) return;
      try { await api.adjustAdminCredits(id, { rmb_amount: rmbAmount, reason: rechargeReason }); toast(`充值成功，已增加 ${pointsAmount} 积分`); await renderAdmin(); } catch (error) { toast(error.message || "充值失败", "error"); }
      return;
      /* legacy minute adjustment flow retained for compatibility */
      const amountText = window.prompt("输入调整分钟数（充值填正数，扣减填负数）", "100");
      if (amountText === null) return;
      const amount = Number(amountText);
      if (!Number.isInteger(amount) || amount === 0) { toast("请输入非零整数", "error"); return; }
      const reason = window.prompt("调整原因", "测试额度调整") || "管理员调整";
      try { await api.adjustAdminCredits(id, { amount, reason }); toast("额度已更新"); await renderAdmin(); } catch (error) { toast(error.message || "额度更新失败", "error"); }
      return;
    }
    if (action === "admin-status") {
      try { await api.setAdminUserStatus(id, actionElement.dataset.active === "1"); toast("账号状态已更新"); await renderAdmin(); } catch (error) { toast(error.message || "状态更新失败", "error"); }
      return;
    }
    if (action === "admin-retry") {
      try { await api.retryAdminTask(id); toast("任务已重新排队"); await renderAdmin(); } catch (error) { toast(error.message || "任务重试失败", "error"); }
      return;
    }
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
    if (action === "auth-forgot") {
      clearInterval(state.authCooldownTimer);
      state.authCooldownTimer = null;
      state.authCooldown = 0;
      state.authMode = "forgot";
      state.authBusy = false;
      renderAuth();
      return;
    }
    if (action === "request-code") { await requestCode(); return; }
    if (action === "auth-submit") { event.preventDefault(); await submitAuth(); return; }
    if (action === "points") { await openPointsModal(); return; }
    if (action === "task-page") { state.taskPage = Number(actionElement.dataset.page) || 0; renderList(); return; }
    if (action === "points-page") { state.pointsPage = Number(actionElement.dataset.page) || 0; await openPointsModal(); return; }
    if (action === "points-filter") {
      state.pointsKeyword = $("#pointsKeyword")?.value.trim() || "";
      state.pointsType = $("#pointsType")?.value || "all";
      state.pointsFrom = $("#pointsFrom")?.value || "";
      state.pointsTo = $("#pointsTo")?.value || "";
      state.pointsPage = 0;
      await openPointsModal();
      return;
    }
    if (action === "points-reset") {
      state.pointsKeyword = "";
      state.pointsType = "all";
      state.pointsFrom = "";
      state.pointsTo = "";
      state.pointsPage = 0;
      await openPointsModal();
      return;
    }
    if (action === "logout") { await logout(); return; }
    if (action === "create") openCreateModal();
    if (action === "close-modal") $("#modalRoot").innerHTML = "";
    if (action === "back") go("tasks");
    if (action === "account") openAccountModal();
    if (action === "recharge") openRechargeModal();
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
    if (action === "download-all") {
      try {
        const payload = await api.getExportAll(actionElement.dataset.format || "md");
        const url = URL.createObjectURL(payload.blob); const link = document.createElement("a"); link.href = url; link.download = payload.fileName; link.click(); URL.revokeObjectURL(url);
      } catch (error) { toast(error.message || "下载失败", "error"); }
    }
    if (action === "remove-file") {
      const titleValue = $("#taskTitle") ? $("#taskTitle").value : "";
      state.upload = { files: [], file: null, durationSec: 0, reading: false, error: "" };
      rerenderCreateModal(titleValue);
    }
    if (action === "submit-task") {
      const files = state.upload.files || (state.upload.file ? [state.upload.file] : []);
      const title = $("#taskTitle") ? $("#taskTitle").value.trim() : "";
      if (!files.length || !state.upload.durationSec) return;
      actionElement.disabled = true;
      actionElement.textContent = "正在创建...";
      try {
        const task = await api.createTask({
          title,
          files,
          durationSec: Math.round(state.upload.durationSec),
          estimatedMinutes: uploadEstimate(),
        });
        state.profile = await api.getProfile();
        renderHeader();
        $("#modalRoot").innerHTML = "";
        toast("任务已创建，正在后台识别");
        go("tasks");
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
    if (event.target.id === "fileInput") setUploadFiles(event.target.files);
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
    setUploadFiles(event.dataTransfer.files);
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
