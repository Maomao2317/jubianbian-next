/**
 * Hash-routed workspace UI. The page renders from the API contract only; mock
 * mode and the future FastAPI implementation share the same view code.
 */
(function () {
  const cfg = window.APP_CONFIG;
  const POINTS_PER_MINUTE = 5;
  const POINTS_PER_YUAN = 12.8;
  const api = window.AppAPI;
  const $ = (selector, root = document) => root.querySelector(selector);

  const state = {
    view: "list",
    taskId: "",
    keyword: "",
    status: "all",
    tasks: [],
    current: null,
    reviewIds: [],
    profile: null,
    authMode: "login",
    authBusy: false,
    authCooldown: 0,
    authCooldownTimer: null,
    authEmail: "",
    menuTaskId: "",
    upload: { files: [], file: null, durations: [], durationSec: 0, estimatedMinutes: 0, reading: false, error: "" },
    pollTimer: null,
    rateLimitNotified: false,
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
    adminRechargeType: "all",
    taskPage: 0,
    expandedBatches: {},
    queue: { waitMinutes: 0, blocked: false, workerConcurrency: 8 },
    pointsPage: 0,
    pointsKeyword: "",
    pointsType: "all",
    pointsFrom: "",
    pointsTo: "",
    listRequestId: 0,
    listLoading: false,
    groupedTasksSource: null,
    groupedTasksCache: null,
    adminRequestId: 0,
    adminOverviewCache: null,
    adminOverviewCachedAt: 0,
    adminTaskUsersCache: null,
    adminTaskUsersCachedAt: 0,
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
    if (Number.isNaN(date.getTime())) return "时间未知";
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(date).reduce((result, item) => ({ ...result, [item.type]: item.value }), {});
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
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

  function mobileUploadDisabled() {
    return Boolean(window.matchMedia?.("(max-width: 620px)").matches);
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
    const displayStatus = task.status;
    const displayStage = task.stage;
    let text = displayStatus === "running"
      ? cfg.stageText[displayStage]
      : cfg.statusText[displayStatus] || displayStatus;
    if (mediaProgressLabel(task)) text += ` ${compressionPercent(task)}%`;
    return `<span class="badge ${displayStatus}"><span class="badge-dot"></span>${escapeHtml(text)}</span>`;
  }

  function isCompletedTask(task) {
    return task.status === "done";
  }

  function compressionPercent(task) {
    return Math.max(0, Math.min(100, Math.floor(Number(task.stageProgressPercent) || 0)));
  }

  function mediaProgressLabel(task) {
    if (task.status !== "running") return "";
    if (task.stage === "compressing") return "压缩";
    if (task.stageProgressPercent == null) return "";
    return { source_download: "原视频读取", video_upload: "识别视频提交" }[task.stage] || "";
  }

  function normalizeUserTask(task) {
    return task;
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
        <div class="header-right">
          <button class="credit-pill" type="button" data-action="points" title="查看积分余额和流水">
            <span class="credit-label">剩余积分</span>
            <strong>${credits}</strong><span>积分</span>
          </button>
          ${state.profile && state.profile.role === "admin" ? `<a class="text-btn admin-link" href="#/admin"><img class="nav-icon admin-nav-logo" src="${cfg.brand.adminLogo || cfg.brand.logo}" alt="" />管理后台</a>` : ""}
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

  const ledgerTypes = {
    recharge: { label: "充值", symbol: "+" },
    refund: { label: "退费", symbol: "↩" },
    consume: { label: "扣款", symbol: "−" },
    grant: { label: "赠送", symbol: "★" },
    adjustment: { label: "人工调整", symbol: "⇄" },
    other: { label: "其他变动", symbol: "·" },
  };

  function ledgerBadge(type) {
    const key = Object.hasOwn(ledgerTypes, type) ? type : "other";
    const item = ledgerTypes[key];
    return `<span class="ledger-badge ledger-${key}"><span aria-hidden="true">${item.symbol}</span>${item.label}</span>`;
  }

  function adminLedgerRow(item) {
    const amount = Number(item.points_amount) || 0;
    const sign = amount > 0 ? "+" : amount < 0 ? "−" : "";
    const tone = amount > 0 ? "income" : amount < 0 ? "expense" : "neutral";
    const rmb = item.rmb_amount == null
      ? (item.entry_type === "recharge" ? "未记录" : "—")
      : `¥ ${Number(item.rmb_amount).toFixed(2)}`;
    const reason = item.reason === "task recognition failed" ? "视频识别失败，积分已退回"
      : item.reason === "failed task retry" ? "失败任务重试扣款" : item.reason || "—";
    return `<tr><td><strong>${escapeHtml(item.name || item.email || item.user_id)}</strong><small>${escapeHtml(item.email || "")}</small></td><td>${ledgerBadge(item.entry_type)}</td><td class="ledger-money">${rmb}</td><td class="ledger-amount ledger-${tone}">${sign}${Math.abs(amount).toFixed(1)} <span>积分</span></td><td class="ledger-balance">${Number(item.balance_after).toFixed(1)} 积分</td><td class="ledger-reason">${escapeHtml(reason)}</td><td>${formatDate(item.created_at)}</td></tr>`;
  }

  function adminUserIdentity(user) {
    return `<strong>${escapeHtml(user.name || "未命名用户")}</strong><small>${escapeHtml(user.email || user.user_id || user.id || "—")}</small>`;
  }

  function adminTaskStatus(item) {
    const labels = { done: "成功", review: "待用户复核", failed: "失败", running: "进行中", queued: "排队中" };
    const partial = item.status === "failed" && Number(item.failed_count) < Number(item.task_count);
    const label = partial ? "部分失败" : Object.hasOwn(labels, item.status) ? labels[item.status] : "状态未知";
    const tone = item.status === "done" ? "on" : item.status === "failed" ? "off" : "wait";
    const counts = [["done_count", "完成"], ["running_count", "进行中"], ["queued_count", "排队"], ["review_count", "待复核"], ["failed_count", "失败"]];
    const detail = Number(item.task_count) > 1 ? counts.filter(([key]) => Number(item[key]) > 0).map(([key, text]) => `${text} ${Number(item[key])}`).join(" · ") : "";
    return `<span class="admin-status ${tone}">${label}</span>${detail ? `<small class="admin-task-counts">${detail}</small>` : ""}`;
  }

  function adminModelUsage(item) {
    const usage = item.model_usage || {};
    const line = (key, label) => {
      const value = usage[key] || {};
      const calls = Number(value.calls) || 0;
      const tokens = Number(value.total_tokens) || 0;
      const cost = Number(value.api_cost_rmb) || 0;
      const estimate = value.estimated ? "约 " : "";
      const failures = Number(value.failed_calls) ? ` / 失败 ${Number(value.failed_calls)}` : "";
      return `<span class="admin-model-line admin-model-${key}"><strong>${label}</strong><small>${calls ? `${calls} 次${failures} · ${tokens.toLocaleString()} Tokens · ${estimate}¥ ${cost.toFixed(4)}` : "0 次"}</small></span>`;
    };
    return `<div class="admin-model-usage">${line("lite", "Lite")}${line("turbo", "Turbo")}</div>`;
  }

  function adminTaskBilling(item) {
    const minutes = Number(item.credits_used) || 0;
    const points = Number(item.points_used ?? (minutes * POINTS_PER_MINUTE)) || 0;
    const rmb = Number(item.points_value_rmb ?? (points / POINTS_PER_YUAN)) || 0;
    return `<span class="admin-task-billing"><strong>${minutes.toFixed(1)} 分钟</strong><small>消耗 ${points.toFixed(1)} 积分 · 折合 ¥ ${rmb.toFixed(2)}</small></span>`;
  }

  function adminEconomics(item) {
    const revenue = Number(item.points_value_rmb) || 0;
    const cost = Number(item.api_cost_rmb) || 0;
    const profit = Number(item.gross_profit_rmb ?? (revenue - cost)) || 0;
    const margin = item.gross_margin == null ? "—" : `${(Number(item.gross_margin) * 100).toFixed(1)}%`;
    const status = item.economics_status || (profit < 0 ? "loss" : "healthy");
    const labels = { healthy: "收益正常", risk: "收益风险", loss: "任务亏损" };
    const mix = item.model_mix_status === "lite_dominant" ? "Lite > Turbo" : item.model_mix_status === "turbo_not_lower" ? "Turbo ≥ Lite" : "暂无调用";
    return `<span class="admin-economics admin-economics-${status}"><span class="admin-economics-head"><strong>${labels[status] || "收益待确认"}</strong><em>${mix}</em></span><small>收入 ¥ ${revenue.toFixed(2)} · 成本 ¥ ${cost.toFixed(4)}</small><small>毛利 ¥ ${profit.toFixed(2)} · ${margin}</small></span>`;
  }

  function adminDateRangeValid(from, to) {
    if (from && to && from > to) { toast("开始日期不能晚于结束日期", "error"); return false; }
    return true;
  }

  function adminPagination(action, page, total, size) {
    const count = Math.max(1, Math.ceil(total / size));
    return `<div class="admin-pagination"><button class="admin-action" data-action="${action}" data-page="${Math.max(0, page - 1)}" ${page === 0 ? "disabled" : ""}>上一页</button><span>第 ${page + 1} / ${count} 页</span><button class="admin-action" data-action="${action}" data-page="${Math.min(count - 1, page + 1)}" ${page >= count - 1 ? "disabled" : ""}>下一页</button></div>`;
  }

  async function renderAdmin() {
    if (!state.profile || state.profile.role !== "admin") { go("tasks"); return; }
    const requestId = ++state.adminRequestId;
    document.querySelector(".admin-page")?.setAttribute("aria-busy", "true");
    try {
      const tabs = [{id:"overview",label:"概览"},{id:"users",label:"用户管理"},{id:"credits",label:"充值与流水"},{id:"tasks",label:"任务管理"},{id:"usage",label:"用量记录"}];
      const tab = state.adminTab;
      const overviewPromise = state.adminOverviewCache && Date.now() - state.adminOverviewCachedAt < 15000
        ? Promise.resolve(state.adminOverviewCache)
        : api.getAdminOverview().then((overview) => {
          state.adminOverviewCache = overview;
          state.adminOverviewCachedAt = Date.now();
          return overview;
        });
      let users = null, rechargePage = null, taskUsers = null, tasks = null;
      const panelPromises = [];
      if (tab === "users") {
        panelPromises.push(api.getAdminUsers({ limit: 20, offset: state.adminUserPage * 20, keyword: state.adminUserKeyword, status: state.adminUserStatus }).then((value) => { users = value; }));
      } else if (tab === "credits") {
        panelPromises.push(api.getAdminRecharges({ limit: 10, offset: state.adminRechargePage * 10, keyword: state.adminRechargeKeyword, date_from: state.adminRechargeFrom, date_to: state.adminRechargeTo, entry_type: state.adminRechargeType }).then((value) => { rechargePage = value; }));
      } else if (tab === "tasks" || tab === "usage") {
        const taskUsersPromise = state.adminTaskUsersCache && Date.now() - state.adminTaskUsersCachedAt < 30000
          ? Promise.resolve(state.adminTaskUsersCache)
          : api.getAdminUsers({ limit: 200 }).then((value) => {
            state.adminTaskUsersCache = value;
            state.adminTaskUsersCachedAt = Date.now();
            return value;
          });
        panelPromises.push(taskUsersPromise.then((value) => { taskUsers = value; }));
        panelPromises.push(api.getAdminTasks({ limit: 20, offset: state.adminTaskPage * 20, keyword: state.adminTaskKeyword, status: state.adminTaskStatus, user_id: state.adminTaskUser, date_from: state.adminTaskFrom, date_to: state.adminTaskTo }).then((value) => { tasks = value; }));
      }
      const overview = await overviewPromise;
      await Promise.all(panelPromises);
      if (requestId !== state.adminRequestId || state.view !== "admin") return;
      for (const [page, key, size] of [[users, "adminUserPage", 20], [rechargePage, "adminRechargePage", 10], [tasks, "adminTaskPage", 20]]) {
        if (!page) continue;
        const last = Math.max(0, Math.ceil(page.total / size) - 1);
        if (state[key] > last) { state[key] = last; await renderAdmin(); return; }
      }
      const nav = tabs.map(item => `<button class="admin-tab ${tab === item.id ? "active" : ""}" aria-pressed="${tab === item.id}" data-action="admin-tab" data-tab="${item.id}">${item.label}</button>`).join("");
      const billing = overview.apiCostSource === "provider_billing";
      let body = `<div class="admin-stats"><article><span>用户总数</span><strong>${overview.users.total || 0}</strong><small>正常账号 ${overview.users.active || 0}</small></article><article><span>用户积分余额合计</span><strong>${Number(overview.users.credits || 0).toFixed(1)}</strong><small>积分</small></article><article><span>视频任务总数</span><strong>${overview.tasks.total || 0}</strong><small>进行中 ${overview.tasks.running || 0} · 排队 ${overview.tasks.queued || 0}</small><small>完成 ${overview.tasks.done || 0} · 待复核 ${overview.tasks.review || 0} · 失败 ${overview.tasks.failed || 0}</small></article><article><span>${billing ? "本月 API 账单" : "API 调用成本"}</span><strong>¥ ${Number(overview.apiCostRmb || 0).toFixed(2)}</strong><small>${billing ? `${escapeHtml(overview.apiCostMonth || "")} · 服务商账单有延迟` : "累计本地记录 · 本月账单暂未获取"}</small></article></div>`;
      if (tab === "users") {
        body += `<div class="admin-panel"><div class="admin-panel-head"><div><h2>用户管理</h2><p>人民币充值与账号启停；1 分钟 = ${POINTS_PER_MINUTE} 积分</p></div><span>${users.total} 个账号</span></div><div class="admin-toolbar"><input id="adminUserKeyword" aria-label="搜索用户名称或邮箱" value="${escapeHtml(state.adminUserKeyword)}" placeholder="搜索用户名称或邮箱" /><select id="adminUserStatus" aria-label="账号状态"><option value="all" ${state.adminUserStatus === "all" ? "selected" : ""}>全部账号</option><option value="active" ${state.adminUserStatus === "active" ? "selected" : ""}>正常</option><option value="disabled" ${state.adminUserStatus === "disabled" ? "selected" : ""}>已停用</option></select><button class="admin-action admin-search-btn" data-action="admin-user-search">筛选</button><button class="admin-action" data-action="admin-user-reset">重置</button></div><div class="admin-table-wrap"><table><thead><tr><th>用户名称 / 账号</th><th>角色</th><th>状态</th><th>剩余积分</th><th>累计计费 / 可用时长</th><th>操作</th></tr></thead><tbody>${users.items.map(user => `<tr><td>${adminUserIdentity(user)}</td><td>${user.role === "admin" ? "管理员" : "用户"}</td><td><span class="admin-status ${user.isActive ? "on" : "off"}">${user.isActive ? "正常" : "已停用"}</span></td><td><strong>${Number(user.credits || 0).toFixed(1)}</strong> 积分</td><td><strong>已用 ${Number(user.totalUsed || 0).toFixed(1)} 分钟</strong><small>可用 ${(Number(user.credits || 0) / POINTS_PER_MINUTE).toFixed(1)} 分钟</small></td><td><button class="admin-action" data-action="admin-credit" data-id="${escapeHtml(user.id)}">人民币充值</button><button class="admin-action ${user.isActive ? "admin-danger" : ""}" data-action="admin-status" data-id="${escapeHtml(user.id)}" data-active="${user.isActive ? "0" : "1"}" ${user.id === state.profile.id ? 'disabled title="不能停用当前登录账号"' : ""}>${user.id === state.profile.id ? "当前账号" : user.isActive ? "停用" : "启用"}</button></td></tr>`).join("") || `<tr><td colspan="6">暂无匹配用户，可重置筛选后查看</td></tr>`}</tbody></table></div>${adminPagination("admin-user-page", state.adminUserPage, users.total, 20)}</div>`;
      } else if (tab === "credits") {
        body += `<div class="admin-panel recharge-ledger-panel"><div class="admin-panel-head"><div><h2>充值与积分流水</h2><p>充值按 1 元 = 12.8 积分入账；退费为积分退回，扣款为积分消费。为用户充值请前往“用户管理”。</p></div><span>${rechargePage.total} 笔流水</span></div><div class="ledger-legend" aria-label="流水类型说明">${["recharge", "refund", "consume", "grant", "adjustment"].map(ledgerBadge).join("")}<span class="ledger-legend-note">＋ 积分增加 / − 积分减少</span></div><div class="admin-toolbar recharge-toolbar"><input id="adminRechargeKeyword" aria-label="搜索用户账号或备注" value="${escapeHtml(state.adminRechargeKeyword)}" placeholder="搜索用户名称、账号或备注" /><select id="adminRechargeType" aria-label="流水类型"><option value="all">全部类型</option>${Object.entries(ledgerTypes).map(([key, value]) => `<option value="${key}" ${state.adminRechargeType === key ? "selected" : ""}>${value.symbol} ${value.label}</option>`).join("")}</select><label class="admin-date-label">开始日期<input id="adminRechargeFrom" aria-label="流水开始日期" title="开始日期" type="date" value="${state.adminRechargeFrom}" /></label><label class="admin-date-label">结束日期<input id="adminRechargeTo" aria-label="流水结束日期" title="结束日期" type="date" value="${state.adminRechargeTo}" /></label><button class="admin-action admin-search-btn" data-action="admin-recharge-search">筛选</button><button class="admin-action" data-action="admin-recharge-reset">重置</button></div><div class="admin-table-wrap"><table><thead><tr><th>用户名称 / 账号</th><th>流水类型</th><th>充值金额（元）</th><th>积分变动</th><th>变动后余额</th><th>备注</th><th>时间</th></tr></thead><tbody>${rechargePage.items.map(adminLedgerRow).join("") || `<tr><td colspan="7">暂无匹配的流水，可重置筛选后查看</td></tr>`}</tbody></table></div>${adminPagination("admin-recharge-page", state.adminRechargePage, rechargePage.total, 10)}</div>`;
      } else if (tab === "tasks" || tab === "usage") {
        body += `<div class="admin-panel"><div class="admin-panel-head"><div><h2>${tab === "usage" ? "用量记录" : "任务管理"}</h2><p>按批次汇总；默认 Lite 优先，只有质量门禁不通过且利润预算允许时才升级 Turbo。</p></div><span>${tasks.total} 个批次</span></div><div class="admin-toolbar"><input id="adminTaskKeyword" aria-label="搜索任务、用户名称或账号" value="${escapeHtml(state.adminTaskKeyword)}" placeholder="搜索任务、用户名称或账号" /><select id="adminTaskUser" aria-label="任务所属用户"><option value="">全部用户</option>${taskUsers.items.map(user => `<option value="${escapeHtml(user.id)}" ${state.adminTaskUser === user.id ? "selected" : ""}>${escapeHtml(user.name || "未命名用户")} · ${escapeHtml(user.email)}</option>`).join("")}</select><select id="adminTaskStatus" aria-label="任务状态"><option value="all" ${state.adminTaskStatus === "all" ? "selected" : ""}>全部状态</option><option value="done" ${state.adminTaskStatus === "done" ? "selected" : ""}>成功</option><option value="review" ${state.adminTaskStatus === "review" ? "selected" : ""}>待用户复核</option><option value="failed" ${state.adminTaskStatus === "failed" ? "selected" : ""}>失败</option><option value="running" ${state.adminTaskStatus === "running" ? "selected" : ""}>进行中</option><option value="queued" ${state.adminTaskStatus === "queued" ? "selected" : ""}>排队中</option></select><label class="admin-date-label">开始日期<input id="adminTaskFrom" type="date" aria-label="任务开始日期" title="开始日期" value="${state.adminTaskFrom}" /></label><label class="admin-date-label">结束日期<input id="adminTaskTo" type="date" aria-label="任务结束日期" title="结束日期" value="${state.adminTaskTo}" /></label><button class="admin-action admin-search-btn" data-action="admin-search">筛选</button><button class="admin-action" data-action="admin-task-reset">重置</button></div><div class="admin-table-wrap"><table class="admin-task-table"><thead><tr><th>任务 / 用户</th><th>状态</th><th>计费 / 模型</th><th>收益</th><th>时间 / 操作</th></tr></thead><tbody>${tasks.items.map(item => {
          const reviewId = (Array.isArray(item.review_task_ids) && item.review_task_ids[0]) || item.id;
          const actions = `<button class="admin-action" data-action="admin-review" data-id="${escapeHtml(reviewId)}">查看详情</button>` + (item.status === "failed" ? `<button class="admin-action" data-action="admin-retry" data-id="${escapeHtml(item.id)}" title="重试失败任务通常会重新扣费，已有免费复核任务除外">重试</button>` : "");
          return `<tr><td class="admin-task-title"><button class="row-link" data-action="admin-review" data-id="${escapeHtml(reviewId)}">${escapeHtml(item.title || item.id)}</button><small>${escapeHtml(item.file_name || "")}</small><span class="admin-task-user">${adminUserIdentity(item)}</span></td><td>${adminTaskStatus(item)}</td><td>${adminTaskBilling(item)}${adminModelUsage(item)}</td><td>${adminEconomics(item)}</td><td><span class="admin-task-time"><small>创建 ${formatDate(item.created_at)}</small><small>完成 ${item.completed_at ? formatDate(item.completed_at) : "—"}</small></span><span class="admin-task-actions">${actions}</span></td></tr>`;
        }).join("") || `<tr><td colspan="5">暂无匹配任务，可重置筛选后查看</td></tr>`}</tbody></table></div>${adminPagination("admin-page", state.adminTaskPage, tasks.total, 20)}</div>`;
      } else {
        body += `<div class="admin-panel"><div class="admin-panel-head"><div><h2>最近视频任务</h2><p>最近创建的 10 条视频任务</p></div><span>点击任务查看详情</span></div><div class="admin-table-wrap"><table><thead><tr><th>任务</th><th>用户名称 / 账号</th><th>状态</th><th>计费时长</th><th>创建时间</th></tr></thead><tbody>${(overview.recentTasks || []).map(item => `<tr><td class="admin-task-title"><button class="row-link" data-action="admin-review" data-id="${escapeHtml(item.id)}">${escapeHtml(item.title || item.id)}</button></td><td>${adminUserIdentity(item)}</td><td>${adminTaskStatus(item)}</td><td>${Number(item.credits_used || 0).toFixed(1)} 分钟</td><td>${formatDate(item.created_at)}</td></tr>`).join("") || `<tr><td colspan="5">暂无视频任务</td></tr>`}</tbody></table></div></div>`;
      }
      $("#main").innerHTML = `<section class="admin-page" aria-busy="false"><div class="admin-heading"><div><span class="eyebrow">ADMIN CONSOLE</span><h1>管理员后台</h1><p>平台运行、用户积分和任务处理</p></div><div class="admin-heading-actions"><button class="secondary-btn" type="button" data-action="admin-refresh">刷新数据</button><a class="secondary-btn admin-back-btn" href="#/tasks"><span aria-hidden="true">←</span>返回工作台</a></div></div><div class="admin-nav" aria-label="后台功能">${nav}</div>${body}<p class="admin-note">数据更新于 ${formatDate(overview.timestamp)}；点击“刷新数据”获取最新状态。</p></section>`;
      // Keep the same table and actions on every screen; mobile cards use its headers as labels.
      document.querySelectorAll(".admin-table-wrap table").forEach(table => {
        const labels = Array.from(table.querySelectorAll("thead th"), header => header.textContent.trim());
        table.querySelectorAll("tbody tr").forEach(row => {
          Array.from(row.children).forEach((cell, index) => {
            if (cell.colSpan !== 1) return;
            cell.setAttribute("data-label", labels[index] || "");
            const value = document.createElement("div");
            value.className = "admin-cell-value";
            while (cell.firstChild) value.appendChild(cell.firstChild);
            cell.appendChild(value);
          });
        });
      });
    } catch (error) {
      if (requestId !== state.adminRequestId) return;
      document.querySelector(".admin-page")?.setAttribute("aria-busy", "false");
      toast(error.message || "后台数据加载失败", "error");
    }
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
      ${download}${retry}
      <button class="danger-item" type="button" data-action="ask-delete" data-id="${task.id}">删除任务</button>
    </div>`;
  }

  function taskRow(task) {
    const meta = [
      task.fileName || "未命名视频",
      formatDuration(task.durationSec),
      `${(Number(task.creditsUsed ?? task.estimatedMinutes ?? 0) * POINTS_PER_MINUTE).toFixed(1)} 积分`,
      timeAgo(task.createdAt),
    ];
    const compressing = task.status === "running" && task.stage === "compressing";
    const mediaLabel = mediaProgressLabel(task);
    const percent = mediaLabel ? compressionPercent(task) : (task.progressPercent || 0);
    const progressLabel = mediaLabel === "压缩" ? "服务器压缩进度" : mediaLabel ? `${mediaLabel}进度` : "处理进度";
    const progress = task.status === "running" || task.status === "queued"
      ? `<div class="row-progress ${compressing ? "compression-progress" : ""}" role="progressbar" aria-label="${progressLabel}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div>`
      : "";
    const quickAction = isCompletedTask(task)
      ? `<button class="row-link" type="button" data-action="open-task" data-id="${task.id}">查看剧本</button><button class="row-link row-download" type="button" data-action="download" data-id="${task.id}" data-format="md">下载 MD</button><button class="row-link row-download" type="button" data-action="download" data-id="${task.id}" data-format="txt">下载 TXT</button>`
      : task.status === "failed"
        ? `<button class="row-link" type="button" data-action="retry" data-id="${task.id}">重试</button>`
        : task.status === "review" ? `<button class="row-link" type="button" data-action="open-task" data-id="${task.id}">查看并复核</button>` : "";
    return `<article class="task-row" data-open="${task.id}" data-scroll-key="task:${escapeHtml(task.id)}">
      <div class="file-mark" aria-hidden="true"><span></span></div>
      <div class="task-main">
        <div class="task-title-line"><div class="task-title">${escapeHtml(task.title)}</div><div class="task-inline-actions">${statusBadge(task)}${quickAction}</div></div>
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

  function groupedTaskItems(tasks) {
    if (state.groupedTasksSource === tasks && state.groupedTasksCache) return state.groupedTasksCache;
    const groups = new Map();
    const items = [];
    // The API already returns tasks in recency order. Avoid sorting the full
    // history again on every render; only the visible batch children are
    // sorted by episode number in batchRow().
    tasks.forEach((task) => {
      if (!task.batchId) { items.push({ type: "task", task }); return; }
      let group = groups.get(task.batchId);
      if (!group) {
        group = { type: "batch", id: task.batchId, title: task.batchTitle || "短剧批次", tasks: [] };
        groups.set(task.batchId, group);
        items.push(group);
      }
      group.tasks.push(task);
    });
    state.groupedTasksSource = tasks;
    state.groupedTasksCache = items;
    return items;
  }

  function batchRow(group) {
    const done = group.tasks.filter((task) => isCompletedTask(task)).length;
    const running = group.tasks.filter((task) => task.status === "running").length;
    const failed = group.tasks.filter((task) => task.status === "failed").length;
    const expanded = Boolean(state.expandedBatches[group.id]);
    const status = failed ? "部分失败" : done === group.tasks.length ? "已完成" : running ? "进行中" : "待开始";
    return `<div class="batch-group ${expanded ? "is-expanded" : ""}"><button class="batch-row" type="button" data-action="toggle-batch" data-id="${group.id}" aria-expanded="${expanded}"><span class="batch-chevron">${expanded ? "⌄" : "›"}</span><span class="batch-main"><strong>${escapeHtml(group.title)}</strong><small>${group.tasks.length} 集 · 已完成 ${done} 集${running ? ` · 进行中 ${running} 集` : ""}</small></span><span class="batch-status ${failed ? "failed" : done === group.tasks.length ? "done" : "pending"}">${status}</span></button>${expanded ? `<div class="batch-children">${group.tasks.map(taskRow).join("")}</div>` : ""}</div>`;
  }

  function formatEta(minutes) {
    const value = Math.max(0, Math.ceil(Number(minutes) || 0));
    if (!value) return "已完成";
    if (value < 60) return `预计约 ${value} 分钟`;
    const hours = Math.floor(value / 60);
    const rest = value % 60;
    return `预计约 ${hours} 小时${rest ? ` ${rest} 分钟` : ""}`;
  }

  // Batch rows intentionally keep each episode action available after expansion.
  function batchRow(group) {
    const done = group.tasks.filter((task) => isCompletedTask(task)).length;
    const review = group.tasks.filter((task) => task.status === "review").length;
    const running = group.tasks.filter((task) => task.status === "running").length;
    const compressing = group.tasks.filter((task) => task.status === "running" && task.stage === "compressing").length;
    const compressionQueued = group.tasks.filter((task) => task.status === "running" && task.stage === "compression_queued").length;
    const failed = group.tasks.filter((task) => task.status === "failed").length;
    const expanded = Boolean(state.expandedBatches[group.id]);
    const remaining = group.tasks.reduce((sum, task) => !["queued", "running"].includes(task.status) ? sum : sum + Number(task.estimatedMinutes || 1) * Math.max(0.05, 1 - Number(task.progressPercent || 0) / 100), 0);
    const eta = remaining ? formatEta(Math.ceil(remaining / Math.max(1, Number(state.queue.workerConcurrency || 8)))) : review ? "等待用户复核" : failed ? "请检查失败任务" : "已完成";
    const status = failed ? "部分失败" : done === group.tasks.length ? "已完成" : running ? "进行中" : review ? "待复核" : "待开始";
    const downloads = `<span class="batch-downloads">${review ? `<button class="row-link" type="button" data-action="review-batch" data-id="${group.id}">批量复核（${review}集）</button>` : ""}${done ? `<button class="row-link" type="button" data-action="download-batch" data-id="${group.id}" data-format="md">下载 MD</button><button class="row-link" type="button" data-action="download-batch" data-id="${group.id}" data-format="txt">下载 TXT</button>` : ""}</span>`;
    const percent = group.tasks.length ? Math.round(group.tasks.reduce((sum, task) => sum + (task.status === "failed" ? 0 : isCompletedTask(task) ? 100 : Number(task.progressPercent || 0)), 0) / group.tasks.length) : 0;
    const compressionSummary = `${review ? ` · 待复核 ${review} 集` : ""}${compressing ? ` · 压缩中 ${compressing} 集` : ""}${compressionQueued ? ` · 等待压缩 ${compressionQueued} 集` : ""}`;
    const totalDurationSec = group.tasks.reduce((sum, task) => sum + Math.max(0, Number(task.durationSec) || 0), 0);
    const durationSummary = totalDurationSec > 0 ? ` · 视频总时长 ${formatDuration(totalDurationSec)}` : "";
    return `<div class="batch-group ${expanded ? "is-expanded" : ""}" data-scroll-key="batch:${escapeHtml(group.id)}"><div class="batch-row"><button class="batch-toggle" type="button" data-action="toggle-batch" data-id="${group.id}" aria-expanded="${expanded}"><span class="batch-chevron">${expanded ? "⌄" : "›"}</span><span class="batch-main"><strong>${escapeHtml(group.title)}</strong><small>${group.tasks.length} 集 · 已完成 ${done} 集${running ? ` · 进行中 ${running} 集` : ""}${compressionSummary} · ${percent}% · ${eta}${durationSummary}</small></span><span class="batch-status ${failed ? "failed" : done === group.tasks.length ? "done" : "pending"}">${status}</span></button>${downloads}</div>${expanded ? `<div class="batch-children">${group.tasks.slice().sort((a, b) => { const ae = Number.isFinite(Number(a.episodeNumber)) ? Number(a.episodeNumber) : Number.POSITIVE_INFINITY; const be = Number.isFinite(Number(b.episodeNumber)) ? Number(b.episodeNumber) : Number.POSITIVE_INFINITY; return ae - be || Number(a.batchIndex || 0) - Number(b.batchIndex || 0); }).map(taskRow).join("")}</div>` : ""}</div>`;
  }

  function captureListViewport() {
    if (!$(`.workspace-panel`)) return null;
    const headerBottom = $("#header")?.getBoundingClientRect().bottom || 0;
    const items = [...document.querySelectorAll("[data-scroll-key]")];
    const anchor = items.find((item) => item.getBoundingClientRect().top >= headerBottom + 8)
      || items.find((item) => item.getBoundingClientRect().bottom > headerBottom + 8);
    const active = document.activeElement?.id === "keyword" ? document.activeElement : null;
    return {
      scrollY: window.scrollY,
      key: anchor?.dataset.scrollKey || "",
      top: anchor?.getBoundingClientRect().top || 0,
      searchFocused: Boolean(active),
      selectionStart: active?.selectionStart,
      selectionEnd: active?.selectionEnd,
    };
  }

  function restoreListViewport(snapshot) {
    if (!snapshot) return;
    const anchor = [...document.querySelectorAll("[data-scroll-key]")]
      .find((item) => item.dataset.scrollKey === snapshot.key);
    if (anchor) {
      window.scrollBy(0, anchor.getBoundingClientRect().top - snapshot.top);
    } else {
      window.scrollTo({ top: snapshot.scrollY, behavior: "auto" });
    }
    if (snapshot.searchFocused) {
      const search = $("#keyword");
      search?.focus({ preventScroll: true });
      if (Number.isInteger(snapshot.selectionStart) && Number.isInteger(snapshot.selectionEnd)) {
        search?.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd);
      }
    }
  }

  function setListLoading(loading) {
    state.listLoading = loading;
    const panel = $(".workspace-panel");
    panel?.classList.toggle("is-loading", loading);
    panel?.setAttribute("aria-busy", String(loading));
  }

  function updateActiveFilter() {
    document.querySelectorAll("[data-filter]").forEach((button) => {
      const active = button.dataset.filter === state.status;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function listDataSignature(tasks, queue) {
    const first = tasks[0];
    const last = tasks[tasks.length - 1];
    const active = [];
    tasks.forEach((task) => {
      if (task.status === "queued" || task.status === "running") {
        active.push(`${task.id}:${task.status}:${task.stage || ""}:${task.progressPercent || 0}:${task.stageProgressPercent ?? ""}:${task.updatedAt || ""}:${task.error || ""}`);
      }
    });
    return [
      tasks.length,
      first?.id || "", first?.createdAt || "",
      last?.id || "", last?.createdAt || "",
      active.join(","),
      queue?.waitMinutes || 0, queue?.blocked ? 1 : 0, queue?.workerConcurrency || 0,
    ].join("|");
  }

  function renderList({ preserveViewport = false, stabilizeHeight = false } = {}) {
    const viewport = preserveViewport ? captureListViewport() : null;
    const previousListHeight = stabilizeHeight ? $(".task-list")?.getBoundingClientRect().height || 0 : 0;
    const filters = cfg.statusFilters.map((item) => `
      <button class="chip ${state.status === item.id ? "active" : ""}" type="button" data-filter="${item.id}" aria-pressed="${state.status === item.id}">${item.label}</button>`).join("");
    const pageSize = 10;
    const grouped = groupedTaskItems(state.tasks);
    const pageCount = Math.max(1, Math.ceil(grouped.length / pageSize));
    const page = Math.min(state.taskPage, pageCount - 1);
    if (page !== state.taskPage) state.taskPage = page;
    const pageTasks = grouped.slice(page * pageSize, (page + 1) * pageSize);
    const rows = state.tasks.length
      ? pageTasks.map((item) => item.type === "batch" ? batchRow(item) : taskRow(item.task)).join("")
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
        <div class="workspace-panel ${state.listLoading ? "is-loading" : ""}" aria-busy="${state.listLoading}">
          <p class="mobile-upload-note">手机端可查看已有任务。上传视频和新建任务请使用电脑端。</p>
          <div class="batch-download-actions"><button class="secondary-btn" type="button" data-action="download-all" data-format="md" ${state.tasks.some(isCompletedTask) ? "" : "disabled"}>下载全部剧本（MD）</button><button class="secondary-btn" type="button" data-action="download-all" data-format="txt" ${state.tasks.some(isCompletedTask) ? "" : "disabled"}>下载全部剧本（TXT）</button></div>
          ${state.queue.blocked ? `<div class="queue-blocked-notice">当前排队预计超过 10 小时，暂时不能继续上传，请稍后再试。</div>` : state.queue.waitMinutes ? `<div class="queue-summary">当前预计排队 ${formatEta(state.queue.waitMinutes)} · 可同时处理 ${state.queue.workerConcurrency || 8} 个视频</div>` : ""}
          <div class="toolbar">
            <label class="search-wrap">
              <input class="search" id="keyword" placeholder="搜索任务或文件名" value="${escapeHtml(state.keyword)}" />
              <button class="search-submit" type="button" data-action="search" aria-label="执行搜索" title="执行搜索">⌕</button>
            </label>
            <div class="filters" aria-label="任务状态筛选">${filters}</div>
            <div class="task-count">${state.tasks.length} 个任务</div>
          </div>
          <div class="task-list">${rows}</div>
          <div class="user-pagination"><button class="secondary-btn" type="button" data-action="task-page" data-page="${Math.max(0, page - 1)}" ${page === 0 ? "disabled" : ""}>上一页</button><span>第 ${page + 1} / ${pageCount} 页</span><button class="secondary-btn" type="button" data-action="task-page" data-page="${Math.min(pageCount - 1, page + 1)}" ${page >= pageCount - 1 ? "disabled" : ""}>下一页</button></div>
        </div>
      </section>`;
    const batchActions = document.querySelector(".batch-download-actions");
    if (batchActions) [...batchActions.childNodes].filter((node) => node.nodeType === Node.TEXT_NODE).forEach((node) => node.remove());
    if (previousListHeight) $(".task-list").style.minHeight = `${Math.ceil(previousListHeight)}px`;
    restoreListViewport(viewport);
  }

  function pipeline(task) {
    const mediaLabel = mediaProgressLabel(task);
    const preparing = ["source_download_wait", "source_download", "compression_queued", "compressing", "video_upload_wait", "video_upload", "video_upload_retry", "video_preparing", "evidence_wait"].includes(task.stage);
    const steps = preparing ? [cfg.pipeline[0], cfg.pipeline[1], { id: task.stage, label: cfg.stageText[task.stage] }, ...cfg.pipeline.slice(2)] : cfg.pipeline;
    const currentIndex = steps.findIndex((item) => item.id === (["video_read_retry", "repairing", "reviewing"].includes(task.stage) ? "vision" : task.stage));
    const percent = mediaLabel ? compressionPercent(task) : (task.progressPercent || 0);
    const note = preparing ? "服务器后台处理中，原片已保留，可以离开本页" : "后台处理中，可以离开本页";
    return `<div class="progress-panel">
      <div class="progress-head">
        <div><strong>${escapeHtml(cfg.stageText[task.stage] || "正在处理")}</strong><span>${note}</span></div>
        <b>${mediaLabel ? `${mediaLabel} ${percent}%` : `${percent}%`}</b>
      </div>
      <div class="main-progress" role="progressbar" aria-label="${mediaLabel === "压缩" ? "服务器压缩进度" : mediaLabel ? `${mediaLabel}进度` : "处理进度"}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div>
      <div class="pipeline">${steps.map((step, index) => {
        const className = index < currentIndex ? "done" : index === currentIndex ? "active" : "";
        return `<div class="pipeline-step ${className}"><i>${index < currentIndex ? "✓" : index + 1}</i><span>${step.label}</span></div>`;
      }).join("")}</div>
    </div>`;
  }

  function scriptPreview(script) {
    if (!script) return "";
    const sceneCounts = new Map();
    // Keep caption evidence in task data, but do not print an empty heading
    // when every block was suppressed. Match screenplay export numbering.
    const visibleScenes = (script.scenes || []).filter(scene => (scene.blocks || []).some(block =>
      !block.suppressInOutput && (String(block.text || "").trim() || block.type === "transition")
    )).map(scene => {
      const heading = String(scene.heading || "");
      const match = heading.match(/^\s*(\d+)-\d+/);
      if (!match) return scene;
      const count = (sceneCounts.get(match[1]) || 0) + 1;
      sceneCounts.set(match[1], count);
      return { ...scene, heading: heading.replace(/^\s*\d+-\d+/, `${match[1]}-${count}`) };
    });
    function blockTime(block) {
      if (!Number.isFinite(Number(block.startSec))) return "";
      const start = Math.max(0, Math.round(Number(block.startSec)));
      const end = Number.isFinite(Number(block.endSec)) ? Math.max(start, Math.round(Number(block.endSec))) : null;
      const clock = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      return `<small class="block-time">${clock(start)}${end !== null ? `–${clock(end)}` : ""}</small>`;
    }
    function renderBlock(block) {
      // The screenplay follows 剧拆拆's readable text layout; timestamps stay
      // in the underlying data for review but are not printed in the body.
      if (block.suppressInOutput) return "";
      const time = "";
      if (block.type === "dialogue") {
        const performance = block.performance ? `<em>${escapeHtml(block.performance)}</em>` : "";
        return `<p class="dialogue">${time}<strong>${escapeHtml(block.speaker || "未知说话人")}</strong>${performance}<span>：${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "vo" || block.type === "os") {
        const isOs = block.type === "os" || block.voKind === "os" || block.isInnerMonologue;
        const speaker = block.speaker && !["旁白", "未知说话人", "OS", "内心独白"].includes(block.speaker)
          ? `${escapeHtml(block.speaker)} ${isOs ? "OS" : "VO"}`
          : (isOs ? "OS" : "VO");
        return `<p class="os-line">${time}<strong>${speaker}</strong><span>：${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "sound") {
        const label = block.category === "music" ? "背景音乐" : block.category === "ambience" ? "环境声" : "音效";
        return `<p class="sound-line">${time}<strong>【${label}】</strong><span>${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "emotion") {
        return `<p class="action-line">${time}<i>▲</i>${escapeHtml(block.text || "")}</p>`;
      }
      if (block.type === "screen_text") {
        return `<p class="sound-line">${time}<strong>【字幕】</strong><span>${escapeHtml(block.text || "")}</span></p>`;
      }
      if (block.type === "transition") {
        const transitionLabel = { flashback: "闪回", return: "闪出", flash: "闪白" }[String(block.transitionType || "").toLowerCase()] || block.transitionType || "转场";
        return `<p class="sound-line">${time}<strong>【${escapeHtml(transitionLabel)}】</strong><span>${escapeHtml(block.text || "")}</span></p>`;
      }
      return `<p class="action-line">${time}<i>▲</i>${escapeHtml(block.text || "")}</p>`;
    }
    return `<div class="script-paper">
      <div class="script-title">
        <span>镜头剧本</span>
        <h2>${escapeHtml(script.title)}</h2>
      </div>
      ${visibleScenes.map((scene) => `<section class="scene">
        <h3>${escapeHtml(scene.heading)}</h3>
        ${(scene.characters || []).length ? `<p class="scene-cast">出场人物：${escapeHtml([...new Set(scene.characters || [])].join("、"))}</p>` : ""}
        <div class="scene-blocks">${(scene.blocks || []).map(renderBlock).join("")}</div>
      </section>`).join("")}
    </div>`;
  }

  function reviewIssues(task) {
    const quality = task.quality || {};
    const seen = new Set();
    return [...(Array.isArray(quality.blockingIssues) ? quality.blockingIssues : []), ...(Array.isArray(quality.issues) ? quality.issues : [])].filter((issue) => {
      if (!issue) return false;
      const key = JSON.stringify(issue);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function reviewIssuesMarkup(task) {
    const issues = reviewIssues(task);
    return `<div class="admin-review-issues">${issues.length ? issues.map(issue => `<p><b>${escapeHtml(issue.severity || "提示")}</b>${escapeHtml(issue.description || issue.tag || issue)}</p>`).join("") : "<p>暂无结构化问题提示，请核对剧本正文。</p>"}</div>`;
  }

  function openUserReview(tasks) {
    if (!tasks.length) { toast("没有待复核的剧本，请刷新任务列表"); return; }
    state.reviewIds = tasks.map(task => task.id);
    $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask"><div class="modal admin-review-modal" role="dialog" aria-modal="true" aria-labelledby="userReviewTitle"><div class="modal-head"><h2 id="userReviewTitle">${tasks.length > 1 ? "批量复核" : "确认复核"}</h2><button class="close-btn" data-action="close-modal" aria-label="关闭">×</button></div><p class="quality-note quality-warning">请核对以下 ${tasks.length} 集剧本及问题提示。确认后即可下载，无需管理员复核。</p>${tasks.map(task => `<section class="review-item"><strong>${escapeHtml(task.title || task.fileName || task.id)}</strong>${reviewIssuesMarkup(task)}<details><summary>查看本集剧本</summary>${scriptPreview(task.result)}</details></section>`).join("")}<p class="quality-note">空剧本不能确认通过。如有一集无法通过，本次批量确认不会提交任何一集。</p><div class="modal-actions"><button class="primary-btn" data-action="confirm-user-review">确认通过并开放下载（${tasks.length}集）</button><button class="cancel-btn" data-action="close-modal">取消</button></div></div></div>`;
  }

  function renderResult(task) {
    if (!task.result) return `<div class="empty-result"><strong>剧本内容暂未生成</strong><span>任务虽然已完成，但当前没有可展示的剧本内容，请稍后刷新或重新识别。</span></div>`;
    return `<div class="result-layout">
      <aside class="result-aside">
        <div class="aside-section">
          <span class="aside-label">原始视频</span>
          <strong class="aside-file">${escapeHtml(task.fileName)}</strong>
          <dl class="meta-list">
            <div><dt>视频时长</dt><dd>${formatDuration(task.durationSec)}</dd></div>
            <div><dt>消耗积分</dt><dd>${(Number(task.creditsUsed ?? task.estimatedMinutes ?? 0) * POINTS_PER_MINUTE).toFixed(1)} 积分</dd></div>
            <div><dt>完成时间</dt><dd>${formatDate(task.completedAt || task.createdAt)}</dd></div>
          </dl>
        </div>
        <div class="aside-section quality-section">
          <span class="aside-label">内容检查提示</span>
          <p class="quality-note">建议对照原视频核对人名、台词及情节内容。</p>
          ${task.status === "review" ? `<p class="quality-note quality-warning">请核对正文和以下提示，复核确认后即可下载。</p>${reviewIssuesMarkup(task)}` : ""}
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
        const label = event.status === "review" || event.type === "review_required"
          ? "待用户复核"
          : event.stage ? (cfg.stageText[event.stage] || event.stage) : (cfg.statusText[event.status] || event.type);
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
    const downloadActions = isCompletedTask(task) ? `
      <div class="download-actions">
        <button class="secondary-btn" type="button" data-action="download" data-id="${task.id}" data-format="md">下载 MD</button>
        <button class="secondary-btn" type="button" data-action="download" data-id="${task.id}" data-format="txt">下载 TXT</button>
        <button class="secondary-btn disabled" type="button" disabled title="正式版开放">Word <small>稍后</small></button>
      </div>` : task.status === "review" ? `<button class="primary-btn" data-action="review-task" data-id="${task.id}">确认复核并开放下载</button>` : "";
    let content = "";
    if (isCompletedTask(task) || task.status === "review") content = renderResult(task);
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
    return Number(state.upload.estimatedMinutes || 0);
  }

  function selectedFilesMarkup(upload) {
    return `<div class="selected-files-list"><div class="selected-files-head"><strong>已选择 ${upload.files.length} 个视频</strong><span>将分别创建任务</span></div>${upload.files.map((file, index) => `<div class="selected-file-row"><span>${index + 1}</span><strong>${escapeHtml(file.name)}</strong><small>${formatSize(file.size)}</small><button type="button" data-action="remove-file" data-index="${index}">移除</button></div>`).join("")}</div>`;
  }

  function createModalMarkup() {
    const upload = state.upload;
    const estimate = uploadEstimate();
    const credits = state.profile ? state.profile.credits : 0;
    // The six-minute limit applies to each file, never to the batch total.
    // setUploadFiles validates every probed duration independently.
    const overDuration = false;
    const estimatedPoints = estimate * POINTS_PER_MINUTE;
    const insufficient = estimatedPoints > credits;
    const durationSummary = upload.reading
      ? "正在读取视频总时长…"
      : upload.durationSec
        ? `视频总时长 ${formatDuration(upload.durationSec)}`
        : "";
    const queueBlocked = Boolean(state.queue.blocked);
    const canSubmit = upload.files.length && upload.durationSec && !upload.reading && !upload.error && !overDuration && !insufficient && !queueBlocked;
    const fileBlock = upload.files.length ? `<div class="selected-file">
      <div class="file-thumb"><span></span></div>
      <div><strong>${escapeHtml(upload.file.name)}</strong><p>${formatSize(upload.file.size)} · ${upload.reading ? "正在读取时长..." : formatDuration(upload.durationSec)}</p></div>
      <button type="button" data-action="remove-file" aria-label="移除视频">×</button>
    </div>` : `<div class="drop-content">
      <div class="upload-icon">↑</div>
      <strong>拖入视频，或点击选择文件</strong>
      <span>支持选择 1-999 个 MP4 视频，单个视频不超过 6 分钟、最大 500MB</span>
      <small>可一次选择多个文件，也可以拖入整批视频</small>
    </div>`;
    let billing = "选择视频后自动读取时长并预估积分";
    if (queueBlocked) billing = "当前排队预计超过 10 小时，暂时不能继续上传";
    else if (upload.error) billing = upload.error;
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
            ${upload.files.length ? selectedFilesMarkup(upload) : fileBlock}
            <input type="file" id="fileInput" accept=".mp4,video/mp4" multiple hidden />
          </div>
        </div>
        <div class="billing-note ${upload.error || overDuration || insufficient ? "warning" : ""}">
          <span>${upload.error || overDuration || insufficient ? "!" : "i"}</span>
          <div><strong>${billing}</strong><p>${durationSummary ? `${durationSummary} · ` : ""}按本批视频总时长向上取整预扣；任务失败自动退回。</p></div>
        </div>
        <div class="modal-actions">
          <button class="cancel-btn" type="button" data-action="close-modal">取消</button>
          <button class="primary-btn" type="button" data-action="submit-task" ${canSubmit ? "" : "disabled"}>开始识别</button>
        </div>
      </div>
    </div>`;
  }

  function openCreateModal() {
    state.upload = { files: [], file: null, durationSec: 0, estimatedMinutes: 0, reading: false, error: "" };
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

  const videoDurationCache = new WeakMap();

  function readVideoDuration(file) {
    const cached = videoDurationCache.get(file);
    if (cached) return cached;
    const pending = new Promise((resolve, reject) => {
      const video = document.createElement("video");
      const url = URL.createObjectURL(file);
      let finished = false;
      const finish = (error) => {
        if (finished) return;
        finished = true;
        const duration = video.duration;
        clearTimeout(timer);
        video.onloadedmetadata = null;
        video.onerror = null;
        video.removeAttribute("src");
        video.load();
        URL.revokeObjectURL(url);
        error ? reject(error) : resolve(duration);
      };
      const timer = setTimeout(() => finish(new Error(`${file.name} 读取视频时长超时`)), 30000);
      video.preload = "metadata";
      video.onloadedmetadata = () => {
        finish(Number.isFinite(video.duration) && video.duration > 0 ? null : new Error("无法读取视频时长"));
      };
      video.onerror = () => {
        finish(new Error("视频无法读取，请确认文件未损坏"));
      };
      video.src = url;
    });
    videoDurationCache.set(file, pending);
    pending.catch(() => videoDurationCache.delete(file));
    return pending;
  }

  async function readVideoDurations(files, concurrency = 4) {
    const durations = new Array(files.length);
    let cursor = 0;
    const worker = async () => {
      while (cursor < files.length) {
        const index = cursor++;
        durations[index] = await readVideoDuration(files[index]);
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, files.length) }, worker));
    return durations;
  }

  async function setUploadFile(file) {
    if (!file) return;
    const titleValue = $("#taskTitle") ? $("#taskTitle").value : "";
    const maxBytes = cfg.upload.maxSizeMB * 1024 * 1024;
    if (!/\.mp4$/i.test(file.name)) {
      state.upload = { file: null, durationSec: 0, estimatedMinutes: 0, reading: false, error: "目前只支持 MP4 视频" };
      rerenderCreateModal(titleValue);
      return;
    }
    if (file.size > maxBytes) {
      state.upload = { file: null, durationSec: 0, estimatedMinutes: 0, reading: false, error: `文件超过 ${cfg.upload.maxSizeMB} MB` };
      rerenderCreateModal(titleValue);
      return;
    }
    state.upload = { file, durationSec: 0, estimatedMinutes: 0, reading: true, error: "" };
    rerenderCreateModal(titleValue);
    try {
      state.upload.durationSec = await readVideoDuration(file);
      state.upload.estimatedMinutes = estimatedMinutes(state.upload.durationSec);
    } catch (error) {
      state.upload.error = error.message;
    }
    state.upload.reading = false;
    rerenderCreateModal(titleValue);
  }

  async function setUploadFiles(fileList, options = {}) {
    const incoming = Array.from(fileList || []);
    // Keep every selected file. Some browsers emit a change event with one
    // file when the picker is used repeatedly, so merge by file identity
    // instead of silently replacing the existing selection.
    const existing = options.replace ? [] : (Array.isArray(state.upload.files) ? state.upload.files : []);
    const files = [...existing, ...incoming].filter((file, index, all) =>
      all.findIndex((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified) === index
    );
    const titleValue = $("#taskTitle") ? $("#taskTitle").value : "";
    const maxBytes = cfg.upload.maxSizeMB * 1024 * 1024;
    if (!files.length) return;
    if (files.length > cfg.upload.maxFiles) { state.upload = { files: existing, file: existing[0] || null, durationSec: state.upload.durationSec || 0, estimatedMinutes: state.upload.estimatedMinutes || 0, reading: false, error: `最多选择 ${cfg.upload.maxFiles} 个视频` }; rerenderCreateModal(titleValue); return; }
    const invalid = files.find((file) => !/\.mp4$/i.test(file.name) || file.size > maxBytes);
    if (invalid) { state.upload = { files: [], file: null, durationSec: 0, estimatedMinutes: 0, reading: false, error: !/\.mp4$/i.test(invalid.name) ? "目前只支持 MP4 视频" : `${invalid.name} 超过 ${cfg.upload.maxSizeMB} MB` }; rerenderCreateModal(titleValue); return; }
    const upload = { files, file: files[0], durationSec: 0, estimatedMinutes: 0, reading: true, error: "" };
    state.upload = upload;
    rerenderCreateModal(titleValue);
    try {
      // Reading metadata for 100+ videos at once creates one decoder and one
      // object URL per file. Keep browser pressure bounded while preserving
      // the original selection order for billing and upload mapping.
      const durations = await readVideoDurations(files, 4);
      if (state.upload !== upload) return;
      state.upload.durations = durations;
      state.upload.durationSec = durations.reduce((sum, value) => sum + value, 0);
      // Bill the selected batch by its aggregate duration, rounded once. This
      // must match the backend, otherwise every short episode is overcharged.
      state.upload.estimatedMinutes = estimatedMinutes(state.upload.durationSec);
      if (durations.some((value) => value > cfg.upload.maxDurationMinutes * 60)) state.upload.error = `单个视频不能超过 ${cfg.upload.maxDurationMinutes} 分钟`;
    } catch (error) {
      if (state.upload !== upload) return;
      state.upload.error = error.message;
    }
    state.upload.reading = false;
    rerenderCreateModal(titleValue);
  }

  async function refreshList({ source = "user" } = {}) {
    const requestId = ++state.listRequestId;
    const query = { keyword: state.keyword, status: state.status };
    const previousSignature = listDataSignature(state.tasks, state.queue);
    if (source !== "poll") setListLoading(true);
    try {
      const [tasks, queue] = await Promise.all([
        api.listTasks(query),
        source === "filter" ? Promise.resolve(state.queue) : api.getQueueSummary().catch(() => state.queue),
      ]);
      if (requestId !== state.listRequestId || state.view !== "list" || query.keyword !== state.keyword || query.status !== state.status) return;
      state.tasks = tasks.map(normalizeUserTask);
      state.queue = queue || state.queue;
      const rateLimitTasks = source === "poll" ? state.tasks.slice(0, 50) : state.tasks;
      const limited = rateLimitTasks.some((task) => /429|ratelimit|setlimit|限流|请求较多/i.test(String(task.error || "")));
      if (limited && !state.rateLimitNotified) { state.rateLimitNotified = true; toast("当前处理请求较多，任务已自动重试，请稍后查看", "warning"); }
      if (!limited) state.rateLimitNotified = false;
      const dataChanged = listDataSignature(state.tasks, state.queue) !== previousSignature;
      if (source !== "poll" || dataChanged || !$(".workspace-panel")) {
        renderList({
          preserveViewport: Boolean($(".workspace-panel")),
          stabilizeHeight: source === "poll",
        });
      }
    } catch (error) {
      if (requestId === state.listRequestId) toast(error.message || "任务列表加载失败", "error");
    } finally {
      if (requestId === state.listRequestId) {
        setListLoading(false);
        schedulePoll(state.tasks.some((task) => task.status === "queued" || task.status === "running"));
      }
    }
  }

  async function refreshDetail() {
    try {
      state.current = normalizeUserTask(await api.getTask(state.taskId));
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
      else refreshList({ source: "poll" });
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
          hardRefreshApp();
        }
      }
      catch (error) { state.authBusy = false; renderAuth(); toast(error.message || (state.authMode === "forgot" ? "密码重置失败" : "注册失败"), "error"); }
      return;
    }
    state.authBusy = true; renderAuth();
    try { state.profile = await api.login(email, password); toast("登录成功"); hardRefreshApp(); }
    catch (error) { state.authBusy = false; renderAuth(); toast(error.message || "登录失败", "error"); }
  }

  async function logout() {
    try { await api.logout(); } catch (_) {}
    clearInterval(state.authCooldownTimer);
    state.authCooldownTimer = null;
    state.authCooldown = 0;
    state.profile = null; state.authBusy = false; state.authMode = "login"; state.authEmail = "";
    state.expandedBatches = {};
    hardRefreshApp();
    $("#modalRoot").innerHTML = ""; renderAuth(); toast("已退出登录");
  }

  // Always rebuild the app after an auth boundary. This prevents the browser
  // bfcache or a stale in-memory bundle from restoring the pre-login layout.
  function hardRefreshApp() {
    const url = new URL(window.location.href);
    url.searchParams.set("ui", String(Date.now()));
    url.hash = "#/tasks";
    window.location.replace(url.toString());
  }

  document.addEventListener("click", async (event) => {
    const actionElement = event.target.closest("[data-action]");
    const row = event.target.closest("[data-open]");
    const filter = event.target.closest("[data-filter]");

    if (filter) {
      const nextStatus = filter.dataset.filter;
      if (nextStatus === state.status) return;
      state.status = nextStatus;
      state.taskPage = 0;
      updateActiveFilter();
      setListLoading(true);
      await refreshList({ source: "filter" });
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
    if (action === "review-task" || action === "review-batch") {
      if (actionElement.dataset.busy === "1") return;
      actionElement.dataset.busy = "1";
      actionElement.disabled = true;
      try {
        const ids = action === "review-task" ? [id] : state.tasks.filter(task => task.batchId === id && task.status === "review").map(task => task.id);
        if (ids.length > 999) throw new Error("单次最多复核 999 集");
        const tasks = [];
        for (let offset = 0; offset < ids.length; offset += 6) {
          tasks.push(...await Promise.all(ids.slice(offset, offset + 6).map(taskId => api.getTask(taskId))));
        }
        openUserReview(tasks.filter(task => task.status === "review"));
      } catch (error) { toast(error.message || "复核内容加载失败", "error"); }
      finally { delete actionElement.dataset.busy; actionElement.disabled = false; }
      return;
    }
    if (action === "confirm-user-review") {
      if (actionElement.dataset.busy === "1" || !state.reviewIds.length) return;
      const ids = state.reviewIds.slice();
      const buttons = Array.from(actionElement.closest(".modal")?.querySelectorAll("button") || []);
      const label = actionElement.textContent;
      actionElement.dataset.busy = "1";
      buttons.forEach(button => { button.disabled = true; });
      actionElement.textContent = "正在确认…";
      try {
        if (ids.length === 1) await api.approveTask(ids[0]);
        else await api.approveTasks(ids);
        state.reviewIds = [];
        $("#modalRoot").innerHTML = "";
        toast("复核完成，可以下载");
        if (state.view === "detail") await refreshDetail();
        else await refreshList();
      } catch (error) {
        delete actionElement.dataset.busy;
        buttons.forEach(button => { button.disabled = false; });
        actionElement.textContent = label;
        toast(error.message || "复核确认失败", "error");
      }
      return;
    }
    if (action === "admin-tab") {
      state.adminTab = actionElement.dataset.tab;
      document.querySelectorAll(".admin-tab").forEach((button) => button.classList.toggle("active", button === actionElement));
      await renderAdmin();
      return;
    }
    if (action === "admin-refresh") { state.adminOverviewCache = null; state.adminOverviewCachedAt = 0; state.adminTaskUsersCache = null; await renderAdmin(); return; }
    if (action === "admin-search") {
      const from = $("#adminTaskFrom")?.value || "", to = $("#adminTaskTo")?.value || "";
      if (!adminDateRangeValid(from, to)) return;
      state.adminTaskKeyword = $("#adminTaskKeyword")?.value.trim() || ""; state.adminTaskStatus = $("#adminTaskStatus")?.value || "all"; state.adminTaskUser = $("#adminTaskUser")?.value || ""; state.adminTaskFrom = from; state.adminTaskTo = to; state.adminTaskPage = 0; await renderAdmin(); return;
    }
    if (action === "admin-task-reset") { state.adminTaskKeyword = ""; state.adminTaskStatus = "all"; state.adminTaskUser = ""; state.adminTaskFrom = ""; state.adminTaskTo = ""; state.adminTaskPage = 0; await renderAdmin(); return; }
    if (action === "admin-page") { state.adminTaskPage = Number(actionElement.dataset.page) || 0; await renderAdmin(); return; }
    if (action === "admin-user-search") { state.adminUserKeyword = $("#adminUserKeyword")?.value.trim() || ""; state.adminUserStatus = $("#adminUserStatus")?.value || "all"; state.adminUserPage = 0; await renderAdmin(); return; }
    if (action === "admin-user-reset") { state.adminUserKeyword = ""; state.adminUserStatus = "all"; state.adminUserPage = 0; await renderAdmin(); return; }
    if (action === "admin-user-page") { state.adminUserPage = Number(actionElement.dataset.page) || 0; await renderAdmin(); return; }
    if (action === "admin-recharge-search") {
      const from = $("#adminRechargeFrom")?.value || "", to = $("#adminRechargeTo")?.value || "";
      if (!adminDateRangeValid(from, to)) return;
      state.adminRechargeType = $("#adminRechargeType")?.value || "all"; state.adminRechargeKeyword = $("#adminRechargeKeyword")?.value.trim() || ""; state.adminRechargeFrom = from; state.adminRechargeTo = to; state.adminRechargePage = 0; await renderAdmin(); return;
    }
    if (action === "admin-recharge-reset") { state.adminRechargeType = "all"; state.adminRechargeKeyword = ""; state.adminRechargeFrom = ""; state.adminRechargeTo = ""; state.adminRechargePage = 0; await renderAdmin(); return; }
    if (action === "admin-recharge-page") { state.adminRechargePage = Number(actionElement.dataset.page) || 0; await renderAdmin(); return; }
    if (action === "admin-credit") {
      const rmbText = window.prompt("输入充值人民币金额（按 1 元 = 12.8 积分自动换算）", "100");
      if (rmbText === null) return;
      const rmbAmount = Number(rmbText);
      if (!Number.isFinite(rmbAmount) || rmbAmount <= 0) { toast("请输入有效人民币金额", "error"); return; }
      const pointsAmount = (rmbAmount * 12.8).toFixed(1);
      const rechargeReason = window.prompt(`本次将增加 ${pointsAmount} 积分，填写充值备注`, "管理员人民币充值");
      if (rechargeReason === null) return;
      try { await api.adjustAdminCredits(id, { rmb_amount: rmbAmount, reason: rechargeReason }); state.adminOverviewCache = null; state.adminOverviewCachedAt = 0; state.adminTaskUsersCache = null; toast(`充值成功，已增加 ${pointsAmount} 积分`); await renderAdmin(); } catch (error) { toast(error.message || "充值失败", "error"); }
      return;
    }
    if (action === "admin-status") {
      if (actionElement.dataset.active === "0" && !window.confirm("停用后，该用户将退出登录且无法继续登录。确定停用该账号？")) return;
      try { await api.setAdminUserStatus(id, actionElement.dataset.active === "1"); state.adminOverviewCache = null; state.adminOverviewCachedAt = 0; state.adminTaskUsersCache = null; toast("账号状态已更新"); await renderAdmin(); } catch (error) { toast(error.message || "状态更新失败", "error"); }
      return;
    }
    if (action === "admin-retry") {
      try { await api.retryAdminTask(id); state.adminOverviewCache = null; state.adminOverviewCachedAt = 0; toast("任务已重新排队"); await renderAdmin(); } catch (error) { toast(error.message || "任务重试失败", "error"); }
      return;
    }
    if (action === "admin-review") {
      if (actionElement.dataset.busy === "1") return;
      const reviewButtonLabel = actionElement.textContent;
      actionElement.dataset.busy = "1";
      actionElement.disabled = true;
      actionElement.textContent = "正在加载…";
      try {
        const task = await api.getAdminTask(id);
        const evidence = task.evidence || {};
        const evidenceSources = evidence.sources && Array.isArray(evidence.sources)
          ? evidence.sources.map((source) => `${source.kind || "unknown"}:${source.status || "unavailable"}`).join(" · ")
          : "未记录";
        const audioSource = Array.isArray(evidence.sources) ? evidence.sources.find((source) => source.kind === "audio") : null;
        const transcriptItem = audioSource && Array.isArray(audioSource.items) ? audioSource.items.find((item) => item.type === "transcript") : null;
        const evidenceLabel = `独立证据来源：${evidenceSources}${transcriptItem && transcriptItem.text ? `；音频转写：${String(transcriptItem.text).slice(0, 280)}` : ""}`;
        const restoreButton = task.recheck && task.recheck.pending ? `<button class="secondary-btn" type="button" data-action="admin-review-restore" data-id="${task.id}">恢复上次已完成版本</button>` : "";
        const episodes = Array.isArray(task.episodes) ? task.episodes : [];
        const episodeLinks = episodes.length > 1 ? `<nav class="review-episodes" aria-label="选择剧集">${episodes.map(episode => `<button class="${episode.id === task.id ? "primary-btn" : "secondary-btn"}" data-action="admin-review" data-id="${episode.id}" ${episode.id === task.id ? "disabled" : ""}>${escapeHtml(episode.fileName || episode.title)} · ${escapeHtml(cfg.statusText[episode.status] || episode.status)}</button>`).join("")}</nav>` : "";
        $("#modalRoot").innerHTML = `<div class="modal-mask" id="modalMask"><div class="modal admin-review-modal" role="dialog" aria-modal="true" aria-labelledby="adminTaskDetailTitle"><div class="modal-head"><h2 id="adminTaskDetailTitle">任务详情</h2><button class="close-btn" type="button" data-action="close-modal" aria-label="关闭">×</button></div>${episodeLinks}<div class="admin-review-meta"><strong>${escapeHtml(task.title || task.id)}</strong>${statusBadge(task)}<span>${escapeHtml(task.fileName || "")}</span></div><p class="quality-note">时长 ${formatDuration(task.durationSec)} · 消耗 ${Number(task.creditsUsed ?? task.estimatedMinutes ?? 0).toFixed(1)} 分钟 · 进度 ${Number(task.progressPercent) || 0}% · 创建 ${formatDate(task.createdAt)}</p>${task.error ? `<p class="quality-note quality-warning">${escapeHtml(task.error)}</p>` : ""}${task.status === "review" ? `<p class="quality-note quality-warning">等待用户自行复核确认，确认后用户即可下载。</p>` : ""}${reviewIssuesMarkup(task)}<p class="quality-note">${escapeHtml(evidenceLabel)}</p><div class="admin-review-script">${task.result ? scriptPreview(task.result) : "<p>暂无剧本正文。</p>"}</div>${renderTaskTimeline(task)}<div class="modal-actions">${restoreButton}<button class="cancel-btn" type="button" data-action="close-modal">关闭</button></div></div></div>`;
      } catch (error) { toast(error.message || "任务详情加载失败", "error"); }
      finally {
        if (actionElement.isConnected) {
          delete actionElement.dataset.busy;
          actionElement.disabled = false;
          actionElement.textContent = reviewButtonLabel;
        }
      }
      return;
    }
    if (action === "admin-review-restore") {
      if (!window.confirm("放弃本次复核结果并恢复上次已完成版本吗？")) return;
      try { await api.restoreAdminTaskRecheck(id); state.adminOverviewCache = null; state.adminOverviewCachedAt = 0; $("#modalRoot").innerHTML = ""; toast("已恢复上次已完成版本"); await renderAdmin(); } catch (error) { toast(error.message || "恢复旧版本失败", "error"); }
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
    if (action === "toggle-batch") { const batchId = actionElement.dataset.id; state.expandedBatches[batchId] = !state.expandedBatches[batchId]; renderList(); return; }
    if (action === "search") { state.keyword = $("#keyword")?.value.trim() || ""; state.taskPage = 0; await refreshList(); return; }
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
    if (action === "create") { if (mobileUploadDisabled()) { toast("上传视频和新建任务请使用电脑端", "warning"); return; } if (state.queue.blocked) { toast("当前排队预计超过 10 小时，请稍后再上传", "warning"); return; } openCreateModal(); }
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
    if (action === "download-batch") {
      try {
        const payload = await api.getExportBatch(id, actionElement.dataset.format || "md");
        const url = URL.createObjectURL(payload.blob); const link = document.createElement("a"); link.href = url; link.download = payload.fileName; link.click(); URL.revokeObjectURL(url);
      } catch (error) { toast(error.message || "下载失败", "error"); }
    }
    if (action === "download-all") {
      try {
        const payload = await api.getExportAll(actionElement.dataset.format || "md");
        const url = URL.createObjectURL(payload.blob); const link = document.createElement("a"); link.href = url; link.download = payload.fileName; link.click(); URL.revokeObjectURL(url);
      } catch (error) { toast(error.message || "下载失败", "error"); }
    }
    if (action === "remove-file") {
      const titleValue = $("#taskTitle") ? $("#taskTitle").value : "";
      const index = Number(actionElement.dataset.index);
      if (Number.isInteger(index) && state.upload.files.length > 1) {
        const remaining = state.upload.files.filter((_, itemIndex) => itemIndex !== index);
        await setUploadFiles(remaining, { replace: true });
        return;
      }
      state.upload = { files: [], file: null, durationSec: 0, estimatedMinutes: 0, reading: false, error: "" };
      rerenderCreateModal(titleValue);
    }
    if (action === "submit-task") {
      if (mobileUploadDisabled()) { toast("上传视频和新建任务请使用电脑端", "warning"); return; }
      const files = state.upload.files || (state.upload.file ? [state.upload.file] : []);
      const title = $("#taskTitle") ? $("#taskTitle").value.trim() : "";
      if (!files.length || !state.upload.durationSec) return;
      actionElement.disabled = true;
      actionElement.textContent = "正在创建...";
      try {
        const task = await api.createTask({
          title,
          files,
          durations: state.upload.durations || [],
          durationSec: Math.round(state.upload.durationSec),
          estimatedMinutes: uploadEstimate(),
          onProgress: (completed, total) => {
            actionElement.textContent = `正在上传 ${completed}/${total}`;
          },
        });
        state.profile = await api.getProfile();
        renderHeader();
        $("#modalRoot").innerHTML = "";
        toast("任务已创建，正在后台识别");
        go("tasks");
      } catch (error) {
        if (error.partialSuccess) {
          state.profile = await api.getProfile().catch(() => state.profile);
          renderHeader();
          $("#modalRoot").innerHTML = "";
          toast(error.message || "部分视频上传失败", "warning");
          go("tasks");
          return;
        }
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

  document.addEventListener("input", (event) => {
    if (event.target.id !== "keyword") return;
    state.keyword = event.target.value.trim();
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
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) window.location.reload();
  });

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
