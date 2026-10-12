const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};

function harness() {
  const nodes = new Map();
  function node(key) {
    if (!nodes.has(key)) nodes.set(key, {
      value: '', textContent: '', innerHTML: '', dataset: {}, hidden: false,
      disabled: false, checked: false, isConnected: true,
      classList: {add: noop, remove: noop, toggle: noop},
      querySelector: node, querySelectorAll: () => [], setAttribute: noop,
      removeAttribute: noop, replaceChildren: noop, addEventListener: noop,
      focus: noop, scrollIntoView: noop,
    });
    return nodes.get(key);
  }
  const issues = node('#formal-review-issues');
  let markup = '', inputs = new Map();
  Object.defineProperty(issues, 'innerHTML', {
    get: () => markup,
    set(value) {
      markup = value;
      inputs = new Map([...value.matchAll(/<textarea\b[^>]*data-review-note="(\d+)"[^>]*>([\s\S]*?)<\/textarea>/g)]
        .map(match => [match[1], {value: match[2], dataset: {reviewNote: match[1]}, focus: noop}]));
    },
  });
  issues.querySelector = selector => inputs.get(selector.match(/data-review-note="(\d+)"/)?.[1]) || null;
  issues.querySelectorAll = selector => selector === '[data-review-note]' ? [...inputs.values()] : [];
  const storage = {getItem: () => null, setItem: noop, removeItem: noop};
  const ctx = vm.createContext({URL, URLSearchParams, Headers, console, Map, Set, Date, JSON,
    localStorage: storage, sessionStorage: storage,
    document: {querySelector: node, querySelectorAll: () => [], createElement: () => ({}), addEventListener: noop},
    window: {location: {pathname: '/projects/a/review', search: '', origin: 'http://localhost'}, addEventListener: noop},
    setTimeout: () => 1, clearTimeout: noop,
  });
  vm.runInContext(source.slice(0, source.indexOf('\nelements.formalOwnerConfirm.addEventListener')), ctx);
  ctx.state = vm.runInContext('state', ctx);
  ctx.elements = vm.runInContext('elements', ctx);
  Object.assign(ctx.state, {agentWorkspaceId: 'w', agentAuth: 'auth', formalBriefAvailable: true});
  ctx.showToast = noop;
  ctx.loadFormalReviewPreviews = async () => {};
  const note = key => {
    const index = ctx.state.formalReview.issues.findIndex(issue => issue.issue_key === key);
    return issues.querySelector(`[data-review-note="${index}"]`);
  };
  return {ctx, note};
}

function model(version = 1, lock = 4) {
  return {state: 'reviewing', project_version: lock, version_label: `版本 ${version}`,
    review_started: true, ready_for_final: false,
    issues: ['first', 'second', 'third'].map((name, index) => ({
      issue_key: `review_version${version}_${name}`, shot_no: index + 1, status: 'open',
    })),
  };
}
function decisionModel(decision = 'approve') {
  const fresh = model(1, 5);
  fresh.issues[0].status = decision;
  fresh.issues.reverse(); // Notes belong to issue keys, not a changing list index.
  return fresh;
}
const first = 'review_version1_first', second = 'review_version1_second';
function ready() {
  const h = harness();
  h.ctx.renderFormalReview(model());
  h.note(first).value = '已核对本项';
  h.note(second).value = '第二项待补充 <细节> & 保留换行\n第二行';
  return h;
}
const checks = [];
async function check(name, fn) {
  try { await fn(); checks.push(`STEP_PASS|${name}`); }
  catch (error) { checks.push(`STEP_FAIL|${name}|${error.message}`); }
}

(async () => {
  await check('R01-approve-retains-other-latest-text-by-issue-key', async () => {
    const {ctx, note} = ready();
    let resolve; ctx.api = () => new Promise(done => { resolve = done; });
    const pending = ctx.submitFormalReviewDecision(0, 'approve', note(first).value);
    note(second).value += '\n请求期间继续填写';
    const expected = note(second).value;
    resolve(decisionModel()); await pending;
    assert.equal(note(first), null, 'Successfully submitted issue must not keep an editable note');
    assert.equal(note(second).value, expected, 'Submitting another issue discarded unsent text');
    assert.equal(ctx.state.formalReview.project_version, 5);
  });
  await check('R02-return-retains-other-notes-without-restoring-submitted-item', async () => {
    const {ctx, note} = ready(); const expected = note(second).value;
    ctx.api = async () => decisionModel('return');
    await ctx.submitFormalReviewDecision(0, 'return', note(first).value);
    assert.equal(note(first), null); assert.equal(note(second).value, expected);
  });
  await check('R03-failed-decision-keeps-all-inputs-for-retry', async () => {
    const {ctx, note} = ready(); const expected = note(second).value;
    ctx.api = async () => { throw Error('offline'); };
    await ctx.submitFormalReviewDecision(0, 'approve', note(first).value);
    assert.equal(note(first).value, '已核对本项'); assert.equal(note(second).value, expected);
    assert.equal(ctx.state.formalReviewBusy, false);
  });
  await check('R04-version-conflict-reload-keeps-notes-for-same-review-items', async () => {
    const {ctx, note} = ready(); const expected = note(second).value;
    ctx.api = async (url, options) => {
      if (options?.method === 'POST') throw {payload: {detail: {code: 'PROJECT_VERSION_CONFLICT'}}};
      return url.includes('/review?') ? model(1, 5) : {project_version: 5};
    };
    await ctx.submitFormalReviewDecision(0, 'approve', note(first).value);
    assert.equal(note(first).value, '已核对本项'); assert.equal(note(second).value, expected);
  });
  await check('R05-new-version-does-not-inherit-prior-version-notes', async () => {
    const {ctx, note} = ready();
    ctx.api = async url => url.includes('/review?') ? model(2, 5) : {project_version: 5};
    await ctx.loadFormalReview('a');
    assert.equal(note('review_version2_first').value, '');
    assert.equal(note('review_version2_second').value, '');
  });
  await check('R06-late-project-response-cannot-replace-new-project-or-its-input', async () => {
    const {ctx, note} = ready(); let resolve;
    ctx.api = () => new Promise(done => { resolve = done; });
    const pending = ctx.submitFormalReviewDecision(0, 'approve', note(first).value);
    ctx.window.location.pathname = '/projects/b/review';
    ctx.state.formalReviewEpoch += 1;
    ctx.renderFormalReview(model());
    assert.equal(note(second).value, '', 'Notes cannot be reused in another project');
    note(second).value = 'B 项目意见';
    resolve(decisionModel()); await pending;
    assert.equal(ctx.state.formalReview.project_version, 4);
    assert.equal(note(second).value, 'B 项目意见');
  });
  await check('R07-late-conflict-cannot-reload-previous-project', async () => {
    const {ctx, note} = ready(); let reject; let calls = 0;
    ctx.api = () => { calls += 1; return new Promise((resolve, fail) => { reject = fail; }); };
    const pending = ctx.submitFormalReviewDecision(0, 'approve', note(first).value);
    ctx.window.location.pathname = '/projects/b/review'; ctx.state.formalReviewEpoch += 1;
    ctx.renderFormalReview(model(2));
    reject({payload: {detail: {code: 'PROJECT_VERSION_CONFLICT'}}});
    // A stale conflict must return immediately, without starting a second request.
    await Promise.race([pending, new Promise(resolve => setImmediate(resolve))]);
    assert.equal(calls, 1); assert.equal(ctx.state.formalReview.version_label, '版本 2');
  });
  process.stdout.write(checks.join('\n') + '\n');
  if (checks.some(line => line.startsWith('STEP_FAIL'))) process.exitCode = 1;
})();
