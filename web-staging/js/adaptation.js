(() => {
  const api = window.AppAPI;
  const main = () => document.getElementById("main");
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
  const kinds = {
    analyze: "正在分析原稿并整理改编建议", preview: "正在试改这一场",
    plan: "正在整理分集大纲", episode: "正在写正文并检查本集",
    impact: "正在核对受影响的集数", review_all: "正在检查整剧衔接"
  };
  const keepKinds = { character: "人物", plot: "主线", ending: "结局", line: "台词" };
  let availability, view = 0, timer, project, selected = "", busy = false, planNotes = "";
  let selectedDirection = "", directionNotes = "", keepNotes = "";
  let refreshBalance = async () => {};
  const mobileReadOnly = () => window.matchMedia?.("(max-width: 780px)").matches === true;
  const sourceDrafts = new Map();
  const episodeNotes = new Map();
  const sourceText = value => value.replace(/\r\n?/g, "\n").trim();
  const projectPoints = p => p?.billing?.projectPoints ?? availability?.projectPoints ?? 20;
  const episodePoints = p => p.billing?.episodePoints ?? 20;
  const rewritePoints = p => p.billing?.rewritePoints ?? 8;
  const extraCost = p => ` · ${rewritePoints(p)} 积分`;

  const idPath = () => "/" + encodeURIComponent(project.id);
  function leave() { view += 1; clearTimeout(timer); busy = false; }
  function go(path) { location.hash = "#/adaptation" + path; }
  function error(message) {
    const node = main().querySelector(".adapt-error");
    if (node) { node.textContent = message || "操作失败，请重试。"; node.hidden = false; }
  }
  function layout(title, subtitle, body, isProjectList = false) {
    const backHref = isProjectList ? "#/workspace" : "#/adaptation";
    const backLabel = isProjectList ? "返回工作台" : "返回改编项目";
    return `<section class="page adapt-page"><a class="back-btn adapt-back" href="${backHref}"><span aria-hidden="true">←</span> ${backLabel}</a>
      <header class="adapt-title"><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></header>
      <p class="error-panel adapt-error" role="alert" hidden></p>${body}</section>`;
  }
  const sourceOptions = (sources, id) => sources.map(item =>
    `<option value="${esc(item.id)}" ${item.id === id ? "selected" : ""}>${esc(item.label)}</option>`).join("");
  function renderList(items, formatUpdatedAt) {
    main().innerHTML = layout("剧本改编", "从已有剧本开始，先试改一场，再确认大纲和分集正文。", `
      ${mobileReadOnly() ? '<p class="adapt-readonly">手机端支持浏览改编项目，编辑和生成请使用电脑。</p>' : '<div class="adapt-toolbar"><a class="primary-btn" href="#/adaptation/new">上传剧本</a><a class="secondary-btn" href="#/tasks">从平台任务选择</a></div>'}
      <div class="adapt-panel"><h2>我的改编项目</h2>${items.length ? `<ul class="adapt-projects">${items.map(item => `
        <li><a href="#/adaptation/${encodeURIComponent(item.id)}">${esc(item.title)}</a><time datetime="${esc(item.updatedAt)}">${esc(formatUpdatedAt(item.updatedAt))}</time></li>`).join("")}</ul>` : "<p>还没有改编项目。</p>"}</div>`, true);
  }
  function renderNew(task) {
    if (mobileReadOnly()) {
      main().innerHTML = layout("新建改编", "手机端支持浏览，请在电脑端创建改编项目。", '<a href="#/adaptation">查看已有项目</a>');
      return;
    }
    const fromTask = Boolean(task);
    main().innerHTML = layout("新建改编", fromTask ? `使用剧编编出稿《${task.batchTitle || task.title}》。` : "上传自己的剧本，也可以直接粘贴正文。", `
      <form class="adapt-panel adapt-form" data-adapt-form="create" data-task="${esc(task?.id || "")}">
        ${fromTask ? "" : `<p class="adapt-upload-notice" role="note">正文超过 60,000 字符，请拆成几份再上传</p>`}
        ${fromTask ? "" : `<label>上传剧本 <input type="file" data-adapt-file accept=".docx,.txt,.md"><small>DOCX、TXT 或 Markdown，最大 2 MB。其他格式可复制正文。</small></label>
        <label>原稿正文 <textarea name="source_text" rows="8" minlength="20" maxlength="60000" required placeholder="上传后可在这里检查全文，或直接粘贴剧本"></textarea></label>`}
        <label>项目名（选填）<input name="title" maxlength="100" value="${esc(task?.batchTitle || task?.title || "")}" placeholder="AI 会根据原稿推荐名称"></label>
        <label>目标集数 <input name="episode_count" type="number" min="1" max="30" value="${Math.min(30, task?.batchTotal || 3)}" required></label>
        <p class="adapt-note">导入后由 AI 阅读原稿并提供改编方向。原稿分析 ${projectPoints()} 积分，含第 1 集正文和审稿。</p>
        <button class="primary-btn" type="submit">开始改编项目</button>
      </form>`);
  }
  function screenplay(scenes, cast, episode) {
    const names = new Map(cast.map(item => [item.id, item.name]));
    return scenes.map((scene, index) => [`${episode}-${index + 1} ${scene.setting}`,
      ...scene.blocks.map(block => block.speaker_id ? `${names.get(block.speaker_id)}：${block.text}` : `△ ${block.text}`)].join("\n")).join("\n\n");
  }
  function currentSource() {
    return project.sources.find(item => item.id === selected)
      || project.sources.find(item => item.id === project.analysis?.preview_source_id)
      || project.sources.find(item => item.text.includes("\n"))
      || project.sources[0];
  }
  function feedback(p, kind, id = "") {
    const issue = p.error && (p.errorKind === kind || (!p.errorKind && kind === "preview"));
    const running = p.job === kind;
    const matches = kind === "preview" && p.preview?.source_id === id;
    return `<div class="adapt-feedback" data-adapt-feedback="${kind}">
      <p class="adapt-inline-error" role="alert" ${issue ? "" : "hidden"}>${issue ? esc(p.error) : ""}</p>
      <p class="adapt-inline-status" role="status" ${running || matches && !issue ? "" : "hidden"}>${running ? esc(kinds[kind] + "，完成后会自动显示结果。") :
        matches ? '试改已完成。 <a href="#adapt-preview" data-adapt="show-preview">查看试改稿</a>' : ""}</p>
    </div>`;
  }
  function feedbackText(element, message, failed = false, isJob = false) {
    const panel = element?.closest(".adapt-panel");
    const target = isJob ? panel?.querySelector(`[data-adapt-feedback="${element.dataset.adapt}"]`) : null;
    if (!target) {
      if (failed) error(message);
      return;
    }
    for (const node of target.querySelectorAll("p")) node.hidden = true;
    if (!message) return;
    const node = target.querySelector(failed ? ".adapt-inline-error" : ".adapt-inline-status");
    node.textContent = message;
    node.hidden = false;
  }
  function renderSource(p) {
    const source = currentSource();
    const hint = source.start_sec == null ? "" : ` · 原片约 ${Math.floor(source.start_sec / 60)}:${String(Math.floor(source.start_sec % 60)).padStart(2, "0")}`;
    return `<section class="adapt-panel adapt-source-panel"><div class="adapt-section-heading"><span class="adapt-step">01</span><div><h2>原稿</h2><p>保留原文作为后续分析、改编和核对的依据。</p></div></div>
      ${p.warnings.length ? `<details class="adapt-warnings"><summary>平台原稿有 ${p.warnings.length} 条重点核对提示</summary><ul>${p.warnings.map(item => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}
      <label class="adapt-label">原稿场次 <select data-adapt-source>${sourceOptions(p.sources, source.id)}</select></label>
      <div class="adapt-note">${esc(source.label + hint)} ${source.task_id ? `<a href="#/task/${encodeURIComponent(source.task_id)}">查看平台原稿</a>` : ""}</div>
      ${p.analysis ? `<details><summary>查看这段原稿</summary><pre>${esc(source.text)}</pre></details>` : `<form data-adapt-form="source" class="adapt-form">
        <textarea name="text" rows="7" maxlength="12000" required aria-label="原稿修订副本">${esc(sourceDrafts.get(source.id) ?? source.text)}</textarea>
        ${source.originalText ? `<details><summary>查看导入时的原文</summary><pre>${esc(source.originalText)}</pre></details>` : ""}
        <div class="adapt-toolbar"><button class="secondary-btn" type="submit">保存原稿修订</button>
          <button class="primary-btn" type="button" data-adapt="analyze">${p.job === "analyze" ? "正在分析…" : `分析原稿 · ${projectPoints(p)} 积分`}</button></div>
        <p class="adapt-note">原稿分析含第 1 集首稿和审稿。单独试改、生成大纲等 AI 操作每次 ${rewritePoints(p)} 积分。失败会退回预扣积分。</p>
        ${feedback(p, "analyze")}
      </form>`}</section>`;
  }
  function renderAnalysis(p) {
    const analysis = p.analysis;
    if (!analysis) return "";
    const source = currentSource();
    const direction = p.analysis.directions.find(item => item.id === (selectedDirection || p.selectedDirectionId))
      || p.analysis.directions[0];
    const selectedKeeps = new Set(Array.isArray(p.selectedKeepOptionIds)
      ? p.selectedKeepOptionIds : p.analysis.keep_options.map(item => item.id));
    return `<section class="adapt-panel adapt-analysis-panel"><div class="adapt-section-heading"><span class="adapt-step">02</span><div><h2 tabindex="-1">选择改编方向</h2><p>建议基于原稿生成，可直接采用，也可以补充自己的要求。</p></div></div>
      <div class="adapt-analysis-summary"><h3>${esc(analysis.title)}</h3><p>${esc(analysis.summary)}</p>
        <div class="adapt-character-list">${analysis.characters.map(item => `<span>${esc(item)}</span>`).join("")}</div></div>
      <fieldset class="adapt-choice-group"><legend>改编方向</legend><div class="adapt-direction-grid">${analysis.directions.map(item => `
        <label class="adapt-direction-card ${item.id === direction.id ? "is-selected" : ""}">
          <input type="radio" name="adapt-direction" data-adapt-direction value="${esc(item.id)}" ${item.id === direction.id ? "checked" : ""}>
          <span class="adapt-direction-title">${esc(item.title)}</span><span>${esc(item.description)}</span>
          <small>适合这部剧，因为 ${esc(item.reason)}</small>
        </label>`).join("")}</div></fieldset>
      ${analysis.keep_options.length ? `<fieldset class="adapt-choice-group"><legend>希望保留的内容</legend><div class="adapt-keep-list">${analysis.keep_options.map(item => {
        const referenced = p.sources.find(scene => scene.id === item.source_id);
        return `<label class="adapt-keep-option"><input type="checkbox" data-adapt-keep value="${esc(item.id)}" ${selectedKeeps.has(item.id) ? "checked" : ""}>
          <span><strong>${esc(keepKinds[item.kind] || "建议")} · ${esc(item.label)}</strong><small>${esc(item.instruction)}</small>
          <em>${esc(referenced?.label || "原稿依据")}：“${esc(item.quote)}”</em></span></label>`;
      }).join("")}</div></fieldset>` : '<p class="adapt-note">这次没有找到可以直接引用的保留项。你可以先选改编方向，再按需补充保留要求。</p>'}
      ${analysis.questions.length ? `<div class="adapt-warnings"><strong>原稿中有待确认的信息</strong><ul>${analysis.questions.map(item => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
      <label class="adapt-label">补充改编要求（选填）<textarea id="adapt-direction-notes" rows="2" maxlength="1000" placeholder="AI 已起草改编方向，需要时再补充">${esc(directionNotes)}</textarea></label>
      <label class="adapt-label">补充保留要求（选填）<textarea id="adapt-keep-notes" rows="2" maxlength="1200" placeholder="例如：保留原剧的时代背景">${esc(keepNotes)}</textarea></label>
      <p class="adapt-note adapt-selected-scene">试改场次 · ${esc(source?.label || "请选择一场")} · 可在原稿场次菜单中更换</p>
      <button class="primary-btn" type="button" data-adapt="preview">${p.previewUsed ? "重新试改这一场" : "对照试改这一场"}${extraCost(p)}</button>${feedback(p, "preview", source?.id || "")}</section>`;
  }
  function renderPreview(p) {
    const preview = p.preview;
    if (!preview) return "";
    const original = p.sources.find(item => item.id === preview.source_id);
    return `<section class="adapt-panel" id="adapt-preview"><div class="adapt-section-heading"><span class="adapt-step">03</span><div><h2 tabindex="-1">对照试改</h2><p>检查人物、语气和关键情节是否符合你想要的方向。</p></div></div><div class="adapt-columns">
      <div><h3>原稿 · ${esc(original.label)}</h3><pre>${esc(original.text)}</pre></div>
      <div><h3>试改稿</h3><pre>${esc(screenplay([preview.scene], preview.cast, 1))}</pre></div>
      </div><ul>${preview.decisions.map(item => `<li>${esc(item)}</li>`).join("")}</ul>
      ${preview.questions.length ? `<div class="adapt-warnings"><strong>建议确认</strong><ul>${preview.questions.map(item => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
      ${p.confirmed ? "" : `<label class="adapt-label">补充改编要求（选填）<textarea id="adapt-plan-notes" rows="2" maxlength="1000" placeholder="说明试改里哪些改法要保留或调整">${esc(planNotes)}</textarea></label>
      <button class="primary-btn" data-adapt="plan" type="button">${p.planUsed ? "重新生成大纲" : "按这个方向生成分集大纲"}${extraCost(p)}</button>${feedback(p, "plan")}`}</section>`;
  }
  function renderCharacters(p) {
    const cast = p.plan?.cast || p.preview?.cast;
    if (!cast?.length) return "";
    return `<section class="adapt-panel adapt-characters"><h2>人物介绍</h2><div class="adapt-character-grid">${cast.map(c => `
      <article><h3>${esc(c.name)}</h3><small>原人物 ${esc(c.original_name)}</small><p>${esc(c.role)}</p>
        <p>${esc(c.biography || "完整简介待补充，可沿用已有身份与关系继续改编。")}</p>
        ${c.relationships ? `<p><strong>人物关系</strong> ${esc(c.relationships)}</p>` : ""}
        ${c.motivation ? `<p><strong>目标与冲突</strong> ${esc(c.motivation)}</p>` : ""}</article>`).join("")}</div></section>`;
  }
  function lockRow(p, item = {}) {
    return `<div class="adapt-lock-row">
      <label>落在哪一集 <input name="lock_episode" type="number" min="1" max="${p.episodeCount}" value="${item.episode || 1}" required></label>
      <label>说话人 <select name="lock_speaker">${p.plan.cast.map(c =>
        `<option value="${esc(c.id)}" ${c.id === item.speaker_id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>
      <label>原文来源 <select name="lock_source">${sourceOptions(p.sources, item.source_id)}</select></label>
      <label>原文台词 <input name="lock_text" value="${esc(item.text)}" maxlength="500" placeholder="复制原台词，保留标点" required></label>
      <button class="secondary-btn" type="button" data-adapt="remove-line">移除</button>
    </div>`;
  }
  function renderPlan(p) {
    const plan = p.plan;
    if (!plan) return "";
    return `<section class="adapt-panel"><div class="adapt-section-heading"><span class="adapt-step">04</span><div><h2>确认分集大纲</h2><p>可以调整人物、冲突、集尾悬念和必须保留的台词。</p></div></div>
      <form class="adapt-form" data-adapt-form="plan">
        <label>新剧名 <input name="title" value="${esc(plan.title)}" maxlength="100" required></label>
        <label>全剧梗概 <textarea name="premise" rows="4" maxlength="1100" required>${esc(plan.premise)}</textarea></label>
        <details open><summary>跨集共用的人物表</summary><div class="adapt-cast">${plan.cast.map(c => `
          <div class="adapt-cast-item" data-cast-id="${esc(c.id)}"><span>${esc(c.original_name)} · ${esc(c.id)}</span>
            <label>改编名 <input name="character_name" maxlength="40" value="${esc(c.name)}" required></label>
            <label>身份与定位 <input name="character_role" maxlength="160" value="${esc(c.role)}" required></label>
            <label>人物简介 <textarea name="character_biography" maxlength="500" rows="3">${esc(c.biography)}</textarea></label>
            <label>人物关系 <textarea name="character_relationships" maxlength="300" rows="2">${esc(c.relationships)}</textarea></label>
            <label>目标与冲突 <textarea name="character_motivation" maxlength="300" rows="2">${esc(c.motivation)}</textarea></label></div>`).join("")}</div></details>
        <details open><summary>分集大纲 · ${p.episodeCount} 集</summary><div class="adapt-outlines">${plan.episodes.map(e => `
          <div class="adapt-outline" data-episode="${e.number}"><h3>第 ${e.number} 集</h3>
            <label>冲突与转折 <textarea name="beats" rows="3" maxlength="500" required>${esc(e.beats)}</textarea></label>
            <label>集尾悬念 <input name="hook" maxlength="200" value="${esc(e.hook)}" required></label>
            <p class="adapt-note">原稿参考 · ${esc(e.source_ids.map(id => p.sources.find(item => item.id === id)?.label).join("、"))}</p>
          </div>`).join("")}</div></details>
        <details ${p.locks.length ? "open" : ""}><summary>必须逐字保留的台词</summary>
          <p class="adapt-note">为关键台词选定原稿场次、说话人和目标集。字句或说话人对不上时，正文会提示重试。</p>
          <div class="adapt-locks">${p.locks.map(item => lockRow(p, item)).join("")}</div>
          <button class="secondary-btn" type="button" data-adapt="add-line">添加关键台词</button>
        </details>
        <div class="adapt-toolbar"><button class="primary-btn" type="submit">${p.confirmed ? "保存大纲修改" : "确认大纲"}</button>
        ${p.planBeforeEdit ? `<button class="secondary-btn" type="button" data-adapt="impact">查找受影响的集数${extraCost(p)}</button>` : ""}</div>
      </form>
      ${feedback(p, "impact")}
      ${p.impact ? `<div class="adapt-warnings"><strong>大纲改动影响</strong>${p.impact.length ? `<ul>${p.impact.map(item =>
        `<li>第 ${item.number} 集 · ${esc(item.reason)}<br>建议 · ${esc(item.suggestion)}</li>`).join("")}</ul>` : "<p>本次没有找到可定位的影响，仍可按集核对。</p>"}</div>` : ""}
    </section>`;
  }
  function draftEditor(p, number, output) {
    if (output.locked) return "";
    return `<details class="adapt-editor"><summary>编辑第 ${number} 集</summary>
      <form class="adapt-form" data-adapt-form="episode" data-number="${number}">
        ${output.draft.scenes.map((scene, index) => `<div class="adapt-draft-scene">
          <label>第 ${index + 1} 场场景 <input name="setting" maxlength="120" value="${esc(scene.setting)}" required></label>
          ${scene.blocks.map(block => `<div class="adapt-draft-block">
            <select aria-label="动作或说话人"><option value="" ${block.speaker_id ? "" : "selected"}>动作</option>
            ${p.plan.cast.map(c => `<option value="${esc(c.id)}" ${c.id === block.speaker_id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>
            <textarea aria-label="动作或台词正文" rows="2" maxlength="1000" required>${esc(block.text)}</textarea>
          </div>`).join("")}
        </div>`).join("")}
        <label>下集衔接 <textarea name="continuity" rows="2" maxlength="800" required>${esc(output.draft.continuity)}</textarea></label>
        <button class="primary-btn" type="submit">${output.stale ? "确认这集已核对" : "保存本集修改"}</button>
      </form></details>`;
  }
  function episodeRevision(p, number, output) {
    return `<div class="adapt-revision">
      <label class="adapt-label">AI 修改要求（选填）
        <textarea data-adapt-revision-notes="${number}" rows="2" maxlength="1000" placeholder="例如：缩短开场对白，保留账册被调换的线索">${esc(episodeNotes.get(Number(number)))}</textarea>
      </label>
      <button class="secondary-btn" type="button" data-adapt="episode" data-number="${number}">${output.stale ? "按最新大纲更新" : "用 AI 修改本集"}${extraCost(p)}</button>
    </div>`;
  }
  function episodeHistory(number, output) {
    if (!(output.version > 1)) return "";
    const versions = Array.from({ length: output.version - 1 }, (_, index) => output.version - 1 - index);
    return `<details class="adapt-editor adapt-history"><summary>查看历史稿 · 当前第 ${output.version} 版</summary>
      <div class="adapt-toolbar"><select aria-label="历史版本">${versions.map(version =>
        `<option value="${version}">第 ${version} 版</option>`).join("")}</select>
        <button class="secondary-btn" type="button" data-adapt="view-version" data-number="${number}">查看</button></div>
      <pre data-adapt-history-text aria-live="polite" hidden></pre></details>`;
  }
  function renderOutputs(p) {
    if (!p.confirmed) return "";
    const outputs = Object.entries(p.outputs).sort(([a], [b]) => Number(a) - Number(b));
    const ready = outputs.find(([_, value]) => value.stale);
    const next = outputs.length + 1;
    const complete = outputs.length === p.episodeCount && !ready;
    const review = p.reviewAll;
    return `<section class="adapt-panel adapt-output-panel"><div class="adapt-section-heading"><span class="adapt-step">05</span><div><h2>分集正文与审稿</h2><p>逐集生成会同时检查大纲、保留要求和人物连续性。</p></div></div>
      <div class="adapt-toolbar">
        ${next <= p.episodeCount && !ready ? `<button class="primary-btn" type="button" data-adapt="episode" data-number="${next}">写第 ${next} 集${next === 1 ? " · 已含在项目费中" : ` · ${episodePoints(p)} 积分`}</button>` : ""}
        ${outputs.length && !ready ? `<a class="secondary-btn" href="${esc(api.adaptationDownloadUrl(p.id))}">导出已写正文 · Markdown</a>
          <a class="secondary-btn" href="${esc(api.adaptationDownloadUrl(p.id, "txt"))}">导出已写正文 · TXT</a>` : ""}
      </div>
      ${feedback(p, "episode")}
      ${ready ? '<p class="adapt-warnings">有正文需要按最新大纲核对，请从最早受影响的一集更新。已锁定集可手动解锁。</p>' : ""}
      ${outputs.map(([number, output]) => `<details class="adapt-episode" ${number === (ready?.[0] || outputs.at(-1)?.[0]) ? "open" : ""}>
        <summary>第 ${number} 集 ${output.locked ? "· 已锁定" : ""} ${output.stale ? "· 待更新" : ""}</summary>
        <pre>${esc(screenplay(output.draft.scenes, p.plan.cast, Number(number)))}</pre>
        <p class="adapt-note">下集衔接 · ${esc(output.draft.continuity)}</p>
        ${output.revisionNotes ? `<p class="adapt-note">本版修改要求 · ${esc(output.revisionNotes)}</p>` : ""}
        ${p.reviews?.[number] ? `<div class="adapt-review"><strong>AI 审稿 · ${esc(p.reviews[number].summary)}</strong>${p.reviews[number].issues.length ? `<ul>${p.reviews[number].issues.map(issue => `<li class="${issue.severity === "error" ? "is-error" : ""}"><b>${issue.severity === "error" ? "需处理" : "建议核对"}</b> · ${esc(issue.description)}<br>建议 · ${esc(issue.suggestion)}</li>`).join("")}</ul>` : "<p>未发现有明确依据的剧情或保留项遗漏。</p>"}</div>` : ""}
        ${draftEditor(p, number, output)}
        ${episodeHistory(number, output)}
        ${!output.locked && (!ready || Number(number) <= Number(ready[0])) ? episodeRevision(p, number, output) : ""}
        <div class="adapt-toolbar"><button class="secondary-btn" type="button" data-adapt="lock" data-number="${number}" ${output.stale && !output.locked ? "disabled" : ""}>${output.locked ? "解锁此集" : "锁定此集"}</button></div>
      </details>`).join("")}
      ${complete ? `<div class="adapt-full-review"><div><h3>整剧衔接检查</h3><p>检查人物状态、时间线、关键证据和伏笔回收。</p></div><button class="secondary-btn" type="button" data-adapt="review_all">${p.reviewAllUsed ? "重新检查整剧" : "检查整剧衔接"}${extraCost(p)}</button>${feedback(p, "review_all")}
        ${review ? `<div class="adapt-review"><strong>${esc(review.summary)}</strong>${review.issues.length ? `<ul>${review.issues.map(issue => `<li class="${issue.severity === "error" ? "is-error" : ""}"><b>第 ${issue.episode} 集 · ${issue.severity === "error" ? "需处理" : "建议核对"}</b><br>${esc(issue.description)}<br>建议 · ${esc(issue.suggestion)}</li>`).join("")}</ul>` : "<p>未发现明确的跨集矛盾。</p>"}</div>` : ""}</div>` : ""}
    </section>`;
  }
  function renderProject(p) {
    project = p;
    if (p.analysis && !p.analysis.directions.some(item => item.id === selectedDirection)) {
      selectedDirection = p.selectedDirectionId || p.analysis.directions[0].id;
    }
    if (p.analysis && !selected) selected = p.analysis.preview_source_id;
    const completedStep = p.reviewAll ? 6 : p.outputs && Object.keys(p.outputs).length === p.episodeCount
      && !Object.values(p.outputs).some(output => output.stale) ? 5
        : p.confirmed ? 4 : p.preview ? 3 : p.analysis ? 2 : 1;
    const steps = ["原稿", "方向", "试改", "大纲", "分集", "审稿"];
    main().innerHTML = layout(p.plan?.title || p.title, p.goal || p.analysis?.summary || "先读懂原稿，再一起确定改编方向。", `
      <div class="adapt-flow" aria-label="改编进度">${steps.map((label, index) => `<span class="${index < completedStep ? "is-complete" : ""}"><i>${String(index + 1).padStart(2, "0")}</i>${label}</span>`).join("")}</div>
      <div class="adapt-pricing"><strong>${p.billing?.setupPaid ? "项目分析费已支付，包含第 1 集" : `原稿分析 ${projectPoints(p)} 积分，含第 1 集`}</strong><span>第 2 集起 ${episodePoints(p)} 积分/集 · 单独发起的 AI 操作 ${rewritePoints(p)} 积分/次 · 手动编辑、保存和导出免费</span></div>
      ${availability?.ready === false ? '<p class="adapt-note">改编服务暂不可用，原稿仍可保存。</p>' : ""}
      <div id="adapt-progress-region">${renderProgress(p)}</div>
      ${mobileReadOnly() ? '<p class="adapt-readonly">手机端为只读浏览，编辑和生成请使用电脑。</p>' : ""}
      <fieldset class="adapt-workbench" ${p.job ? "disabled" : ""}>
        ${renderSource(p)}${renderAnalysis(p)}${renderPreview(p)}${renderCharacters(p)}${renderPlan(p)}${renderOutputs(p)}
        <button class="secondary-btn adapt-delete" type="button" data-adapt="delete">删除此项目</button>
      </fieldset>`);
    if (p.error && !p.errorKind && p.plan) error(p.error);
    applyReadOnly();
    if (p.job) poll(p.job);
  }
  function renderProgress(p) {
    const progress = p.progress || { percent: 0, label: kinds[p.job] || "等待开始改编" };
    const percent = Number(progress.percent) || 0;
    const updated = progress.updatedAt || p.updatedAt;
    return `<section class="adapt-progress ${p.job ? "is-running" : ""}" aria-label="当前改编状态">
      <div class="adapt-progress-heading"><strong role="status">${esc(progress.label)}</strong><span>${percent}%</span></div>
      <progress max="100" value="${percent}" aria-label="已完成的改编阶段">${percent}%</progress>
      ${progress.episodeCount ? `<small>正文已完成 ${Number(progress.completedEpisodes) || 0}/${Number(progress.episodeCount)} 集</small>` : ""}
      <p>${p.job ? "正在处理中，完成后自动显示结果。离开页面后可从项目列表继续查看。" : p.error ? esc(p.error) : "进度随已完成阶段更新。"}</p>
      ${updated ? `<small>最后更新 ${esc(new Date(updated).toLocaleString("zh-CN", { hour12: false }))}</small>` : ""}</section>`;
  }
  function applyReadOnly() {
    const readonly = mobileReadOnly();
    for (const node of main().querySelectorAll?.(".adapt-workbench input, .adapt-workbench textarea, .adapt-workbench button, .adapt-workbench select") || []) {
      const readAction = ["show-preview", "view-version"].includes(node.dataset.adapt)
        || node.matches("[data-adapt-source], .adapt-history select");
      if (readAction) continue;
      if (readonly && !node.disabled) { node.dataset.mobileDisabled = "true"; node.disabled = true; }
      if (!readonly && node.dataset.mobileDisabled) { node.disabled = false; delete node.dataset.mobileDisabled; }
    }
  }
  function showPreview() {
    const heading = main().querySelector("#adapt-preview h2");
    heading?.focus({ preventScroll: true });
    heading?.scrollIntoView({ block: "start" });
  }
  function poll(kind, failures = 0) {
    const active = view;
    timer = setTimeout(async () => {
      if (active !== view) return;
      const path = idPath();
      try {
        const status = await api.adaptation(path + "/progress");
        if (active !== view) return;
        const element = main().querySelector(`button[data-adapt="${kind}"]`);
        if (status.job) {
          if (status.progress) {
            project.progress = status.progress;
            const region = document.getElementById("adapt-progress-region");
            if (region) region.innerHTML = renderProgress(project);
          }
          if (failures) feedbackText(element, kinds[kind] + "，完成后会自动显示结果。", false, true);
          poll(kind);
          return;
        }
        const next = await api.adaptation(path);
        if (active !== view) return;
        renderProject(next);
        await refreshBalance();
        if (kind === "analyze" && next.analysis && !next.error && !next.job) {
          const heading = main().querySelector(".adapt-analysis-panel h2");
          heading?.focus({ preventScroll: true });
          heading?.scrollIntoView({ block: "start" });
        }
        if (kind === "preview" && next.preview && !next.error && !next.job) {
          const button = main().querySelector('[data-adapt="preview"]');
          const bounds = button?.getBoundingClientRect();
          if (bounds && bounds.bottom >= 0 && bounds.top <= window.innerHeight) showPreview();
        }
      } catch (err) {
        if (active !== view) return;
        const element = main().querySelector(`button[data-adapt="${kind}"]`);
        if (err.status === 401 || err.status === 403 || err.status === 404) {
          feedbackText(element, err.message, true, true);
          return;
        }
        feedbackText(element, "连接暂时中断，正在重查任务状态。", false, true);
        poll(kind, failures + 1);
      }
    }, Math.min(15000, 2200 * (failures + 1)));
  }
  async function open(path, formatUpdatedAt) {
    const active = view;
    await refreshBalance();
    if (active !== view) return;
    project = null;
    selected = "";
    planNotes = "";
    selectedDirection = "";
    directionNotes = "";
    keepNotes = "";
    sourceDrafts.clear();
    episodeNotes.clear();
    main().innerHTML = layout("剧本改编", "", "<p role='status'>正在加载…</p>");
    try {
      const [page, taskId] = path.split("/").filter(Boolean);
      if (page === "new") {
        const task = taskId ? await api.getTask(decodeURIComponent(taskId)) : null;
        if (active === view) renderNew(task);
      } else if (page) {
        const p = await api.adaptation("/" + encodeURIComponent(page));
        if (active === view) renderProject(p);
      } else {
        const items = await api.adaptation();
        if (active === view) renderList(items, formatUpdatedAt);
      }
    } catch (err) { if (active === view) error(err.message); }
  }
  async function action(work, element, pendingText = "", isJob = false) {
    if (busy) return;
    const active = view;
    busy = true;
    if (element) element.disabled = true;
    feedbackText(element, pendingText, false, isJob);
    try {
      const next = await work();
      if (next && active === view) renderProject(next);
    } catch (err) {
      if (active === view) feedbackText(element, err.message, true, isJob);
    } finally {
      await refreshBalance();
      if (active === view) { busy = false; if (element?.isConnected) element.disabled = false; }
    }
  }
  function planBody(form) {
    return {
      revision: project.revision,
      plan: {
        ...project.plan,
        title: form.elements.title.value, premise: form.elements.premise.value,
        cast: [...form.querySelectorAll("[data-cast-id]")].map((row, i) => ({
          ...project.plan.cast[i], name: row.querySelector("[name=character_name]").value,
          role: row.querySelector("[name=character_role]").value,
          biography: row.querySelector("[name=character_biography]").value,
          relationships: row.querySelector("[name=character_relationships]").value,
          motivation: row.querySelector("[name=character_motivation]").value
        })),
        episodes: [...form.querySelectorAll("[data-episode]")].map((row, i) => ({
          ...project.plan.episodes[i], beats: row.querySelector("[name=beats]").value,
          hook: row.querySelector("[name=hook]").value
        }))
      },
      locks: [...form.querySelectorAll(".adapt-lock-row")].map(row => ({
        episode: Number(row.querySelector("[name=lock_episode]").value),
        speaker_id: row.querySelector("[name=lock_speaker]").value,
        source_id: row.querySelector("[name=lock_source]").value,
        text: row.querySelector("[name=lock_text]").value
      }))
    };
  }
  document.addEventListener("submit", event => {
    const form = event.target.closest("[data-adapt-form]");
    if (!form) return;
    event.preventDefault();
    if (mobileReadOnly()) return;
    const kind = form.dataset.adaptForm;
    const data = new FormData(form);
    action(async () => {
      if (kind === "create") {
        const created = await api.adaptation("", "POST", { ...Object.fromEntries(data), task_id: form.dataset.task,
          episode_count: Number(data.get("episode_count")) });
        if (form.isConnected) go("/" + created.id);
      } else if (kind === "source") {
        const id = currentSource().id;
        const saved = await api.adaptation(idPath() + "/source", "PATCH", {
          revision: project.revision, source_id: id, text: data.get("text")
        });
        if (form.isConnected) sourceDrafts.delete(id);
        return saved;
      } else if (kind === "episode") {
        const number = Number(form.dataset.number);
        const draft = {
          continuity: data.get("continuity"),
          scenes: [...form.querySelectorAll(".adapt-draft-scene")].map(scene => ({
            setting: scene.querySelector("[name=setting]").value,
            blocks: [...scene.querySelectorAll(".adapt-draft-block")].map(block => ({
              speaker_id: block.querySelector("select").value,
              text: block.querySelector("textarea").value
            }))
          }))
        };
        return api.adaptation(idPath() + "/episodes/" + number, "PUT", { revision: project.revision, draft });
      } else return api.adaptation(idPath() + "/plan", "PUT", planBody(form));
    }, event.submitter);
  });
  document.addEventListener("input", event => {
    const node = event.target;
    if (project && node.matches('[data-adapt-form="source"] textarea')) {
      sourceDrafts.set(currentSource().id, node.value);
    }
    if (node.matches("#adapt-plan-notes")) planNotes = node.value;
    if (node.matches("#adapt-direction-notes")) directionNotes = node.value;
    if (node.matches("#adapt-keep-notes")) keepNotes = node.value;
    if (node.matches("[data-adapt-revision-notes]")) episodeNotes.set(Number(node.dataset.adaptRevisionNotes), node.value);
  });
  document.addEventListener("change", event => {
    const node = event.target;
    if (node.matches("[data-adapt-source]")) {
      selected = node.value;
      node.closest(".adapt-panel").outerHTML = renderSource(project);
      applyReadOnly();
      const label = main().querySelector(".adapt-selected-scene");
      if (label) label.textContent = `试改场次 · ${currentSource().label} · 可在原稿场次菜单中更换`;
    }
    if (node.matches("[data-adapt-direction]")) selectedDirection = node.value;
    if (node.matches("[data-adapt-direction]")) {
      main().querySelectorAll(".adapt-direction-card").forEach(card => {
        card.classList.toggle("is-selected", card.contains(node));
      });
    }
    if (!node.matches("[data-adapt-file]") || !node.files[0]) return;
    if (mobileReadOnly()) return;
    const form = node.closest("form");
    const data = new FormData();
    data.append("file", node.files[0]);
    node.value = "";
    action(async () => {
      const parsed = await api.adaptation("/parse", "POST", data);
      if (form.isConnected) form.elements.source_text.value = parsed.text;
    }, node);
  });
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-adapt]");
    if (!button || !project) return;
    const kind = button.dataset.adapt;
    if (kind === "show-preview") { event.preventDefault(); showPreview(); return; }
    if (mobileReadOnly() && kind !== "view-version") return;
    const episode = Number(button.dataset.number || 1);
    if (kind === "add-line") {
      const list = main().querySelector(".adapt-locks");
      if (list.children.length < 40) list.insertAdjacentHTML("beforeend", lockRow(project));
      return;
    }
    if (kind === "remove-line") { button.closest(".adapt-lock-row").remove(); return; }
    if (busy) return;
    if (kind === "view-version") {
      const history = button.closest(".adapt-history");
      const version = Number(history.querySelector("select").value);
      const preview = history.querySelector("[data-adapt-history-text]");
      action(async () => {
        preview.hidden = false;
        preview.textContent = "正在读取历史稿…";
        try {
          const saved = await api.adaptation(idPath() + "/episodes/" + episode + "/versions/" + version);
          preview.textContent = saved.notes ? saved.notes + "\n\n" + saved.screenplay : saved.screenplay;
        } catch (err) { preview.textContent = err.message || "无法读取历史稿。"; }
      }, button);
      return;
    }
    if (kind === "delete" && !window.confirm("删除这个改编项目及其改稿？")) return;
    if (kind === "episode" || kind === "impact") {
      const form = main().querySelector('[data-adapt-form="plan"]');
      if (form && JSON.stringify(planBody(form)) !== JSON.stringify({
        revision: project.revision, plan: project.plan, locks: project.locks
      })) {
        feedbackText(button, "请先保存大纲和关键台词的修改。", true, true); return;
      }
    }
    const source = currentSource();
    const sourceField = main().querySelector('[data-adapt-form="source"] textarea');
    const isJob = kind in kinds;
    const path = idPath();
    const active = view;
    const existingEpisode = kind === "episode" ? project.outputs[String(episode)] : null;
    const revisionNotes = existingEpisode ? episodeNotes.get(episode) || "" : "";
    if (kind === "analyze" && sourceField && !sourceText(sourceField.value)) {
      feedbackText(button, "原稿正文为空，请先填写要试改的内容。", true, true);
      sourceField.focus();
      return;
    }
    const amount = !isJob ? 0 : kind === "analyze" ? projectPoints(project)
      : kind === "episode" && !existingEpisode ? (episode === 1 ? 0 : episodePoints(project))
      : rewritePoints(project);
    if (amount && !window.confirm(`本次将扣除 ${amount} 积分。${kind === "analyze" ? "项目费包含第 1 集正文和审稿。" : ""}失败或中断的任务会退回预扣积分。`)) return;
    action(async () => {
      if (kind === "delete") { await api.adaptation(idPath(), "DELETE"); if (button.isConnected) go(""); return; }
      if (kind === "lock") return api.adaptation(idPath() + "/episodes/" + episode + "/lock", "PUT", {
        revision: project.revision, locked: !project.outputs[episode].locked
      });
      let revision = project.revision;
      if (kind === "analyze" && sourceField && sourceText(sourceField.value) !== sourceText(source.text)) {
        const saved = await api.adaptation(path + "/source", "PATCH", {
          revision, source_id: source.id, text: sourceField.value
        });
        if (active !== view) return;
        sourceDrafts.delete(source.id);
        project = saved;
        revision = saved.revision;
      }
      try {
        return await api.adaptation(path + "/jobs", "POST", {
          kind, episode, source_id: source.id,
          direction_id: kind === "preview" ? (main().querySelector("[data-adapt-direction]:checked")?.value || selectedDirection) : "",
          keep_option_ids: kind === "preview" ? [...main().querySelectorAll("[data-adapt-keep]:checked")].map(item => item.value) : [],
          notes: kind === "plan" ? planNotes : kind === "preview" ? directionNotes : revisionNotes,
          keep_notes: kind === "preview" ? keepNotes : "", revision, expected_points: amount
        });
      } catch (err) {
        if (active === view && (!err.status || err.status === 409)) {
          const updated = await api.adaptation(path).catch(() => null);
          if (updated && (updated.job || updated.revision !== revision)) return updated;
        }
        throw err;
      }
    }, button, isJob ? "正在提交…" : "", isJob);
  });
  window.matchMedia?.("(max-width: 780px)").addEventListener?.("change", applyReadOnly);
  window.AdaptationStudio = { open, leave, setAvailability(value) { availability = value; },
    setBalanceRefresh(callback) { refreshBalance = callback; } };
})();
