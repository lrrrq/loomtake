/* Loaded after app.js; requests and image URLs use the workbench authorities. */
(() => {
  "use strict";

  const sessions = new Map();
  const runningStatuses = new Set(["created", "queued", "pending", "understanding", "executing", "running", "processing"]);
  let active = null;

  function key(prefix) {
    return `${prefix}-${crypto.randomUUID()}`;
  }

  function path(session, suffix = "") {
    return `/api/formal/projects/${encodeURIComponent(session.projectId)}/image-creations${suffix}?workspace_id=${encodeURIComponent(session.workspaceId)}`;
  }

  function basis(context) {
    return { target_basis: context?.target_basis, selection_basis: context?.selection_basis };
  }

  function current(session) {
    return active === session && Boolean(session.dialog?.isConnected);
  }

  function uncertain(session) {
    return Boolean(session.pendingSubmission?.resultUncertain || [...session.items.values()].some(item => sameTarget(session, item) && item.result_uncertain && !session.acknowledgedUncertain.has(item.task_id)));
  }

  function working(session) {
    return [...session.items.values()].some(item => sameTarget(session, item) && runningStatuses.has(item.status));
  }

  function notice(session, message) {
    session.message = message;
    if (current(session)) session.dialog.querySelector("[data-creation-notice]").textContent = message;
  }

  function errorMessage(error) {
    return formalError(error);
  }

  function installStyles() {
    if (document.getElementById("mplus-image-creation-style")) return;
    const style = document.createElement("style");
    style.id = "mplus-image-creation-style";
    style.textContent = `
      .image-creation-dialog:not([open]) { display:none; }
      .image-creation-dialog { width:min(1120px,calc(100vw - 32px)); max-width:none; height:min(880px,calc(100dvh - 32px)); max-height:none; margin:auto; padding:0; gap:0; border:1px solid var(--line,#ddd); border-radius:6px; box-shadow:0 16px 80px #0003; animation:none; }
      .image-creation-dialog::backdrop { background:rgba(24,24,24,.4); }
      .image-creation-dialog > header { padding:20px 24px; border-bottom:1px solid var(--line,#ddd); }
      .image-creation-dialog h2 { font-size:24px; }
      .image-creation-dialog h3 { margin:0 0 10px; font-size:15px; }
      .image-creation-dialog .image-creation-layout { display:grid; grid-template-columns:minmax(360px, .9fr) minmax(0,1fr); flex:1 1 0; min-height:0; overflow:hidden; }
      .image-creation-controls,.image-creation-results { overflow:auto; min-width:0; padding:24px; overscroll-behavior:contain; }
      .image-creation-controls { border-right:1px solid var(--line,#ddd); }
      .image-creation-controls > * + * { margin-top:16px; }
      .image-creation-dialog textarea { display:block; width:100%; min-height:230px; margin-top:10px; padding:14px; resize:vertical; border:1px solid var(--line,#ddd); border-radius:3px; background:var(--paper,#fff); color:var(--ink,#222); font:inherit; line-height:1.8; }
      .image-creation-dialog textarea:focus { outline:2px solid var(--lime,#41ff00); outline-offset:2px; }
      .image-creation-dialog select { display:block; width:100%; margin-top:6px; padding:9px 10px; border:1px solid var(--line,#ddd); border-radius:3px; background:var(--paper,#fff); color:var(--ink,#222); font:inherit; }
      .image-creation-dialog details { margin-top:16px; font-size:13px; }
      .image-creation-dialog summary { cursor:pointer; color:var(--muted,#666); }
      .image-creation-dialog [data-creation-target] { display:block; max-width:68ch; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted,#666); font-size:12px; }
      .image-creation-dialog > header > div { min-width:0; }
      .image-creation-prompt-actions { display:flex; flex-wrap:wrap; align-items:center; gap:12px; }
      .image-creation-posttext { padding:12px 14px; background:var(--ground,#f6f6f6); border-left:2px solid var(--line,#ddd); }
      .image-creation-posttext p { margin:4px 0 0; }
      .image-creation-result-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:16px; }
      .image-creation-result-heading h3 { margin:0; }
      .image-creation-dialog label,.image-creation-dialog p { font-size:13px; line-height:1.65; }
      .image-creation-dialog fieldset { min-width:0; margin-inline:0; padding:0; border:0; }
      .image-creation-dialog legend { margin-bottom:8px; font-size:13px; font-weight:600; }
      .image-creation-reference-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }
      .image-creation-reference { display:block; position:relative; min-width:0; overflow-wrap:anywhere; padding:6px; border:1px solid var(--line,#ddd); border-radius:3px; }
      .image-creation-reference:has(input:checked) { border-color:var(--ink,#222); box-shadow:0 0 0 2px var(--lime,#41ff00); }
      .image-creation-reference .creative-picker-pixel { margin-bottom:6px; }
      .image-creation-reference input { margin:0 5px 0 0; width:auto; }
      .image-creation-reference button { margin-top:5px; padding:3px; font-size:12px; }
      .image-creation-muted { color:var(--muted,#666); font-size:12px!important; }
      .image-creation-results { background:var(--ground,#f6f6f6); }
      .image-creation-result { padding:16px; margin:0 0 18px; background:var(--paper,#fff); border:1px solid var(--line,#ddd); border-radius:3px; }
      .image-creation-result header { align-items:flex-start; margin-bottom:12px; }
      .image-creation-result figure { min-width:0; margin:0; }
      .image-creation-result figcaption { padding:7px 0; color:var(--muted,#666); font-size:12px; }
      .image-creation-compare { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,2fr); gap:12px; align-items:start; }
      .image-creation-compare.no-references { grid-template-columns:minmax(0,1fr); }
      .image-creation-original { min-width:0; display:grid; gap:8px; }
      .image-creation-result .creative-picker-pixel { aspect-ratio:4/3; }
      .image-creation-result-actions { display:flex; align-items:center; flex-wrap:wrap; gap:10px; padding-top:12px; }
      .image-creation-empty { display:grid; place-content:center; min-height:240px; border:1px dashed var(--silver,#c9c9c9); text-align:center; padding:24px; }
      .image-creation-dialog > footer { display:flex; align-items:center; flex-wrap:wrap; padding:16px 24px; margin:0; gap:12px; border-top:1px solid var(--line,#ddd); background:var(--paper,#fff); }
      .image-creation-dialog [data-creation-notice] { flex:1; min-width:180px; margin:0; overflow-wrap:anywhere; }
      .image-creation-dialog [hidden] { display:none!important; }
      @media(max-width:760px) {
        .image-creation-dialog { width:100vw; height:100dvh; border-radius:0; border:0; }
        .image-creation-dialog > header,.image-creation-dialog > footer { padding:16px; }
        .image-creation-dialog .image-creation-layout { display:block; overflow:auto; }
        .image-creation-controls,.image-creation-results { overflow:visible; padding:18px 16px; }
        .image-creation-controls { border-right:0; border-bottom:1px solid var(--line,#ddd); }
        .image-creation-dialog [data-creation-notice] { flex-basis:100%; font-size:12px; }
        .image-creation-dialog > footer [data-creation-generate] { flex:1; }
        .image-creation-dialog textarea { min-height:200px; }
      }
    `;
    document.head.appendChild(style);
  }

  function previewAllowed(session, value) {
    if (typeof value !== "string" || !value.startsWith("/api/") || value.includes("\\")) return false;
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin
        && url.pathname.startsWith(`/api/formal/projects/${encodeURIComponent(session.projectId)}/`)
        && url.searchParams.get("workspace_id") === session.workspaceId;
    } catch { return false; }
  }

  async function showPreview(session, host, url, label, taskId = "") {
    session.previewStates ||= new Map();
    const valid = () => current(session) && host.isConnected;
    const status = (value) => {
      if (!valid() || !taskId) return;
      session.previewStates.set(taskId, { url, status: value });
      updateResultControls(session);
    };
    const fail = () => {
      if (!valid()) return;
      host.innerHTML = '图片未能加载' + (taskId ? '<button type="button" class="text-button" data-result-preview-retry>重试图片</button>' : '，请刷新图片记录');
      status('failed');
    };
    if (!previewAllowed(session, url)) { fail(); return; }
    if (session.previewStates.get(taskId)?.url !== url || session.previewStates.get(taskId)?.status !== 'ready') status('loading');
    host.textContent = "正在读取图片…";
    try {
      const blobUrl = await requestFormalPreview(url, { owner: session.previewOwner, variant: "original", priority: 1 });
      if (!valid()) return;
      const image = document.createElement("img");
      image.alt = label || "图片预览";
      image.decoding = "async";
      image.addEventListener("load", () => status(image.naturalWidth > 0 ? 'ready' : 'failed'), { once: true });
      image.addEventListener("error", fail, { once: true });
      image.src = blobUrl;
      host.replaceChildren(image);
    } catch { fail(); }
  }

  function resultEligible(session, item) {
    if (!item) return false;
    const preview = session.previewStates?.get(item.task_id);
    return sameTarget(session, item) && !session.adopted.has(item.task_id) && !item.result_uncertain
      && !runningStatuses.has(item.status) && preview?.url === item.preview_url && preview.status === 'ready';
  }

  function updateResultControls(session) {
    if (!current(session)) return;
    const locked = session.adopting || session.batchAdopting || session.submitting;
    session.dialog.querySelectorAll('[data-creation-task]').forEach(card => {
      const item = session.items.get(card.dataset.creationTask);
      const button = card.querySelector('[data-adopt-task]');
      if (button) button.disabled = locked || !resultEligible(session, item);
      const checkbox = card.querySelector('[data-result-select]');
      if (checkbox) checkbox.disabled = locked || session.adopted.has(item.task_id);
    });
    if (!session.batch) return;
    const selected = [...(session.resultSelected || [])].map(id => session.items.get(id)).filter(Boolean);
    const eligible = selected.filter(item => resultEligible(session, item));
    const count = session.dialog.querySelector('[data-result-count]');
    if (count) count.textContent = `已选 ${selected.length} 张 · 可采用 ${eligible.length} 张 · 待处理 ${selected.length - eligible.length} 张；每镜最多选一张`;
    const button = session.dialog.querySelector('[data-results-adopt]');
    if (button) {
      button.disabled = locked || !selected.length || eligible.length !== selected.length;
      button.textContent = session.batchAdopting ? '正在逐镜采用…' : session.deferSelection ? `更新所选 ${selected.length} 镜建议` : `采用所选 ${selected.length} 张`;
    }
    session.dialog.querySelectorAll('[data-results-select]').forEach(button => { button.disabled = locked; });
  }

  function selectResults(session, mode) {
    if (session.adopting || session.batchAdopting || session.submitting) return;
    session.resultSelected ||= new Set();
    session.resultSelected.clear();
    const targets = new Set();
    if (mode !== 'none') for (const item of [...session.items.values()].reverse()) {
      const target = JSON.stringify([item.target?.kind, item.target?.id]);
      if (!sameTarget(session, item) || session.adopted.has(item.task_id) || !item.preview_url || item.result_uncertain
        || runningStatuses.has(item.status) || targets.has(target) || (mode === 'eligible' && !resultEligible(session, item))) continue;
      targets.add(target); session.resultSelected.add(item.task_id);
    }
    session.dialog.querySelectorAll('[data-result-select]').forEach(input => { input.checked = session.resultSelected.has(input.dataset.resultSelect); });
    updateResultControls(session);
  }

  async function adoptSelected(session) {
    if (session.batchAdopting || session.adopting || session.submitting) return;
    const ids = [...(session.resultSelected || [])];
    if (!ids.length || ids.some(id => !resultEligible(session, session.items.get(id)))) return;
    session.batchAdopting = true; updateResultControls(session); updateControls(session);
    let saved = 0, pending = 0;
    try {
      for (const id of ids) {
        if (!current(session)) break;
        const result = await adopt(session, id, { batch: true });
        if (result === 'saved') { saved += 1; session.resultSelected.delete(id); }
        else pending += 1;
      }
    } finally {
      session.batchAdopting = false;
      if (current(session)) {
        renderResults(session);
        notice(session, `本次已采用 ${saved} 张 · 待处理 ${pending} 张 · 未处理 ${ids.length - saved - pending} 张。${session.deferSelection ? '图片已加入素材，请返回核对建议并应用所选。' : '选图进度已保存，仍需分析、核对并保存版本。'}${pending ? '问题与未成功项保留，请逐项查看后再处理。' : ''}`);
      }
    }
  }

  function updateControls(session) {
    if (!current(session)) return;
    updateResultControls(session);
    if (session.batch) { updateBatchControls(session); return; }
    const locked = session.submitting || session.adopting || session.batchAdopting || Boolean(session.pendingSubmission) || uncertain(session) || working(session);
    session.dialog.querySelectorAll("[data-creation-input]").forEach(element => { element.disabled = locked; });
    const generate = session.dialog.querySelector("[data-creation-generate]");
    const purpose = session.context?.capabilities?.purposes?.find(item => item.purpose === session.purpose);
    generate.disabled = !session.context?.available || locked || session.preparing || session.context?.prepared_prompt?.stale || !session.prompt.trim() || Boolean(purpose && session.prompt.length > purpose.prompt_limit);
    generate.textContent = session.submitting || working(session) ? "正在生成一张图片…" : `生成一张${purpose ? ` · ${purpose.size.replace("x", "×")}` : ""}`;
    session.dialog.querySelector("[data-creation-model]").textContent = purpose
      ? `${purpose.model} · ${purpose.size.replace("x", "×")} · ${session.prompt.length}/${purpose.prompt_limit} 字${session.prompt.length > purpose.prompt_limit ? "，请手动缩短，原文已保留" : ""}` : "正在读取可用模型…";
    const limit = session.dialog.querySelector('[data-creation-limit]');
    limit.hidden = !purpose || session.prompt.length <= purpose.prompt_limit;
    limit.textContent = purpose ? `此用途最多 ${purpose.prompt_limit} 字，当前 ${session.prompt.length} 字。请缩短描述，或选择其他用途。` : '';
    const retry = session.dialog.querySelector("[data-creation-retry]");
    retry.hidden = !session.pendingSubmission || session.pendingSubmission.resultUncertain;
    retry.disabled = session.submitting;
    session.dialog.querySelector("[data-creation-new-request]").hidden = !uncertain(session) || session.submitting;
    session.dialog.querySelector("[data-creation-availability]").textContent = session.context
      ? session.context.available ? '' : session.context.unavailable_reason || "图片生成暂不可用，请配置账号后重新读取。"
      : "正在读取当前目标与图片能力…";
    session.dialog.querySelector('[data-creation-availability]').hidden = Boolean(session.context?.available);
    renderPromptBasis(session);
  }

  function promptBasisHtml(context) {
    const draft = context?.prepared_prompt;
    if (!draft) return '<p role="status">正在整理本镜画面…</p>';
    const issue = draft.stale ? '原镜头或需求已变化。你的描述已保留，请重新整理，或核对后保留这份描述。' : draft.status === 'failed' ? draft.error_message : '';
    const posttext = (draft.postproduction_text || []).join('；');
    return `${issue ? `<p role="status">${escapeHtml(issue)}</p>` : ''}
      ${posttext ? `<div class="image-creation-posttext"><strong>后期添加的文字</strong><p>${escapeHtml(posttext)}</p></div>` : ''}
      <details data-prompt-source><summary>查看原镜头</summary><p>${escapeHtml(context.original_shot?.visual || context.original_shot?.画面描述 || '')}</p></details>`;
  }

  function renderPromptBasis(session) {
    const host = session.dialog?.querySelector('[data-prompt-basis]');
    if (host) {
      const expanded = host.querySelector('[data-prompt-source]')?.open;
      host.innerHTML = promptBasisHtml(session.context);
      if (expanded && host.querySelector('[data-prompt-source]')) host.querySelector('[data-prompt-source]').open = true;
    }
    const keep = session.dialog?.querySelector('[data-prompt-adopt-basis]');
    if (keep) keep.hidden = !session.context?.prepared_prompt?.stale;
    const save = session.dialog?.querySelector('[data-prompt-save]');
    if (save) { save.disabled = session.preparing || !session.prompt?.trim() || !session.promptEdited; save.textContent = session.promptEdited ? '保存描述' : '已保存'; }
    const regenerate = session.dialog?.querySelector('[data-prompt-regenerate]');
    if (regenerate) { regenerate.disabled = session.preparing; regenerate.textContent = session.preparing ? '正在整理…' : '按原镜头重新整理'; }
  }

  async function preparePrompt(session, target = session, changes = {}) {
    const previous = target.prompt;
    target.preparing = true; updateControls(session);
    try {
      const context = await api(path(session, '/prepare'), {method: 'POST', body: JSON.stringify({
        target_kind: target.targetKind, target_id: target.targetId,
        additional_instruction: target.additionalInstruction || '',
        expected_prompt_sha256: target.context?.prepared_prompt?.sha256 || null, ...changes})});
      target.context = context;
      if (previous === target.prompt || changes.regenerate || changes.use_saved_prompt) {
        target.prompt = context.suggested_prompt || ''; target.promptEdited = false;
      }
      if (!context.prepared_prompt?.stale) target.visibleBasis = basis(context);
      if (current(session)) {
        if (session.batch) renderBatchTargets(session);
        else { session.dialog.querySelector('[data-creation-prompt]').value = target.prompt; renderPromptBasis(session); }
      }
      return context;
    } finally { target.preparing = false; updateControls(session); }
  }

  async function savePrompt(session, target = session) {
    if (target.savePromise) await target.savePromise;
    if (!target.promptEdited && target.context?.prepared_prompt) return;
    const value = target.prompt.trim();
    if (!value) return;
    target.savePromise = preparePrompt(session, target, {edited_prompt: value});
    try { await target.savePromise; } finally { target.savePromise = null; }
  }

  function renderReferences(session) {
    if (!current(session)) return;
    const select = session.dialog.querySelector("[data-creation-purpose]");
    select.innerHTML = (session.context?.capabilities?.purposes || []).map(item =>
      `<option value="${escapeHtml(item.purpose)}">${escapeHtml(item.label)}</option>`).join("");
    select.value = session.purpose || "storyboard";
    updateControls(session);
  }

  function sameTarget(session, item) {
    if (session.batch) return session.targets.some(target => target.targetKind === item.target?.kind && target.targetId === item.target?.id);
    return item.target?.kind === session.targetKind && item.target?.id === session.targetId;
  }

  function putItem(session, item) {
    if (!item?.task_id) return;
    const previous = session.items.get(item.task_id);
    if (previous && !runningStatuses.has(previous.status) && runningStatuses.has(item.status)) return;
    session.items.set(item.task_id, item);
  }

  function renderResults(session) {
    if (!current(session)) return;
    const host = session.dialog.querySelector("[data-creation-results]");
    host.replaceChildren();
    if (session.batch) host.innerHTML = '<div class="image-creation-result-actions"><button type="button" class="text-button" data-results-select="all">全选最新结果</button><button type="button" class="text-button" data-results-select="none">清空</button><button type="button" class="text-button" data-results-select="eligible">仅选可采用</button><button type="button" class="primary-button" data-results-adopt>采用所选</button></div><p data-result-count role="status"></p>';
    const allItems = [...session.items.values()].reverse();
    const items = allItems.filter(item => sameTarget(session, item) || (!session.batch && session.showAllHistory));
    if (!session.batch) host.innerHTML = `<div class="image-creation-result-heading"><h3>${session.showAllHistory ? '项目图片记录' : '本镜生成结果'}</h3>${allItems.some(item => !sameTarget(session, item)) ? `<button type="button" class="text-button" data-creation-history>${session.showAllHistory ? '返回本镜' : '查看项目记录'}</button>` : ''}</div>`;
    if (!items.length) host.insertAdjacentHTML('beforeend', '<div class="image-creation-empty"><h3>本镜还没有生成图片</h3><p class="image-creation-muted">核对画面描述后，点击“生成一张”。<br>生成结果会显示在这里。</p></div>');
    for (const item of items) {
      const article = document.createElement("article");
      article.className = "image-creation-result";
      article.dataset.creationTask = item.task_id;
      const isRunning = runningStatuses.has(item.status);
      const ready = Boolean(item.preview_url) && !isRunning && !item.result_uncertain;
      const adopted = session.adopted.has(item.task_id);
      const status = item.result_uncertain ? "结果待核实" : isRunning ? "正在生成" : ready ? "生成结果" : "生成未完成";
      article.innerHTML = `<header><div><h3>${escapeHtml(item.target?.label || "当前画面")} · ${escapeHtml(status)}</h3><span>${escapeHtml(item.model_label || item.origin_label || "")}</span></div><button type="button" class="text-button" data-refresh-task>重新读取</button></header>`;
      if (ready) {
        const refs = item.reference_previews || [];
        const comparison = document.createElement("div");
        comparison.className = `image-creation-compare${refs.length ? "" : " no-references"}`;
        if (refs.length) {
          const originals = document.createElement("div"); originals.className = "image-creation-original";
          refs.forEach(reference => {
            const figure = document.createElement("figure");
            figure.innerHTML = `<div class="creative-picker-pixel"></div><figcaption>参考 · ${escapeHtml(reference.label || "原图")}</figcaption>`;
            originals.appendChild(figure);
            void showPreview(session, figure.querySelector(".creative-picker-pixel"), reference.preview_url, reference.label || "参考原图");
          });
          comparison.appendChild(originals);
        }
        const result = document.createElement("figure");
        result.innerHTML = '<div class="creative-picker-pixel" data-creation-result-preview></div><figcaption>本次生成 · 单张</figcaption>';
        comparison.appendChild(result); article.appendChild(comparison);
        // Start previews after insertion, so shared preview-cache hits remain visible.
        queueMicrotask(() => { if (current(session)) void showPreview(session, result.querySelector(".creative-picker-pixel"), item.preview_url, "生成结果原图", item.task_id); });
        article.insertAdjacentHTML("beforeend", `<div class="image-creation-result-actions">${sameTarget(session, item) ? `${session.batch ? `<label><input type="checkbox" data-result-select="${escapeHtml(item.task_id)}"${session.resultSelected?.has(item.task_id) ? " checked" : ""}>选择此结果</label>` : ""}<button type="button" class="primary-button compact" data-adopt-task${adopted || session.adopting ? " disabled" : ""}>${adopted ? (session.deferSelection ? "已加入素材" : "已采用") : session.adoptReviews?.has(item.task_id) ? "按最新进度采用" : session.adoptRequests.has(item.task_id) ? "重试采用回执" : session.targetKind === "creative_block" ? "采用为本段参考图" : session.deferSelection ? "加入素材并更新本镜建议" : "采用并保存选图进度"}</button>` : ""}</div>`);
      } else {
        const message = document.createElement("p");
        message.textContent = item.result_uncertain
          ? "生成请求可能已经完成，请重新读取任务核实结果；当前不会再次发起生成。"
          : item.error_message || (isRunning ? "图片正在生成，可以关闭此窗口，稍后再查看。" : "本次没有可用结果，原输入仍保留。可调整后再次生成。");
        article.appendChild(message);
      }
      if (session.adoptionIssues?.has(item.task_id)) article.insertAdjacentHTML("beforeend", `<p role="status">${escapeHtml(session.adoptionIssues.get(item.task_id))}</p>`);
      host.appendChild(article);
    }
    updateControls(session); updateResultControls(session);
  }

  async function readContext(session, { initial = false } = {}) {
    const serial = ++session.contextSerial;
    let context = await api(`${path(session, "/context")}&target_kind=${encodeURIComponent(session.targetKind)}&target_id=${encodeURIComponent(session.targetId)}`);
    if (serial !== session.contextSerial) return context;
    if (context.target?.kind !== session.targetKind || context.target?.id !== session.targetId) throw new Error("图片目标已变化，请重新打开当前镜头");
    session.context = context;
    if (!context.prepared_prompt || (session.additionalInstruction && context.prepared_prompt.additional_instruction !== session.additionalInstruction)) {
      context = await preparePrompt(session);
    }
    if (!session.purpose) session.purpose = context.suggested_purpose || "storyboard";
    if (initial || !session.visibleBasis) session.visibleBasis = basis(context);
    if (!session.promptEdited && !session.prompt) session.prompt = context.suggested_prompt || "";
    for (const item of [...(context.items || [])].reverse()) putItem(session, item);
    if (current(session)) {
      session.dialog.querySelector("[data-creation-target]").textContent = context.target.label || "当前画面";
      const prompt = session.dialog.querySelector("[data-creation-prompt]");
      if (prompt.value !== session.prompt) prompt.value = session.prompt;
      renderReferences(session); renderResults(session); schedulePoll(session);
    }
    return context;
  }

  function schedulePoll(session) {
    clearTimeout(session.timer);
    if (!current(session) || uncertain(session) || !working(session)) return;
    session.timer = setTimeout(async () => {
      const items = [...session.items.values()].filter(item => runningStatuses.has(item.status));
      try {
        const responses = await Promise.all(items.map(item => api(path(session, `/${encodeURIComponent(item.task_id)}`))));
        responses.forEach(item => putItem(session, item));
        if (!current(session)) return;
        renderResults(session); schedulePoll(session);
      } catch {
        if (current(session)) notice(session, "任务状态暂未读到，原输入仍保留。请点击“刷新图片记录”继续查看；不会重新生成。");
      }
    }, 2000);
  }

  async function generate(session, retry = false) {
    if (!current(session) || session.submitting || uncertain(session)) return;
    if (!retry && (!session.context?.available || working(session) || session.pendingSubmission)) return;
    if (!retry) {
      if (!session.prompt.trim()) { notice(session, "请先填写画面要求。"); return; }
      session.submitting = true; updateControls(session);
      try { await savePrompt(session); } catch (error) { session.submitting = false; updateControls(session); notice(session, `${errorMessage(error)} 请保存画面稿后再生成。`); return; }
      const payload = { expected_project_version: session.context.project_version,
        target_kind: session.targetKind, target_id: session.targetId, prompt: session.prompt.trim(),
        operation_key: key("image-create"), purpose: session.purpose,
        expected_prompt_sha256: session.context.prepared_prompt?.sha256 || null,
        expected_script_revision: session.context.script_revision ?? null,
        expected_target_basis: session.visibleBasis?.target_basis ?? null,
        expected_creative_revision: session.targetKind === "creative_block" ? session.context.creative_revision ?? null : null };
      session.pendingSubmission = { json: JSON.stringify(payload), files: [], resultUncertain: false };
    }
    if (!session.pendingSubmission) return;
    session.submitting = true; updateControls(session); notice(session, "正在提交本次单张生成请求…");
    const request = session.pendingSubmission;
    let body = request.json;
    if (request.files.length) {
      body = new FormData(); body.append("payload", request.json);
      request.files.forEach(file => body.append("reference_files", file, file.name));
    }
    try {
      const item = await api(path(session), { method: "POST", body });
      if (!item?.task_id) { request.resultUncertain = true; throw new Error("未取得生成回执"); }
      putItem(session, item); session.pendingSubmission = null;
      notice(session, item.result_uncertain ? "生成结果待核实，请重新读取任务。不会自动再次生成。" : "已提交一张图片。结果先作为参考，采用后仍需分析和核对。");
      renderResults(session); schedulePoll(session);
    } catch (error) {
      request.resultUncertain = request.resultUncertain || Boolean(error.payload?.detail?.result_uncertain || error.payload?.result_uncertain);
      if (error.status && error.status < 500 && !request.resultUncertain) session.pendingSubmission = null;
      notice(session, session.pendingSubmission
        ? request.resultUncertain ? "请求结果暂时无法确定。请刷新图片记录核实，不会再次发起生成。"
          : "未收到生成回执，原输入与请求已保留。可先刷新记录，或点击“重试原请求”读取同一次操作的结果。"
        : `${errorMessage(error)} 原输入仍保留。`);
    } finally { session.submitting = false; updateControls(session); }
  }

  async function adopt(session, taskId, { batch = false } = {}) {
    if (!current(session) || session.adopting || session.submitting || (!batch && session.batchAdopting) || session.adopted.has(taskId)) return;
    const item = session.items.get(taskId);
    if (!item || !resultEligible(session, item)) return;
    session.adoptReviews ||= new Set();
    session.adoptionIssues ||= new Map();
    session.adopting = true; renderResults(session);
    const target = session.batch ? session.targets.find(target => target.targetId === item.target.id && target.targetKind === item.target.kind) : session;
    try {
      let request = session.adoptRequests.get(taskId);
      if (!request) {
        const latest = session.batch ? await api(`${path(session, "/context")}&target_kind=${encodeURIComponent(target.targetKind)}&target_id=${encodeURIComponent(target.targetId)}`) : await readContext(session);
        if (!current(session)) return;
        const latestBasis = basis(latest);
        if (JSON.stringify(latestBasis) !== JSON.stringify(target.visibleBasis)) {
          target.visibleBasis = latestBasis; session.adoptReviews.add(taskId);
          session.adoptionIssues.set(taskId, "目标已有更新，请核对画面后按最新进度采用。");
          notice(session, `“${latest.target.label || "当前目标"}”已有更新，当前画面要求：${latest.suggested_prompt}。请核对本次图片，再点击“按最新进度采用”。`);
          return;
        }
        request = JSON.stringify({ expected_project_version: latest.project_version,
          defer_selection: Boolean(session.deferSelection),
          expected_creative_revision: target.targetKind === "creative_block" ? latest.creative_revision ?? null : null,
          expected_progress_revision: target.targetKind === "formal_shot" ? latest.progress_revision ?? null : null,
          expected_script_revision: target.targetKind === "formal_shot" ? latest.script_revision ?? null : null,
          acknowledged_target_basis: session.adoptReviews.has(taskId) ? latest.target_basis : null,
          target_kind: target.targetKind, target_id: target.targetId, operation_key: key("image-adopt") });
        session.adoptRequests.set(taskId, request);
      }
      const result = await api(path(session, `/${encodeURIComponent(taskId)}/adopt`), { method: "POST", body: request });
      if (result?.status !== (target.targetKind === "creative_block" ? "reference_saved" : session.deferSelection ? "pool_saved" : "progress_saved")) throw new Error("采用回执暂未确认");
      session.adopted.add(taskId); session.adoptRequests.delete(taskId); session.adoptReviews.delete(taskId); session.adoptionIssues.delete(taskId);
      notice(session, result.message || "已采用为参考，仍需分析与核对；没有自动标记人工通过。");
      try { await session.onAdopt?.(result); }
      catch { session.adoptionIssues.set(taskId, "图片已加入素材，但工作台暂未刷新，请返回核对建议或当前稿，无需再次采用。"); notice(session, session.adoptionIssues.get(taskId)); }
      target.visibleBasis = null;
      session.resultSelected?.delete(taskId);
      return 'saved';
    } catch (error) {
      if (error.status && error.status < 500) session.adoptRequests.delete(taskId);
      session.adoptionIssues.set(taskId, errorMessage(error));
      notice(session, `${errorMessage(error)} 图片和原输入仍保留。`);
    } finally { session.adopting = false; renderResults(session); }
  }

  function targetIssue(session, target) {
    if (!target.context) return target.contextError || '正在读取生成条件';
    if (!target.context.available) return target.context.unavailable_reason || '当前不可生成';
    if (target.preparing) return '正在保存画面稿';
    if (target.context.prepared_prompt?.stale) return '画面依据已变化，请核对并采用当前依据';
    if ([...session.items.values()].some(item => item.target?.id === target.targetId && item.target?.kind === target.targetKind && (runningStatuses.has(item.status) || item.result_uncertain))) return '已有任务未完成或结果待核实，请刷新图片记录';
    if (!target.prompt?.trim()) return '请填写画面要求';
    const limit = target.context?.capabilities?.purposes?.find(p => p.purpose === target.purpose)?.prompt_limit || 3600;
    if (target.prompt.length > limit) return `画面要求超出 ${limit} 字，请手动缩短`;
    return '';
  }

  function updateBatchControls(session) {
    if (!current(session)) return;
    const chosen = session.targets.filter(target => target.selected);
    const unit = session.targets.every(target => target.targetKind === 'creative_block') ? '段' : '镜';
    const invalid = chosen.filter(target => targetIssue(session, target));
    const locked = session.submitting || session.adopting || session.batchAdopting || Boolean(session.pendingSubmission);
    session.dialog.querySelectorAll('[data-batch-input], [data-targets-select]').forEach(input => { input.disabled = locked; });
    const button = session.dialog.querySelector('[data-batch-generate]');
    button.disabled = locked || !chosen.length || invalid.length > 0;
    button.textContent = session.submitting ? '正在依次生成…' : `生成选中的 ${chosen.length} ${unit}`;
    const count = session.dialog.querySelector('[data-target-count]');
    if (count) count.textContent = `已选 ${chosen.length} ${unit} · 可生成 ${chosen.length - invalid.length} ${unit} · 待处理 ${invalid.length} ${unit}${invalid.length ? '；可选择“仅选可生成”调整范围后提交' : ''}`;
    session.targets.forEach((target, index) => {
      const issue = session.dialog.querySelector(`[data-target-issue="${index}"]`);
      if (issue) issue.textContent = targetIssue(session, target);
    });
    session.dialog.querySelector('[data-batch-retry]').hidden = !session.pendingSubmission;
    session.dialog.querySelector('[data-batch-retry]').disabled = session.submitting;
  }

  async function generateBatch(session, retry = false) {
    if (session.submitting || session.adopting || session.batchAdopting || !current(session) || (!retry && session.pendingSubmission)) return;
    const selected = retry && session.pendingSubmission
      ? [session.targets.find(target => target.targetId === session.pendingSubmission.targetId)]
      : session.targets.filter(target => target.selected);
    if (!selected.length) return;
    if (!retry && selected.some(target => targetIssue(session, target))) { notice(session, "所选镜头有待处理项，请调整范围后生成。"); return; }
    session.submitting = true; updateControls(session);
    try {
      for (const target of selected) {
        let pending = session.pendingSubmission;
        if (!pending) {
          await savePrompt(session, target);
          const c = target.context;
          pending = { targetId: target.targetId, json: JSON.stringify({ expected_project_version: c.project_version,
            expected_script_revision: target.targetKind === 'formal_shot' ? c.script_revision : null,
            expected_creative_revision: target.targetKind === 'creative_block' ? c.creative_revision : null, expected_target_basis: target.visibleBasis?.target_basis,
            target_kind: target.targetKind, target_id: target.targetId, prompt: target.prompt.trim(),
            expected_prompt_sha256: c.prepared_prompt?.sha256 || null,
            purpose: target.purpose, operation_key: key('batch-image') }) };
          session.pendingSubmission = pending;
        }
        notice(session, `${target.context?.target?.label || target.targetId}：正在提交一张图片。`);
        let item;
        try {
          item = await api(path(session), { method: 'POST', body: pending.json });
          if (!item?.task_id) throw new Error('生成回执暂未读到');
        } catch (error) {
          if (error.status && error.status < 500) session.pendingSubmission = null;
          notice(session, `${errorMessage(error)} 已完成项和原请求保留；未提交的镜头仍在列表中。`);
          break;
        }
        session.pendingSubmission = null; target.selected = false; putItem(session, item); renderResults(session);
        const checkbox = session.dialog?.querySelector(`[data-batch-select="${session.targets.indexOf(target)}"]`);
        if (checkbox) checkbox.checked = false;
        while (runningStatuses.has(item.status)) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          try { item = await api(path(session, `/${encodeURIComponent(item.task_id)}`)); }
          catch { notice(session, '本次任务状态暂未读到，已保留原任务；请刷新图片记录。'); break; }
          putItem(session, item); renderResults(session);
        }
        notice(session, `${target.context?.target?.label || target.targetId}：${item.preview_url ? '图片已完成，可采用' : item.error_message || '原任务已保留，请重新读取'}。`);
        if (!current(session)) break;
      }
    } finally { session.submitting = false; updateControls(session); }
  }

  async function openBatch(options) {
    if (!options.projectId || !options.workspaceId || !options.targets?.length) throw new Error('请先选择需要生成的镜头');
    if (active) close(active);
    installStyles();
    const sessionKey = JSON.stringify(['batch', options.workspaceId, options.projectId, options.proposalTaskId || '', options.targets.map(target => [target.targetKind || options.targetKind || 'formal_shot', target.targetId])]);
    let session = sessions.get(sessionKey);
    if (!session) {
      session = { ...options, batch: true, targets: options.targets.map(target => ({ ...target, targetKind: target.targetKind || options.targetKind || 'formal_shot', selected: true })),
        items: new Map(), adopted: new Set(), adoptRequests: new Map(), acknowledgedUncertain: new Set(), localUrls: [],
        previewOwner: key('batch-preview'), message: '先核对缺口与每镜用途，默认请求无水印新图；收到结果后仍须核对水印与画面。只有点击生成才会发起请求。' };
      sessions.set(sessionKey, session);
    }
    session.deferSelection = Boolean(options.deferSelection);
    session.onAdopt = options.onAdopt; session.opener = document.activeElement; active = session;
    session.hadPickerOpen = document.body.classList.contains('creative-picker-open');
    document.body.classList.add('creative-picker-open');
    const dialog = session.dialog = document.createElement('dialog');
    dialog.className = 'creative-picker image-creation-dialog'; dialog.setAttribute('aria-label', '缺口生成与集中取舍');
    dialog.innerHTML = `<header><div><span>范围已固定 · ${session.targets.length} 镜</span><h2>缺口生成与集中取舍</h2></div><button type="button" class="secondary-button compact" data-creation-close>关闭</button></header>
      <div class="image-creation-layout"><section class="image-creation-controls"><div class="image-creation-result-actions"><button type="button" class="text-button" data-targets-select="all">全选</button><button type="button" class="text-button" data-targets-select="none">清空</button><button type="button" class="text-button" data-targets-select="eligible">仅选可生成</button></div><p data-target-count role="status"></p><div data-batch-targets>正在读取已保存工作稿…</div><button type="button" class="primary-button" data-batch-generate disabled>生成选中的镜头</button><button type="button" class="secondary-button" data-batch-retry hidden>读取原请求回执</button></section><section class="image-creation-results" data-creation-results aria-label="各镜生成结果"></section></div>
      <footer><p data-creation-notice role="status" aria-live="polite"></p><button type="button" class="text-button" data-batch-refresh>刷新图片记录</button></footer>`;
    document.body.appendChild(dialog); dialog.showModal(); notice(session, session.message);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(session); });
    dialog.addEventListener('input', event => {
      if (event.target.matches('[data-batch-prompt]')) {
        const target = session.targets[Number(event.target.dataset.batchPrompt)];
        target.prompt = event.target.value; target.promptEdited = true;
      }
      updateControls(session);
    });
    dialog.addEventListener('change', event => {
      const input = event.target;
      if (input.matches('[data-result-select]')) {
        session.resultSelected ||= new Set();
        const item = session.items.get(input.dataset.resultSelect);
        if (input.checked) {
          for (const id of session.resultSelected) if (session.items.get(id)?.target?.id === item.target.id) session.resultSelected.delete(id);
          session.resultSelected.add(item.task_id);
        } else session.resultSelected.delete(item.task_id);
        dialog.querySelectorAll('[data-result-select]').forEach(box => { box.checked = session.resultSelected.has(box.dataset.resultSelect); });
        updateResultControls(session);
      }
      if (input.matches('[data-batch-select]')) session.targets[Number(input.dataset.batchSelect)].selected = input.checked;
      if (input.matches('[data-batch-purpose]')) session.targets[Number(input.dataset.batchPurpose)].purpose = input.value;
      updateControls(session);
    });
    dialog.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (!button) return;
      if (button.matches('[data-creation-close]')) close(session);
      else if (button.matches('[data-targets-select]')) {
        if (session.submitting || session.adopting || session.batchAdopting || session.pendingSubmission) return;
        const mode = button.dataset.targetsSelect;
        session.targets.forEach(target => { target.selected = mode === 'all' || mode === 'eligible' && !targetIssue(session, target); });
        renderBatchTargets(session);
      }
      else if (button.matches('[data-results-select]')) selectResults(session, button.dataset.resultsSelect);
      else if (button.matches('[data-results-adopt]')) void adoptSelected(session);
      else if (button.matches('[data-result-preview-retry]')) {
        const card = button.closest('[data-creation-task]'), item = session.items.get(card.dataset.creationTask);
        void showPreview(session, card.querySelector('[data-creation-result-preview]'), item.preview_url, '生成结果原图', item.task_id);
      }
      else if (button.matches('[data-batch-generate]')) void generateBatch(session);
      else if (button.matches('[data-batch-retry]')) void generateBatch(session, true);
      else if (button.matches('[data-adopt-task]')) void adopt(session, button.closest('[data-creation-task]').dataset.creationTask);
      else if (button.matches('[data-refresh-task]')) {
        const taskId = button.closest('[data-creation-task]').dataset.creationTask;
        void api(path(session, `/${encodeURIComponent(taskId)}`)).then(item => { putItem(session, item); renderResults(session); })
          .catch(error => notice(session, errorMessage(error)));
      } else if (button.matches('[data-batch-prompt-save]')) {
        void savePrompt(session, session.targets[Number(button.dataset.batchPromptSave)]).catch(error => notice(session, errorMessage(error)));
      } else if (button.matches('[data-batch-basis-update], [data-batch-basis-keep]')) {
        const target = session.targets[Number(button.dataset.batchBasisUpdate ?? button.dataset.batchBasisKeep)];
        void preparePrompt(session, target, button.matches('[data-batch-basis-keep]') ? {edited_prompt: target.prompt, use_saved_prompt: true} : {regenerate: true})
          .catch(error => notice(session, errorMessage(error)));
      } else if (button.matches('[data-batch-refresh]')) void refreshBatch(session);
    });
    await refreshBatch(session);
  }

  async function refreshBatch(session) {
    for (const target of session.targets) {
      try {
        let context = await api(`${path(session, '/context')}&target_kind=${encodeURIComponent(target.targetKind)}&target_id=${encodeURIComponent(target.targetId)}`);
        if (context.target?.id !== target.targetId || context.target?.kind !== target.targetKind) throw new Error('生成目标已变化，请重新打开');
        target.context = context; target.contextError = '';
        if (!context.prepared_prompt) context = await preparePrompt(session, target);
        if (!target.visibleBasis) target.visibleBasis = basis(context);
        if (!target.prompt) target.prompt = context.suggested_prompt || '';
        if (!target.purpose) target.purpose = context.suggested_purpose || 'storyboard';
        for (const item of context.items || []) if (sameTarget(session, item)) putItem(session, item);
      } catch (error) {
        target.context = null; target.contextError = errorMessage(error);
      }
    }
    if (!current(session)) return;
    renderBatchTargets(session); renderResults(session);
  }

  function renderBatchTargets(session) {
    if (!current(session)) return;
    const host = session.dialog.querySelector('[data-batch-targets]');
      host.innerHTML = session.targets.map((target, index) => `<article class="image-creation-result"><label><input type="checkbox" data-batch-input data-batch-select="${index}"${target.selected ? ' checked' : ''}>${escapeHtml(target.context?.target?.label || target.targetId)}</label>
        <label>用途与模型<select data-batch-input data-batch-purpose="${index}">${(target.context?.capabilities?.purposes || []).map(p => `<option value="${escapeHtml(p.purpose)}"${p.purpose === target.purpose ? ' selected' : ''}>${escapeHtml(p.label)} · ${escapeHtml(p.model)} · ${escapeHtml(p.size)}</option>`).join('')}</select></label>
        <details open><summary>静态画面提示词</summary><textarea data-batch-input data-batch-prompt="${index}" maxlength="3600">${escapeHtml(target.prompt || '')}</textarea></details><button type="button" data-batch-input data-batch-prompt-save="${index}">${target.promptEdited ? '保存画面稿' : '画面稿已保存'}</button>${promptBasisHtml(target.context)}<button type="button" data-batch-input data-batch-basis-update="${index}">从当前镜头重新提炼</button>${target.context?.prepared_prompt?.stale ? `<button type="button" data-batch-input data-batch-basis-keep="${index}">保留修改并采用当前依据</button>` : ''}<p data-target-issue="${index}" role="status"></p></article>`).join('');
    updateControls(session);
  }

  function close(session) {
    if (!current(session)) return;
    clearTimeout(session.timer);
    session.localUrls.forEach(value => URL.revokeObjectURL(value)); session.localUrls = [];
    session.dialog.close(); session.dialog.remove(); session.dialog = null;
    if (!session.hadPickerOpen) document.body.classList.remove("creative-picker-open");
    active = null;
    session.opener?.isConnected && session.opener.focus({ preventScroll: true });
  }

  async function open(options) {
    if (!["creative_block", "formal_shot"].includes(options?.targetKind) || !options.projectId || !options.workspaceId || !options.targetId) throw new Error("请先选择生成图片的目标");
    if (active) close(active);
    installStyles();
    const sessionKey = JSON.stringify([options.workspaceId, options.projectId, options.targetKind, options.targetId]);
    let session = sessions.get(sessionKey);
    if (!session) {
      session = { ...options, prompt: "", promptEdited: false, localUrls: [],
        items: new Map(), acknowledgedUncertain: new Set(), adopted: new Set(), adoptRequests: new Map(), contextSerial: 0, message: "生成后可预览，核对后采用为本镜参考图。", previewOwner: key("image-creation-preview") };
      sessions.set(sessionKey, session);
    }
    if (options.prompt && !session.pendingSubmission) { session.prompt = options.prompt; session.promptEdited = true; }
    session.additionalInstruction = options.additionalInstruction || '';
    session.showAllHistory = false;
    session.deferSelection = Boolean(options.deferSelection);
    session.onAdopt = options.onAdopt; session.opener = document.activeElement; active = session;
    session.hadPickerOpen = document.body.classList.contains("creative-picker-open");
    document.body.classList.add("creative-picker-open");
    const dialog = document.createElement("dialog"); session.dialog = dialog;
    dialog.className = "creative-picker image-creation-dialog";
    dialog.setAttribute("aria-labelledby", "image-creation-title");
    dialog.innerHTML = `<header><div><span data-creation-target>当前画面</span><h2 id="image-creation-title">生成新图</h2></div><button type="button" class="secondary-button compact" data-creation-close aria-label="关闭图片生成">关闭</button></header>
      <div class="image-creation-layout"><form id="image-creation-form" class="image-creation-controls" data-creation-form>
        <p class="creative-picker-basis" data-creation-availability></p>
        <label for="image-creation-prompt"><strong>画面描述</strong><textarea id="image-creation-prompt" data-creation-input data-creation-prompt maxlength="3600" placeholder="描述这一张图中的主体、场景、构图与光线。" required></textarea></label>
        <div class="image-creation-prompt-actions"><button type="button" class="secondary-button compact" data-prompt-save>保存描述</button><button type="button" class="text-button" data-prompt-regenerate>按原镜头重新整理</button><button type="button" class="secondary-button compact" data-prompt-adopt-basis hidden>保留描述并确认依据</button></div>
        <p data-creation-limit role="status" hidden></p><div data-prompt-basis></div>
        <label>生成用途<select data-creation-input data-creation-purpose aria-label="生成用途"></select></label><details><summary>模型与尺寸</summary><p class="image-creation-muted" data-creation-model></p></details>
      </form><section class="image-creation-results" data-creation-results aria-label="图片结果与原图对照"></section></div>
      <footer><p data-creation-notice role="status" aria-live="polite"></p><button type="button" class="secondary-button compact" data-creation-new-request hidden>保留未知任务，另写新请求</button><button type="button" class="text-button" data-creation-refresh>刷新图片记录</button><button type="button" class="secondary-button" data-creation-retry hidden>重试原请求</button><button type="submit" form="image-creation-form" class="primary-button" data-creation-generate disabled>生成一张</button></footer>`;
    document.body.appendChild(dialog); dialog.showModal();
    dialog.querySelector("[data-creation-prompt]").value = session.prompt;
    notice(session, session.message); renderReferences(session); renderResults(session);
    dialog.addEventListener("cancel", event => { event.preventDefault(); close(session); });
    dialog.addEventListener("submit", event => { event.preventDefault(); void generate(session); });
    dialog.addEventListener("input", event => {
      if (event.target.matches("[data-creation-prompt]")) { session.prompt = event.target.value; session.promptEdited = true; updateControls(session); }
    });
    dialog.addEventListener("change", event => {
      const input = event.target;
      if (input.matches("[data-creation-purpose]")) { session.purpose = input.value; updateControls(session); }

    });
    dialog.addEventListener("click", event => {
      const button = event.target.closest("button");
      if (!button) return;
      if (button.matches("[data-creation-close]")) close(session);
      else if (button.matches('[data-creation-history]')) { session.showAllHistory = !session.showAllHistory; renderResults(session); }
      else if (button.matches('[data-prompt-save]')) void savePrompt(session).catch(error => notice(session, errorMessage(error)));
      else if (button.matches('[data-prompt-regenerate], [data-prompt-adopt-basis]')) {
        void preparePrompt(session, session, button.matches('[data-prompt-adopt-basis]') ? {edited_prompt: session.prompt, use_saved_prompt: true} : {regenerate: true})
          .catch(error => notice(session, errorMessage(error)));
      }
      else if (button.matches("[data-creation-new-request]")) {
        for (const item of session.items.values()) if (item.result_uncertain) session.acknowledgedUncertain.add(item.task_id);
        session.pendingSubmission = null; session.prompt = ""; session.promptEdited = true;

        const input = dialog.querySelector("[data-creation-prompt]"); input.value = "";
        notice(session, "原任务结果仍未知并保留记录。请另写本次画面要求；点击生成会提交一个新的图片请求。");
        renderReferences(session); updateControls(session); input.focus();
      }
      else if (button.matches("[data-creation-retry]")) void generate(session, true);
      else if (button.matches('[data-result-preview-retry]')) {
        const card = button.closest('[data-creation-task]'), item = session.items.get(card.dataset.creationTask);
        void showPreview(session, card.querySelector('[data-creation-result-preview]'), item.preview_url, '生成结果原图', item.task_id);
      }
      else if (button.matches("[data-adopt-task]")) void adopt(session, button.closest("[data-creation-task]").dataset.creationTask);
      else if (button.matches("[data-refresh-task]")) {
        const id = button.closest("[data-creation-task]").dataset.creationTask;
        button.disabled = true;
        void api(path(session, `/${encodeURIComponent(id)}`)).then(item => { putItem(session, item); renderResults(session); schedulePoll(session); })
          .catch(error => notice(session, `${errorMessage(error)} 请稍后重新读取任务。`))
          .finally(() => { if (button.isConnected) button.disabled = false; });
      } else if (button.matches("[data-creation-refresh]")) {
        button.disabled = true;
        void readContext(session).catch(error => notice(session, `${errorMessage(error)} 原输入仍保留。`))
          .finally(() => { if (button.isConnected) button.disabled = false; });
      }
    });
    try { await readContext(session, { initial: true }); }
    catch (error) { if (current(session)) notice(session, `${errorMessage(error)} 请重新读取图片记录。`); }
  }

  window.MPlusImageCreation = { open, openBatch };
})();
