const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};
const nodes = new Map();
const node = key => {
  if (!nodes.has(key)) nodes.set(key, {value:'', dataset:{}, hidden:false, innerHTML:'', textContent:'',
    querySelector:node, querySelectorAll:()=>[], replaceChildren:noop, setAttribute:noop,
    addEventListener:noop, insertAdjacentHTML:noop, classList:{toggle:noop}});
  return nodes.get(key);
};
const ctx = vm.createContext({URL, URLSearchParams, Map, Set, Date, JSON, console,
  document:{querySelector:node, querySelectorAll:()=>[], addEventListener:noop},
  window:{location:{pathname:'/projects/a/script',search:''},addEventListener:noop},
  localStorage:{getItem:()=>null}, sessionStorage:{getItem:()=>null,setItem:noop}});
vm.runInContext(source.slice(0, source.indexOf('\nelements.formalOwnerConfirm.addEventListener')), ctx);
vm.runInContext(source.slice(source.indexOf('function workbenchModel()'), source.indexOf('async function workbenchProtect(')), ctx);
ctx.state = vm.runInContext('state', ctx);
ctx.state.formalScript = {state:'ready', shots:[{shot_id:'one',shot_no:'1',structure:'开场',
  visual:'第一行\n第二行\n第三行\n第四行完整画面',sound:'同期声',voiceover:'旁白检索词',subtitle:'字幕检索词',notes:'内部备注'}]};
ctx.syncWorkbenchAssistantContext = noop;
const check = (name, fn) => { fn(); console.log(`STEP_PASS|${name}`); };
check('reading-includes-complete-copy-and-only-nonempty-optional-fields', () => {
  const model = ctx.workbenchModel();
  const html = ctx.workbenchScriptBody(model, model.items[0]);
  assert.match(html, /第一行\n第二行\n第三行\n第四行完整画面/);
  assert.match(html, /旁白检索词/); assert.match(html, /字幕检索词/);
  assert.doesNotMatch(html, /内部备注/);
  const blank = {...model.items[0],voiceover:'',subtitle:'  ',sound:''};
  assert.doesNotMatch(ctx.workbenchScriptBody(model, blank), /<dt>(声音|旁白|字幕)<\/dt>/);
});
check('search-covers-optional-copy-and-local-changes', () => {
  const view = ctx.creativeState();
  for (const query of ['旁白检索词','字幕检索词','内部备注']) {
    view.filter = query; assert.equal(ctx.workbenchModel().filtered.length, 1);
  }
  ctx.state.formalScriptDirtyCells.set('one:subtitle', {value:'刚输入的新字幕'});
  view.filter = '刚输入'; assert.equal(ctx.workbenchModel().filtered.length, 1);
  view.filter = '字幕检索词'; assert.equal(ctx.workbenchModel().filtered.length, 0);
  view.filter = '';
});
check('reading-matches-retry-inflight-and-clear-values-of-editor', () => {
  ctx.state.formalScriptSaveRetryPending = {key:'one:visual',value:'重试文字'};
  assert.equal(ctx.workbenchModel().value(ctx.state.formalScript.shots[0], 'visual'), '重试文字');
  ctx.state.formalScriptDirtyCells.set('one:visual', {value:''});
  assert.equal(ctx.workbenchModel().value(ctx.state.formalScript.shots[0], 'visual'), '');
  ctx.state.formalScriptDirtyCells.clear(); ctx.state.formalScriptSaveRetryPending = null;
  ctx.state.formalScriptSaveInFlightPending = {key:'one:visual',value:'正在保存'};
  assert.equal(ctx.workbenchModel().value(ctx.state.formalScript.shots[0], 'visual'), '正在保存');
  ctx.state.formalScriptSaveInFlightPending = null;
});
check('copy-escapes-html-and-image-panel-keeps-compact-cards', () => {
  const shot = ctx.state.formalScript.shots[0]; shot.subtitle = '<img src=x onerror=alert(1)>';
  ctx.renderWorkbenchCards();
  const reading = node('#creative-script-overview').innerHTML;
  assert.match(reading, /is-script-reading/); assert.match(reading, /&lt;img/);
  assert.doesNotMatch(reading, /<img src=x/);
  ctx.window.location.search = '?panel=images'; ctx.renderWorkbenchCards();
  const images = node('#creative-image-overview').innerHTML;
  assert.doesNotMatch(images, /is-script-reading|workbench-script-copy/);
});
check('first-draft-search-uses-current-input-and-keeps-pagination', () => {
  ctx.window.location.search = '';
  ctx.state.formalScript = {state:'empty'};
  const view = ctx.firstCreativeView();
  view.data = {blocks:Array.from({length:8}, (_,i)=>({block_id:`b${i}`,shot_no:String(i+1),fields:{'画面描述':'旧内容','字幕':'原字幕'}}))};
  view.dirty.set('b0:字幕', {value:'新字幕'}); view.search = '新字幕';
  assert.equal(ctx.workbenchModel().filtered.length, 1);
  view.search = '原字幕'; assert.equal(ctx.workbenchModel().filtered.length, 7);
  view.search = ''; view.page = 2;
  assert.equal(ctx.workbenchModel().visible.length, 2);
  assert.equal(ctx.workbenchModel().pages, 2);
});
