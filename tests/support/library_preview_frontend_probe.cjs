const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};
const storage = {getItem: () => null, setItem: noop, removeItem: noop};
const nodes = new Map();
function node(key) {
  if (!nodes.has(key)) nodes.set(key, {value: '', textContent: '', innerHTML: '', dataset: {}, isConnected: true,
    classList: {add: noop, remove: noop, toggle: noop}, querySelector: node, querySelectorAll: () => [],
    setAttribute: noop, removeAttribute: noop, addEventListener: noop, replaceChildren: noop, focus: noop});
  return nodes.get(key);
}
const ctx = vm.createContext({URL, URLSearchParams, Headers, FormData, Blob, AbortController, console,
  Map, Set, Date, JSON, setTimeout, clearTimeout, localStorage: storage, sessionStorage: storage,
  document: {querySelector: node, getElementById: node, querySelectorAll: () => [], createElement: () => ({}), addEventListener: noop},
  window: {location: {pathname: '/projects/anonymous/script', search: '', origin: 'http://localhost'}, addEventListener: noop}});
vm.runInContext(source.slice(0, source.indexOf('\nelements.formalOwnerConfirm.addEventListener')), ctx);
ctx.state = vm.runInContext('state', ctx);
ctx.state.agentWorkspaceId = 'workspace';
ctx.state.agentAuth = 'anonymous-auth';
const tick = () => new Promise(resolve => setImmediate(resolve));
const checks = [];
async function check(name, run) {
  try { await run(); checks.push(`PASS ${name}`); }
  catch (error) { checks.push(`FAIL ${name}: ${error.stack}`); process.exitCode = 1; }
}
function queueHarness(options = {}) {
  const jobs = [], revoked = [];
  let running = 0, peak = 0;
  const queue = ctx.createFormalPreviewQueue({
    load: (requestPath, {signal}) => new Promise((resolve, reject) => {
      running += 1; peak = Math.max(peak, running);
      let settled = false;
      const finish = (error, size = 1) => {
        if (settled) return;
        settled = true; running -= 1;
        if (error) reject(error); else resolve(new Blob(['x'.repeat(size)]));
      };
      signal?.addEventListener('abort', () => finish(Object.assign(new Error('aborted'), {name: 'AbortError'})), {once: true});
      jobs.push({path: requestPath, resolve: size => finish(null, size), signal});
    }),
    createUrl: blob => `blob:${jobs.length}:${blob.size}`,
    revokeUrl: url => revoked.push(url),
    ...options,
  });
  return {queue, jobs, revoked, get peak() {return peak;}};
}

(async () => {
  await check('actual picker requests full inventory instead of silently inheriting shot filtering', async () => {
    const active = ctx.creativePickerActive, detail = ctx.renderCreativePickerDetail;
    ctx.creativePickerActive = () => true; ctx.renderCreativePickerDetail = noop;
    let called;
    ctx.api = async requestPath => {called = new URL(requestPath, 'http://localhost'); return {
      library_candidates: [], inventory_records: 321, inventory_groups: 266, filtered_groups: 266,
      page_groups: 0, page_records: 0, page: 1, has_more: true, facets: {},
    };};
    const picker = {projectId: 'anonymous', projectVersion: 8, shotNo: '3', source: 'library',
      request: 0, page: 1, query: '', matchScope: 'all', filters: {}};
    try {
      await ctx.loadCreativePicker(picker);
      assert.equal(called.searchParams.get('match_scope'), 'all');
      assert.match(node('.creative-picker-results').innerHTML, /321/);
      assert.match(node('.creative-picker-results').innerHTML, /266/);
    } finally {ctx.creativePickerActive = active; ctx.renderCreativePickerDetail = detail;}
  });
  await check('all library / current-shot filter / recommendations preserve query and server facets', async () => {
    const picker = {projectId: 'anonymous', projectVersion: 8, shotNo: '3', source: 'library', page: 1,
      query: '入口 人群', matchScope: 'all', filters: {source: 'creative', scene: '__unlabeled__'}};
    const all = new URL(ctx.creativePickerCandidatesPath(picker), 'http://localhost');
    assert.equal(all.searchParams.get('view'), 'library');
    assert.equal(all.searchParams.get('match_scope'), 'all');
    assert.equal(all.searchParams.get('page_size'), '12');
    assert.equal(all.searchParams.get('query'), '入口 人群');
    assert.equal(all.searchParams.get('source'), 'creative');
    assert.equal(all.searchParams.get('scene'), '__unlabeled__');
    picker.matchScope = 'current_shot';
    assert.equal(new URL(ctx.creativePickerCandidatesPath(picker), all).searchParams.get('match_scope'), 'current_shot');
    picker.source = 'recommended';
    const recommendation = new URL(ctx.creativePickerCandidatesPath(picker), all);
    assert.equal(recommendation.searchParams.get('view'), 'recommended');
    assert.equal(recommendation.searchParams.get('shot_no'), '3');
    assert.equal(recommendation.searchParams.get('query'), '入口 人群');
    assert.equal(recommendation.searchParams.get('source'), 'creative');
    ctx.resetCreativePickerFilters(picker);
    assert.equal(picker.query, ''); assert.equal(picker.page, 1); assert.equal(picker.matchScope, 'all');
    assert.deepEqual(Object.keys(picker.filters), []);
  });

  await check('recommendations without a selected shot preserve the existing category and all-shot scope', () => {
    const picker = {projectId: 'anonymous', projectVersion: 8, shotNo: '', source: 'recommended',
      query: '入口 人群', page: 1, matchScope: 'all', filters: {source: 'creative'}};
    const request = new URL(ctx.creativePickerCandidatesPath(picker), 'http://localhost');
    assert.equal(request.searchParams.get('match_scope'), '');
    assert.equal(request.searchParams.get('shot_no'), '');
    assert.equal(request.searchParams.get('view'), 'recommended');
    assert.equal(request.searchParams.get('query'), '入口 人群');
    assert.equal(request.searchParams.get('source'), 'creative');
    picker.shotNo = '2';
    assert.equal(new URL(ctx.creativePickerCandidatesPath(picker), request).searchParams.get('match_scope'), 'current_shot');
  });

  await check('duplicate groups preserve all source identities and counts never imply page is inventory', () => {
    const candidates = [
      {candidate_key: 'a', duplicate_group_id: 'g1'},
      {candidate_key: 'b', duplicate_group_id: 'g2'},
      {candidate_key: 'copy-a', duplicate_group_id: 'g1', duplicate_of: 'a'},
      {candidate_key: 'animation', duplicate_group_id: 'animation', is_animated: true},
    ];
    const groups = ctx.creativePickerCandidateGroups(candidates);
    assert.equal(groups.length, 3); assert.equal(groups[0].length, 2);
    assert.equal(groups[0][1].candidate_key, 'copy-a'); assert.equal(candidates.length, 4);
    const text = ctx.creativeLibraryCountText({inventory_records: 321, inventory_groups: 266,
      filtered_groups: 77, page_groups: 12, page_records: 14, page: 2});
    assert.match(text, /321/); assert.match(text, /266/); assert.match(text, /77/); assert.match(text, /12/); assert.match(text, /14/);
  });

  await check('recommendation counts show returned groups and records without inventing inventory totals', () => {
    const candidates = Array.from({length: 8}, (_, i) => ({candidate_key: `image-${i}`, duplicate_group_id: `group-${i}`}));
    candidates[0].sources = [{file: '匿名.xlsx', sheet: '素材', row: 1}, {file: '匿名.xlsx', sheet: '素材', row: 2}];
    candidates.push({candidate_key: 'copy-0', duplicate_group_id: 'group-0', duplicate_of: 'image-0'});
    const count = (model, values = candidates, source = 'recommended') => ctx.renderCreativePickerResults(
      {source, candidates: values, selected: null, page: 1}, model,
    ).match(/<p class="creative-picker-counts" role="status">([^<]*)<\/p>/)[1];
    assert.equal(count({candidate_count: 321, filtered_groups: 266}),
      '筛选后 266 组 · 本次推荐 8 组图片 / 9 条候选记录');
    assert.equal(count({candidate_count: 321}), '本次推荐 8 组图片 / 9 条候选记录');
    assert.equal(count({candidate_count: 321, filtered_groups: 0}, []),
      '筛选后 0 组 · 本次推荐 0 组图片 / 0 条候选记录');
    assert.equal(count({inventory_records: 321, inventory_groups: 266, filtered_groups: 77,
      page_groups: 12, page_records: 14, page: 2}, [], 'library'),
      '库存 321 条 · 266 组图片 · 筛选后 77 组 · 本页 12 组 / 14 条 · 第 2 页');
  });

  await check('shared queue caps all callers at four and starts selected requests before waiting cards', async () => {
    const h = queueHarness();
    const requests = Array.from({length: 4}, (_, i) => h.queue.request({key: `a${i}`, path: `/a${i}`, owner: 'page-a'}));
    await tick(); assert.equal(h.jobs.length, 4);
    requests.push(h.queue.request({key: 'waiting', path: '/waiting', owner: 'page-b', priority: 2}));
    requests.push(h.queue.request({key: 'selected', path: '/selected', owner: 'detail', priority: 0}));
    h.jobs[0].resolve(); await tick(); assert.equal(h.jobs[4].path, '/selected');
    h.jobs.slice(1).forEach(job => job.resolve()); await tick();
    h.jobs.slice(5).forEach(job => job.resolve()); await Promise.all(requests);
    assert.equal(h.peak, 4); h.queue.clear();
  });

  await check('same preview is deduplicated and page cancellation preserves another live subscriber', async () => {
    const h = queueHarness();
    const oldPage = h.queue.request({key: 'same', path: '/same', owner: 'old-page'});
    const selected = h.queue.request({key: 'same', path: '/same', owner: 'detail', priority: 0});
    const cancelled = assert.rejects(oldPage, error => error.name === 'AbortError');
    await tick(); assert.equal(h.jobs.length, 1);
    h.queue.release('old-page'); await cancelled; assert.equal(h.jobs[0].signal.aborted, false);
    h.jobs[0].resolve(3); assert.ok((await selected).startsWith('blob:'));
    const abandoned = h.queue.request({key: 'gone', path: '/gone', owner: 'old-page'});
    const abandonedResult = assert.rejects(abandoned, error => error.name === 'AbortError');
    await tick(); h.queue.release('old-page'); await abandonedResult;
    assert.equal(h.jobs[1].signal.aborted, true); h.queue.clear();
  });

  await check('timeout fails only its card and a retry can succeed', async () => {
    const h = queueHarness({timeoutMs: 15});
    const timed = h.queue.request({key: 'slow', path: '/slow', owner: 'page'});
    await assert.rejects(timed, error => error.code === 'PREVIEW_TIMEOUT');
    assert.equal(h.jobs[0].signal.aborted, true);
    const retry = h.queue.request({key: 'slow', path: '/slow', owner: 'page'});
    await tick(); h.jobs[1].resolve(); assert.ok(await retry); h.queue.clear();
  });

  await check('an immediate retry after a failed preview creates a fresh job', async () => {
    let attempts = 0;
    const queue = ctx.createFormalPreviewQueue({
      load: async () => { if (++attempts === 1) throw new Error('offline'); return new Blob(['image']); },
      createUrl: () => 'blob:recovered', revokeUrl: noop,
    });
    const result = await queue.request({key: 'retry', path: '/retry'}).catch(() =>
      queue.request({key: 'retry', path: '/retry'}));
    assert.equal(result, 'blob:recovered'); assert.equal(attempts, 2); queue.clear();
  });

  await check('blob budget evicts least recently used URLs and logout cleanup revokes every survivor', async () => {
    const h = queueHarness({maxBytes: 5});
    const first = h.queue.request({key: 'one', path: '/one', owner: 'page'});
    await tick(); h.jobs[0].resolve(3); const one = await first;
    const second = h.queue.request({key: 'two', path: '/two', owner: 'page'});
    await tick(); h.jobs[1].resolve(3); const two = await second;
    assert.deepEqual(h.revoked, [one]);
    assert.equal(await h.queue.request({key: 'two', path: '/two', owner: 'page'}), two);
    assert.equal(h.jobs.length, 2);
    const reload = h.queue.request({key: 'one', path: '/one', owner: 'page'});
    await tick(); assert.equal(h.jobs.length, 3); h.jobs[2].resolve(3); await reload;
    h.queue.clear(); assert.equal(h.revoked.length, 3);
  });

  await check('an original larger than the cache budget remains viewable until its last owner leaves', async () => {
    const h = queueHarness({maxBytes: 2});
    const first = h.queue.request({key: 'large', path: '/large', owner: 'detail'});
    h.jobs[0].resolve(3); const url = await first;
    const adopted = await h.queue.request({key: 'large', path: '/large', owner: 'selected'});
    assert.equal(adopted, url); assert.equal(h.jobs.length, 1);
    h.queue.release('detail'); assert.deepEqual(h.revoked, []);
    h.queue.release('selected'); assert.deepEqual(h.revoked, [url]);
    const reload = h.queue.request({key: 'large', path: '/large', owner: 'detail'});
    h.jobs[1].resolve(3); await reload; h.queue.clear();
    assert.equal(h.revoked.length, 2);
  });

  await check('cache context separates project version, media bytes, source package and thumbnail from original', () => {
    const p = '/api/formal/projects/anonymous/images/default-candidates/media-a/preview?workspace_id=workspace&expected_project_version=8';
    const context = {candidate: {candidate_key: 'media-a', content_sha256: 'a'.repeat(64)}, model: {base_version_id: 'v1', package: {manifest_sha256: 'p1'}}};
    const original = ctx.formalPreviewCacheKey(p, context);
    assert.notEqual(original, ctx.formalPreviewCacheKey(p, {...context, variant: 'thumbnail'}));
    assert.notEqual(original, ctx.formalPreviewCacheKey(p.replace('version=8', 'version=9'), context));
    assert.notEqual(original, ctx.formalPreviewCacheKey(p, {...context, candidate: {...context.candidate, content_sha256: 'b'.repeat(64)}}));
    assert.notEqual(original, ctx.formalPreviewCacheKey(p, {...context, model: {base_version_id: 'v1', package: {manifest_sha256: 'p2'}}}));
    assert.equal(ctx.state.formalImagePreviewUrls.size, 0, 'Building thumbnail keys never certifies an original preview');
  });

  await check('new package and project versions retire old alias URLs before rendering', () => {
    ctx.syncFormalPreviewModel('anonymous', 8, {base_version_id: 'v1', package: {manifest_sha256: 'p1'}});
    ctx.state.formalImagePreviewUrls.set('default:media', 'blob:old');
    ctx.state.formalImageThumbnailUrls.set('default:media', 'blob:old-small');
    ctx.syncFormalPreviewModel('anonymous', 8, {base_version_id: 'v1', package: {manifest_sha256: 'p2'}});
    assert.equal(ctx.state.formalImagePreviewUrls.size, 0); assert.equal(ctx.state.formalImageThumbnailUrls.size, 0);
    ctx.state.formalImagePreviewUrls.set('media', 'blob:version-old');
    ctx.syncFormalPreviewModel('anonymous', 9, {base_version_id: 'v2'});
    assert.equal(ctx.state.formalImagePreviewUrls.size, 0);
  });
  process.stdout.write(checks.join('\n') + '\n');
})().catch(error => {console.error(error); process.exitCode = 1;});
