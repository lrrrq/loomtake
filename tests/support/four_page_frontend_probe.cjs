const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};
function harness(page = 'shot-images') {
  const nodes = new Map();
  function node(key) {
    if (!nodes.has(key)) nodes.set(key, {value: '', files: [], textContent: '', innerHTML: '', dataset: {}, hidden: false, disabled: false, checked: false, isConnected: true, classList: {add: noop, remove: noop, toggle: noop}, querySelector: node, querySelectorAll: () => [], setAttribute: noop, removeAttribute: noop, replaceChildren: noop, addEventListener: noop, focus: noop, scrollIntoView: noop, reportValidity: () => true});
    return nodes.get(key);
  }
  const storage = {getItem: () => null, setItem: noop, removeItem: noop};
  const ctx = vm.createContext({URL, URLSearchParams, Headers, FormData, Blob, console, Map, Set, Date, JSON, localStorage: storage, sessionStorage: storage, document: {querySelector: node, getElementById: node, querySelectorAll: () => [], createElement: () => ({}), addEventListener: noop}, window: {location: {pathname: `/projects/a/${page}`, search: '', origin: 'http://localhost'}, addEventListener: noop, setTimeout: () => 1, clearTimeout: noop}, setTimeout: () => 1, clearTimeout: noop});
  vm.runInContext(source.slice(0, source.indexOf('\nelements.formalOwnerConfirm.addEventListener')), ctx);
  ctx.state = vm.runInContext('state', ctx); ctx.elements = vm.runInContext('elements', ctx);
  ctx.syncCreativePrimaryAction=noop;ctx.syncCreativeShotVisibility=noop;ctx.renderCreativeOutline=noop;ctx.syncCreativeNextAction=noop;ctx.syncCreativeVisibility=noop;ctx.creativeState().imageMode='all';ctx.focusFormalImageShot=noop;
  ctx.state.agentWorkspaceId = 'w'; ctx.state.agentAuth = 'auth'; ctx.state.formalBriefAvailable = true;
  ctx.showToast = noop;
  return {ctx, node};
}
function imageHarness() {
  const h = harness(); const {ctx, node} = h;
  const images = ['image-a', 'image-a', 'image-b'];
  ctx.state.formalImageProjectId = 'a';
  ctx.state.formalImageWorkspace = {base_version_id: 'v1', project_version: 4, shots: images.map((key, index) => ({shot_id: `shot-${index + 1}`, shot_no: index + 1, selected_image_key: key, decision_kind: 'image_confirmed', confirmed: false, candidates: [{image_key: key, preview_available: true}], candidate_snapshot_digest: `digest-${index}`}))};
  ctx.state.formalImagePreviewUrls.set('image-a', 'blob:image-a'); ctx.state.formalImagePreviewUrls.set('image-b', 'blob:image-b');
  ctx.elements.formalImageShotGrid.querySelectorAll = selector => selector === '[data-image-shot-confirm]:checked' ? images.map((_, index) => node(`[data-image-shot-confirm="${index}"]`)).filter(input => input.checked) : [];
  h.reason = (index, value) => { node(`[data-image-shot-reuse-reason="${index}"]`).value = value; };
  return h;
}
const checks = [];
async function check(name, fn) { try { await fn(); checks.push(`STEP_PASS|${name}`); } catch (error) { checks.push(`STEP_FAIL|${name}|${error.message}`); } }
(async () => {
  await check('F01-using-an-image-requires-visible-preview-and-reuse-reason', async () => {
    const {ctx, reason} = imageHarness(); let focused = -1; ctx.focusFormalImageShot = index => { focused = index; };
    ctx.useFormalImageShot(0); assert.equal(ctx.state.formalImageWorkspace.shots[0].confirmed, false);
    reason(0, '同一空间从不同角度展示'); ctx.state.formalImagePreviewUrls.delete('image-a');
    ctx.useFormalImageShot(0); assert.equal(ctx.state.formalImageWorkspace.shots[0].confirmed, false);
    ctx.state.formalImagePreviewUrls.set('image-a', 'blob:image-a'); ctx.useFormalImageShot(0);
    assert.equal(ctx.state.formalImageWorkspace.shots[0].confirmed, true); assert.equal(focused, 1);
    assert.equal(ctx.state.formalImageWorkspace.shots[1].confirmed, false);
  });
  await check('F02-shared-reason-only-applies-to-actually-selected-identical-image', async () => {
    const {ctx, node, reason} = imageHarness(); reason(0, '开场与回顾呈现同一空间');
    ctx.applyFormalImageReuseReason(0);
    assert.equal(node('[data-image-shot-reuse-reason="1"]').value, '开场与回顾呈现同一空间');
    assert.equal(node('[data-image-shot-reuse-reason="2"]').value, '');
    assert.equal(ctx.state.formalImageWorkspace.shots.some(shot => shot.confirmed), false);
  });
  await check('F03-temporary-choice-retains-reason-but-cannot-confirm-missing-image', async () => {
    const {ctx, node} = imageHarness(); const shot = ctx.state.formalImageWorkspace.shots[0]; shot.decision_kind = 'temporary_text_basis'; node('[data-image-shot-temporary="0"]').checked = true;
    ctx.useFormalImageShot(0); assert.equal(shot.confirmed, false);
    node('[data-image-shot-reason="0"]').value = '等待补拍入口'; ctx.useFormalImageShot(0);
    assert.equal(shot.confirmed, false); assert.equal(shot.decision_kind, 'temporary_text_basis');
    assert.equal(shot.temporary_reason, '等待补拍入口');
    assert.equal(ctx.formalImageDecisionIssue(shot), '等待补图，仅保存进度');
  });
  await check('F04-review-batch-only-submits-explicitly-selected-current-issues', async () => {
    const {ctx, node} = harness('review'); const review = {state: 'reviewing', project_version: 4, review_started: true, issues: [0, 1, 2].map(index => ({issue_key: `review_${index}`, status: 'open', focus_required: index === 0}))};
    ctx.renderFormalReview(review); let body; let calls = 0;
    ctx.api = async (url, options) => { calls += 1; body = JSON.parse(options.body); return {...review, issues: review.issues.map((issue, index) => ({...issue, status: index === 1 ? 'approve' : 'open'}))}; };
    ctx.elements.formalReviewIssues.querySelectorAll = () => [];
    await ctx.submitFormalReviewBatch(); assert.equal(calls, 0);
    const selected = node('[data-review-select="1"]'); selected.dataset.reviewSelect = '1'; selected.checked = true;
    ctx.elements.formalReviewIssues.querySelectorAll = selector => selector === '[data-review-select]:checked' ? [selected] : [];
    await ctx.submitFormalReviewBatch(); assert.equal(calls, 1); assert.deepEqual(body.issue_keys, ['review_1']); assert.equal(body.decision, 'approve'); assert.ok(!body.note);
  });
  await check('F05-review-return-still-requires-a-reason', async () => {
    const {ctx} = harness('review'); ctx.renderFormalReview({state: 'reviewing', project_version: 4, review_started: true, issues: [{issue_key: 'review_a', status: 'open'}]}); let calls = 0;
    ctx.api = async () => { calls += 1; return ctx.state.formalReview; };
    await ctx.submitFormalReviewDecision(0, 'return', ''); assert.equal(calls, 0);
    await ctx.submitFormalReviewDecision(0, 'approve', ''); assert.equal(calls, 1);
  });
  await check('F06-final-confirmation-is-one-explicit-action-and-keeps-server-gate', async () => {
    const {ctx} = harness('review'); const model = {state: 'ready_final', project_version: 4, ready_for_final: true, issues: []};
    ctx.renderFormalReview(model); assert.equal(ctx.elements.formalReviewFinalSubmit.disabled, false);
    let body; ctx.api = async (url, options) => { body = JSON.parse(options.body); return {...model, state: 'confirmed'}; };
    await ctx.confirmFormalReviewFinal({preventDefault: noop}); assert.equal(body.confirmed, true); assert.ok(!body.note);
    ctx.renderFormalReview({...model, ready_for_final: false}); assert.equal(ctx.elements.formalReviewFinalSubmit.disabled, true);
  });
  await check('F07-material-upload-uses-supplements-and-returned-project-lock', async () => {
    const {ctx} = harness('materials'); ctx.state.formalImages = {pending: 0};
    ctx.elements.formalImageUploadFiles.files = [new Blob(['one']), new Blob(['two'])];
    const uploads = []; ctx.api = async (url, options) => {
      if (!options) return {project_version: 4};
      uploads.push(url); assert.ok(options.body instanceof FormData);
      return {image_key: `image-${uploads.length}`, project_version: 4 + uploads.length};
    };
    ctx.loadFormalImages = async () => {}; ctx.loadFormalProjects = async () => {};
    await ctx.uploadFormalProjectImages({preventDefault: noop});
    assert.equal(uploads.length, 2); assert.ok(uploads.every(url => url.includes('/image-workspace/supplements?')));
    assert.ok(uploads[0].includes('expected_project_version=4')); assert.ok(uploads[1].includes('expected_project_version=5'));
    assert.doesNotMatch(ctx.elements.formalImageUploadState.innerHTML, /确认新 Brief/);
  });
  await check('F08-script-scope-keeps-original-instruction-and-ignores-late-project-response', async () => {
    const {ctx} = harness('script'); ctx.state.formalScriptProjectId = 'a'; ctx.state.formalScript = {revision: 2, project_version: 4, shots:[{shot_id:'shot-1',shot_no:1}]};ctx.creativeState().shotId='shot-1';
    ctx.flushFormalScriptChanges = async () => true; ctx.elements.formalScriptInstruction.value = '节奏更紧凑'; let resolve;
    ctx.api = () => new Promise(done => { resolve = done; });
    const pending = ctx.requestFormalScriptProposal(); await new Promise(resolve => setImmediate(resolve));
    ctx.elements.formalScriptInstruction.value = '迟到的新输入';
    resolve({task_id: 't', response: {type: 'scope_required', analysis_task_id: 't', suggested_targets: [{shot_id: 'shot-1', selected: true}]}}); await pending;
    assert.equal(ctx.state.formalScriptScopeTask.instruction, '节奏更紧凑');
    const stale = ctx.requestFormalScriptProposal(); await new Promise(resolve => setImmediate(resolve));
    ctx.window.location.pathname = '/projects/b/script'; ctx.state.formalScriptEpoch += 1; ctx.state.formalScriptRevisionProposal = null;
    resolve({task_id: 'old', response: {type: 'action_proposal', proposal_digest: 'digest'}}); await stale;
    assert.equal(ctx.state.formalScriptRevisionProposal, null);
  });
  await check('F09-editing-instruction-invalidates-proposal-and-selected-scope', async () => {
    const {ctx} = harness('script'); ctx.state.formalScriptRevisionProposal = {task_id: 't'}; ctx.state.formalScriptScopeTask = {analysis_task_id: 't'};
    ctx.invalidateFormalScriptProposal(); assert.equal(ctx.state.formalScriptRevisionProposal, null); assert.equal(ctx.state.formalScriptScopeTask, null); assert.equal(ctx.elements.formalScriptScope.hidden, true);
  });
  await check('F10-duplicate-presentation-keeps-selected-source-and-all-authoritative-candidates', async () => {
    const {ctx} = harness(); const items = [{image_key: 'a', duplicate_of: ''}, {image_key: 'a-copy', duplicate_of: 'a'}, {image_key: 'b', duplicate_of: ''}];
    assert.equal(ctx.formalVisibleImageCandidates(items, 'image_key', []).length, 2);
    assert.equal(ctx.formalVisibleImageCandidates(items, 'image_key', ['a-copy']).length, 3);
    assert.equal(ctx.formalVisibleImageCandidates(items, 'image_key', [], true).length, 3);
    assert.equal(items.length, 3); assert.equal(items[1].image_key, 'a-copy');
  });
  await check('F11-editing-a-script-cell-invalidates-an-earlier-preview', async () => {
    const {ctx} = harness('script'); ctx.window.clearTimeout = noop; ctx.window.setTimeout = () => 1;
    ctx.state.formalScript = {project_version: 4, revision: 1, shots: [{shot_no: 1, shot_id: 'shot-a', voiceover: '原旁白'}]};
    ctx.state.formalScriptRevisionProposal = {task_id: 'old'};
    const control = {dataset: {scriptShot: '1', scriptShotKey: 'shot-a', scriptField: 'voiceover'}, value: '新旁白'};
    ctx.queueFormalScriptCellSave({target: {closest: () => control}});
    assert.equal(ctx.state.formalScriptRevisionProposal, null);
    assert.equal(ctx.state.formalScriptDirtyCells.get('shot-a:voiceover').value, '新旁白');
  });
  await check('F12-new-instruction-can-be-submitted-while-old-preview-finishes', async () => {
    const {ctx} = harness('script'); ctx.state.formalScriptProjectId = 'a'; ctx.state.formalScript = {revision: 2, project_version: 4, shots:[{shot_id:'shot-1',shot_no:1}]};ctx.creativeState().shotId='shot-1';
    ctx.flushFormalScriptChanges = async () => true; let resolve; ctx.api = () => new Promise(done => { resolve = done; });
    const pending = ctx.requestFormalScriptProposal(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(ctx.elements.formalScriptInstructionSubmit.disabled, true);
    ctx.invalidateFormalScriptProposal(); assert.equal(ctx.elements.formalScriptInstructionSubmit.disabled, false);
    resolve({task_id: 'old', response: {type: 'action_proposal', proposal_digest: 'old'}}); await pending;
    assert.equal(ctx.state.formalScriptRevisionProposal, null); assert.equal(ctx.elements.formalScriptInstructionSubmit.disabled, false);
  });
  process.stdout.write(checks.join('\n') + '\n'); if (checks.some(line => line.startsWith('STEP_FAIL'))) process.exitCode = 1;
})();
