const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};

function harness() {
  const nodes = new Map();
  const storage = new Map();
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, {
      value: '', files: [], textContent: '', innerHTML: '', dataset: {}, hidden: false, disabled: false,
      classList: {add: noop, remove: noop, toggle: noop}, querySelector: node, closest: node, querySelectorAll: () => [],
      getAttribute: () => null, setAttribute: noop, removeAttribute: noop, replaceChildren: noop, append: noop, prepend: noop,
      addEventListener: noop, reportValidity: () => true, setCustomValidity: noop, focus: noop,
      scrollIntoView: noop, insertAdjacentHTML: noop,
    });
    return nodes.get(key);
  };
  const store = {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)};
  const ctx = vm.createContext({
    console, URL, URLSearchParams, Headers, FormData, TextEncoder, Map, Set, Date, JSON,
    CSS: {escape: value => value}, localStorage: store, sessionStorage: store,
    document: {querySelector: node, closest: node, querySelectorAll: () => [], addEventListener: noop},
    window: {location: {pathname: '/projects/a/brief', search: '', origin: 'http://localhost'}, history: {replaceState: noop}, addEventListener: noop},
    setTimeout: () => 1, clearTimeout: noop,
  });
  vm.runInContext(source.slice(0, source.indexOf('\nelements.formalOwnerConfirm.addEventListener')), ctx);
  ctx.state = vm.runInContext('state', ctx);
  ctx.elements = vm.runInContext('elements', ctx);
  ctx.state.formalBriefProjectId = 'a';
  ctx.state.formalBriefUploadLimits = {max_file_bytes: 24, max_documents: 20, max_text_bytes: 12, text_counts_as_document: true};
  ctx.showToast = noop;
  return ctx;
}

(async () => {
  const results = [];
  async function check(name, operation) {
    try {await operation(); results.push(`PASS ${name}`);}
    catch (error) {results.push(`FAIL ${name}: ${error.message}`); process.exitCode = 1;}
  }
  await check('oversized-file-stops-before-network', async () => {
    const ctx = harness(); const requests = [];
    ctx.elements.formalBriefFiles.files = [{name: 'too-large.txt', size: 25}];
    ctx.api = async () => {requests.push('sent'); throw Error('unexpected request');};
    await ctx.uploadFormalBrief({preventDefault: noop});
    assert.equal(requests.length, 0);
  });
  await check('text-counts-utf8-bytes-and-one-document', () => {
    const ctx = harness(); const limits = ctx.state.formalBriefUploadLimits;
    assert.equal(ctx.formalBriefUploadProblem([], '中中中中', limits), '');
    assert.notEqual(ctx.formalBriefUploadProblem([], '中中中中a', limits), '');
    const files = Array.from({length: 20}, (_, number) => ({name: `${number}.txt`, size: 1}));
    assert.equal(ctx.formalBriefUploadProblem(files, '', limits), '');
    assert.notEqual(ctx.formalBriefUploadProblem(files, 'a', limits), '');
  });
  await check('noncritical-failure-is-visible-and-confirmable-critical-is-blocked', () => {
    const ctx = harness();
    ctx.renderFormalStandardCard = () => ({unavailable: false, confirmed: false, missing: []});
    ctx.renderFormalBriefRouting = () => ({ready: true, blocked: false});
    ctx.renderFormalConflicts = noop;
    const response = {
      confirmation_status: 'pending', routing: {script_type: '发布会'},
      brief: {documents: [{filename: 'optional.png', status: 'failed', critical: false, error: {message: '图片文字识别失败'}}]},
    };
    ctx.renderFormalBrief(response);
    assert.equal(ctx.elements.formalBriefConfirm.disabled, false);
    assert.match(ctx.elements.formalBriefFilesState.innerHTML, /图片文字识别失败/);
    response.brief.documents[0].critical = true;
    ctx.renderFormalBrief(response);
    assert.equal(ctx.elements.formalBriefConfirm.disabled, true);
  });
  console.log(results.join('\n'));
})().catch(error => {console.error(error); process.exitCode = 1;});
