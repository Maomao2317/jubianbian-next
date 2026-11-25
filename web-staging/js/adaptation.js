(() => {
  const api = window.AppAPI;
  const main = () => document.getElementById("main");
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
  const kinds = { preview: "正在试改这一场", plan: "正在整理分集大纲", episode: "正在写分集正文", impact: "正在核对受影响的集数" };
  let availability, view = 0, timer, project, selected = "", busy = false;

  const idPath = () => "/" + encodeURIComponent(project.id);
  function leave() { view += 1; clearTimeout(timer); busy = false; }
  function go(path) { location.hash = "#/adaptation" + path; }
  function error(message) {
    const node = main().querySelector(".adapt-error");
    if (node) { node.textContent = message || "操作失败，请重试。"; node.hidden = false; }
  }
  function layout(title, subtitle, body) {
    return `<section class="page adapt-page"><a class="back-btn" href="#/adaptation">← 改编项目</a>
      <header class="adapt-title"><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></header>
      <p class="error-panel adapt-error" role="alert" hidden></p>${body}</section>`;
  }
  const sourceOptions = (sources, id) => sources.map(item =>
    `<option value="${esc(item.id)}" ${item.id === id ? "selected" : ""}>${esc(item.label)}</option>`).join("");
  function renderList(items) {
    main().innerHTML = layout("剧本改编", "从已有剧本开始，先试改一场，再确认大纲和分集正文。", `
      <div class="adapt-toolbar"><a class="primary-btn" href="#/adaptation/new">上传剧本</a><a class="secondary-btn" href="#/tasks">从平台任务选择</a></div>
      <div class="adapt-panel"><h2>我的改编项目</h2>${items.length ? `<ul class="adapt-projects">${items.map(item => `
        <li><a href="#/adaptation/${encodeURIComponent(item.id)}">${esc(item.title)}</a><time>${esc(item.updatedAt?.slice(0, 10))}</time></li>`).join("")}</ul>` : "<p>还没有改编项目。</p>"}</div>`);
  }
  function renderNew(task) {
    const fromTask = Boolean(task);
    main().innerHTML = layout("新建改编", fromTask ? `使用剧编编出稿《${task.batchTitle || task.title}》。` : "上传自己的剧本，也可以直接粘贴正文。", `
      <form class="adapt-panel adapt-form" data-adapt-form="create" data-task="${esc(task?.id || "")}">
        ${fromTask ? "" : `<label>上传剧本 <input type="file" data-adapt-file accept=".docx,.txt,.md"><small>DOCX、TXT 或 Markdown，最大 2 MB。其他格式可复制正文。</small></label>
        <label>原稿正文 <textarea name="source_text" rows="8" minlength="20" maxlength="60000" required placeholder="上传后可在这里检查全文，或直接粘贴剧本"></textarea></label>`}
        <label>项目名 <input name="title" maxlength="100" value="${esc(task?.batchTitle || task?.title || "")}" placeholder="例如：账房翻案"></label>
        <label>想怎么改？ <textarea name="goal" rows="3" minlength="3" maxlength="1000" required placeholder="例如：把都市真人短剧改成古装漫剧，案件发生在商号"></textarea></label>
        <label>希望保留什么？ <textarea name="keep" rows="2" maxlength="1200" placeholder="例如：保留被构陷、查证和当众翻案的主线"></textarea></label>
        <label>目标集数 <input name="episode_count" type="number" min="1" max="30" value="${Math.min(30, task?.batchTotal || 3)}" required></label>
        <button class="primary-btn" type="submit">导入并检查原稿</button>
      </form>`);
  }
  function screenplay(scenes, cast, episode) {
    const names = new Map(cast.map(item => [item.id, item.name]));
    return scenes.map((scene, index) => [`${episode}-${index + 1} ${scene.setting}`,
      ...scene.blocks.map(block => block.speaker_id ? `${names.get(block.speaker_id)}：${block.text}` : `△ ${block.text}`)].join("\n")).join("\n\n");
  }
  function currentSource() {
    return project.sources.find(item => item.id === selected)
      || project.sources.find(item => item.text.includes("\n"))
      || project.sources[0];
  }
  function renderSource(p) {
    const source = currentSource();
    const hint = source.start_sec == null ? "" : ` · 原片约 ${Math.floor(source.start_sec / 60)}:${String(Math.floor(source.start_sec % 60)).padStart(2, "0")}`;
    return `<section class="adapt-panel"><h2>1. 检查原稿并试改一场</h2>
      ${p.warnings.length ? `<details class="adapt-warnings"><summary>平台原稿有 ${p.warnings.length} 条重点核对提示</summary><ul>${p.warnings.map(item => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}
      <label class="adapt-label">原稿场次 <select data-adapt-source>${sourceOptions(p.sources, source.id)}</select></label>
      <div class="adapt-note">${esc(source.label + hint)} ${source.task_id ? `<a href="#/task/${encodeURIComponent(source.task_id)}">查看平台原稿</a>` : ""}</div>
      ${p.plan ? `<pre>${esc(source.text)}</pre>` : `<form data-adapt-form="source" class="adapt-form">
        <textarea name="text" rows="7" maxlength="12000" required aria-label="原稿修订副本">${esc(source.text)}</textarea>
        ${source.originalText ? `<details><summary>查看导入时的原文</summary><pre>${esc(source.originalText)}</pre></details>` : ""}
        <div class="adapt-toolbar"><button class="secondary-btn" type="submit">保存原稿修订</button>
          <button class="primary-btn" type="button" data-adapt="preview">试改选中场次</button></div>
      </form>`}</section>`;
  }
  function renderPreview(p) {
    const preview = p.preview;
    if (!preview) return "";
    const original = p.sources.find(item => item.id === preview.source_id);
    return `<section class="adapt-panel"><h2>2. 审阅这一场的改法</h2><div class="adapt-columns">
      <div><h3>原稿 · ${esc(original.label)}</h3><pre>${esc(original.text)}</pre></div>
      <div><h3>试改稿</h3><pre>${esc(screenplay([preview.scene], preview.cast, 1))}</pre></div>
      </div><ul>${preview.decisions.map(item => `<li>${esc(item)}</li>`).join("")}</ul>
      ${preview.questions.length ? `<div class="adapt-warnings"><strong>建议确认</strong><ul>${preview.questions.map(item => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
      ${p.confirmed ? "" : `<label class="adapt-label">补充改编要求（选填）<textarea id="adapt-plan-notes" rows="2" maxlength="1000" placeholder="说明试改里哪些改法要保留或调整"></textarea></label>
      <button class="primary-btn" data-adapt="plan" type="button">按这个方向生成分集大纲</button>`}</section>`;
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
    return `<section class="adapt-panel"><h2>3. 编辑并确认大纲</h2>
      <form class="adapt-form" data-adapt-form="plan">
        <label>新剧名 <input name="title" value="${esc(plan.title)}" maxlength="100" required></label>
        <label>全剧梗概 <textarea name="premise" rows="4" maxlength="1100" required>${esc(plan.premise)}</textarea></label>
        <details open><summary>跨集共用的人物表</summary><div class="adapt-cast">${plan.cast.map(c => `
          <div class="adapt-cast-item" data-cast-id="${esc(c.id)}"><span>${esc(c.original_name)} · ${esc(c.id)}</span>
            <label>改编名 <input name="character_name" maxlength="40" value="${esc(c.name)}" required></label>
            <label>身份与关系 <input name="character_role" maxlength="160" value="${esc(c.role)}" required></label></div>`).join("")}</div></details>
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
        ${p.planBeforeEdit ? '<button class="secondary-btn" type="button" data-adapt="impact">查找受影响的集数</button>' : ""}</div>
      </form>
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
  function renderOutputs(p) {
    if (!p.confirmed) return "";
    const outputs = Object.entries(p.outputs).sort(([a], [b]) => Number(a) - Number(b));
    const ready = outputs.find(([_, value]) => value.stale);
    const next = ready ? Number(ready[0]) : outputs.length + 1;
    return `<section class="adapt-panel"><h2>4. 逐集写正文</h2>
      <div class="adapt-toolbar">
        ${next <= p.episodeCount && !ready?.[1].locked ? `<button class="primary-btn" type="button" data-adapt="episode" data-number="${next}">${ready ? "更新" : "写"}第 ${next} 集</button>` : ""}
        ${outputs.length && !ready ? `<a class="secondary-btn" href="${esc(api.adaptationDownloadUrl(p.id))}">导出已写正文 · Markdown</a>` : ""}
      </div>
      ${ready ? '<p class="adapt-warnings">有正文需要按最新大纲核对，请从最早受影响的一集更新。已锁定集可手动解锁。</p>' : ""}
      ${outputs.map(([number, output]) => `<details class="adapt-episode" ${number === (ready?.[0] || outputs.at(-1)?.[0]) ? "open" : ""}>
        <summary>第 ${number} 集 ${output.locked ? "· 已锁定" : ""} ${output.stale ? "· 待更新" : ""}</summary>
        <pre>${esc(screenplay(output.draft.scenes, p.plan.cast, Number(number)))}</pre>
        <p class="adapt-note">下集衔接 · ${esc(output.draft.continuity)}</p>
        ${draftEditor(p, number, output)}
        <div class="adapt-toolbar"><button class="secondary-btn" type="button" data-adapt="lock" data-number="${number}" ${output.stale && !output.locked ? "disabled" : ""}>${output.locked ? "解锁此集" : "锁定此集"}</button>
          ${!output.locked && !output.stale ? `<button class="secondary-btn" type="button" data-adapt="episode" data-number="${number}">重写此集</button>` : ""}
        </div>
      </details>`).join("")}
    </section>`;
  }
  function renderProject(p) {
    project = p;
    const ran = Object.values(p.usage).at(-1);
    main().innerHTML = layout(p.plan?.title || p.title, p.goal, `
      ${p.keep ? `<p class="adapt-note">保留要求 · ${esc(p.keep)}</p>` : ""}
      <p class="adapt-note">${ran ? `最近生成 · ${esc(ran.model)} · ${esc(ran.promptVersion)}` :
        availability?.model ? `试用模型 · ${esc(availability.model)}` : "模型暂未配置，可先保存原稿。"}</p>
      ${p.job ? `<div class="adapt-progress" role="status">${esc(kinds[p.job] || "正在生成")}，离开后可从项目列表继续查看。</div>` : ""}
      <fieldset class="adapt-workbench" ${p.job ? "disabled" : ""}>
        ${renderSource(p)}${renderPreview(p)}${renderPlan(p)}${renderOutputs(p)}
        <button class="secondary-btn adapt-delete" type="button" data-adapt="delete">删除此项目</button>
      </fieldset>`);
    if (p.error) error(p.error);
    if (p.job) {
      const activeView = view;
      timer = setTimeout(async () => {
        try {
          const next = await api.adaptation(idPath());
          if (activeView !== view) return;
          renderProject(next);
        } catch (err) {
          if (activeView === view) {
            error(`${err.message}，可刷新页面查看任务状态。`);
          }
        }
      }, 3000);
    }
  }
  async function open(path) {
    const active = view;
    project = null;
    selected = "";
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
        if (active === view) renderList(items);
      }
    } catch (err) { if (active === view) error(err.message); }
  }
  async function action(work, element) {
    if (busy) return;
    const active = view;
    busy = true;
    if (element) element.disabled = true;
    try {
      const next = await work();
      if (next && active === view) renderProject(next);
    } catch (err) {
      if (active === view) error(err.message);
    } finally {
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
          role: row.querySelector("[name=character_role]").value
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
    const kind = form.dataset.adaptForm;
    const data = new FormData(form);
    action(async () => {
      if (kind === "create") {
        const created = await api.adaptation("", "POST", { ...Object.fromEntries(data), task_id: form.dataset.task,
          episode_count: Number(data.get("episode_count")) });
        if (form.isConnected) go("/" + created.id);
      } else if (kind === "source") {
        return api.adaptation(idPath() + "/source", "PATCH", {
          revision: project.revision, source_id: currentSource().id, text: data.get("text")
        });
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
  document.addEventListener("change", event => {
    const node = event.target;
    if (node.matches("[data-adapt-source]")) {
      selected = node.value;
      // Changing the source should not discard in-progress outline edits.
      node.closest(".adapt-panel").outerHTML = renderSource(project);
    }
    if (!node.matches("[data-adapt-file]") || !node.files[0]) return;
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
    const episode = Number(button.dataset.number || 1);
    if (kind === "add-line") {
      const list = main().querySelector(".adapt-locks");
      if (list.children.length < 40) list.insertAdjacentHTML("beforeend", lockRow(project));
      return;
    }
    if (kind === "remove-line") { button.closest(".adapt-lock-row").remove(); return; }
    if (busy) return;
    if (kind === "delete" && !window.confirm("删除这个改编项目及其改稿？")) return;
    if (kind === "episode" || kind === "impact") {
      const form = main().querySelector('[data-adapt-form="plan"]');
      if (form && JSON.stringify(planBody(form)) !== JSON.stringify({
        revision: project.revision, plan: project.plan, locks: project.locks
      })) {
        error("请先保存大纲和关键台词的修改。"); return;
      }
    }
    if (kind === "preview") {
      const sourceField = main().querySelector('[data-adapt-form="source"] textarea');
      if (sourceField && sourceField.value.trim() !== currentSource().text) {
        error("原稿有未保存的修订，请先保存，再试改。"); return;
      }
    }
    action(async () => {
      if (kind === "delete") { await api.adaptation(idPath(), "DELETE"); if (button.isConnected) go(""); return; }
      if (kind === "lock") return api.adaptation(idPath() + "/episodes/" + episode + "/lock", "PUT", {
        revision: project.revision, locked: !project.outputs[episode].locked
      });
      const notes = kind === "plan" ? main().querySelector("#adapt-plan-notes")?.value || "" : "";
      return api.adaptation(idPath() + "/jobs", "POST", {
        kind, episode, source_id: currentSource().id, notes, revision: project.revision
      });
    }, button);
  });
  window.AdaptationStudio = { open, leave, setAvailability(value) { availability = value; } };
})();
