const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};
function harness() {
  const nodes = new Map();
  const storage = new Map();
  const node = (key) => {
    if (!nodes.has(key)) nodes.set(key, {value:'', textContent:'', innerHTML:'', dataset:{}, hidden:false, disabled:false, checked:false, isConnected:true, classList:{add:noop,remove:noop,toggle:noop}, querySelector:node, querySelectorAll:() => [], getAttribute:()=>null, setAttribute:noop, removeAttribute:noop, replaceChildren:noop, addEventListener:noop, focus:noop, scrollIntoView:noop, insertAdjacentHTML:noop});
    return nodes.get(key);
  };
  const store = {getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  const ctx = vm.createContext({console,URL,URLSearchParams,Headers,FormData,Map,Set,Date,JSON,CSS:{escape:x=>x}, localStorage:store,sessionStorage:store, document:{body:node('body'),querySelector:node,getElementById:node,querySelectorAll:()=>[],addEventListener:noop}, window:{location:{pathname:'/projects/a/shot-images',search:'',origin:'http://localhost',get href(){return this.origin+this.pathname+this.search;}},history:{replaceState:noop,pushState:noop},addEventListener:noop,setTimeout:()=>1,clearTimeout:noop},setTimeout:()=>1,clearTimeout:noop});
  vm.runInContext(source.slice(0,source.indexOf('\nelements.formalOwnerConfirm.addEventListener')),ctx);
  ctx.state = vm.runInContext('state',ctx); ctx.elements=vm.runInContext('elements',ctx);
  ctx.syncCreativePrimaryAction=noop;ctx.syncCreativeShotVisibility=noop;ctx.renderCreativeOutline=noop;ctx.syncCreativeNextAction=noop;
  return {ctx,node,storage};
}
function imageAnalysisHarness() {
  const h=harness(), {ctx}=h; const calls=[], renders=[], navigations=[];
  ctx.window.location.pathname='/projects/a/materials';ctx.window.location.search='?return_to=shot-images&focus_shot=1&supplement_image_key=image-a';
  ctx.state.formalImages={pending:1,images:[{image_key:'image-a',status:'pending'}]};
  ctx.FormData=class {constructor(form){this.form=form;} get(key){return this.form.values?.[key] || '';} [Symbol.iterator](){return Object.entries(this.form.values || {})[Symbol.iterator]();}};
  const form={dataset:{formalImageForm:'0'},reportValidity:()=>true,querySelector:()=>h.node('analysis-button'),values:{status:'pass',caption:'甲图片描述'}};
  const event={preventDefault:noop,target:{closest:()=>form}};
  ctx.renderFormalImages=(value)=>{renders.push(value);ctx.state.formalImages=value;};
  ctx.loadFormalImagePreviews=async()=>{};ctx.showToast=noop;ctx.navigateFormal=(url)=>navigations.push(url);
  return {...h,calls,renders,navigations,event};
}
function imageProposalHarness() {
  const h=harness(),{ctx,node}=h;
  ctx.state.formalImageProjectId='a';ctx.state.formalImageWorkspace={base_version_id:'v1',project_version:1,shots:[{shot_id:'shot-1',shot_no:'1',selected_image_key:'image-a',decision_kind:'image_confirmed',candidates:[{image_key:'image-a'},{image_key:'image-b'}],candidate_snapshot_digest:'digest',confirmed:true,temporary_reason:'',reuse_reason:'',query:''}]};
  node('[data-image-shot-confirm="0"]').checked=true;
  ctx.creativeState().imageMode='all';ctx.state.formalImageWorkspace.shots[0].explicitly_changed=true;
  ctx.state.formalImageVersionProposal={proposal_id:'proposal-a',proposal_digest:'a'.repeat(64)};
  ctx.elements.formalImageVersionProposal.hidden=false;
  ctx.refreshFormalImageReuseState=noop;ctx.updateFormalImageShotPreview=noop;ctx.loadFormalShotCandidatePreview=async()=>{};ctx.showToast=noop;
  ctx.validateFormalImageSelections=()=>{ctx.captureFormalImageInputs();ctx.elements.formalImageVersionSave.disabled=false;};
  return h;
}
function imagePreviewHarness() {
  const h=harness(),{ctx,node}=h;const targets=new Map(),requests=new Map();
  ctx.state.formalImageWorkspace={project_version:1,shots:['image-a','image-b'].map((key,index)=>({shot_no:String(index+1),selected_image_key:key,candidates:[{image_key:key}]}))};
  for(let index=0;index<2;index++)targets.set(`#formal-image-shot-preview-${index}`,{isConnected:true,innerHTML:'',images:[],replaceChildren(image){this.images=[image];}});
  ctx.document.querySelector=selector=>targets.get(selector)||node(selector);ctx.document.createElement=()=>({});
  ctx.URL=class extends URL {static createObjectURL(blob){return `blob:${blob.key}`;} static revokeObjectURL(){}};ctx.validateFormalImageSelections=noop;ctx.formalError=()=> '图片读取失败';
  ctx.readFormalImagePreview=path=>new Promise((resolve,reject)=>requests.set(path.includes('/image-a/')?'image-a':'image-b',{resolve,reject}));
  const replaceTargets=()=>{for(const [selector,old]of targets){old.isConnected=false;targets.set(selector,{isConnected:true,innerHTML:'',images:[],replaceChildren(image){this.images=[image];}});}};
  return {...h,targets,requests,replaceTargets};
}
const checks=[];
async function check(name, fn) { try {await fn(); checks.push(`STEP_PASS|${name}`);} catch(e) {checks.push(`STEP_FAIL|${name}|${e.message}`);} }
(async()=>{
  await check('I02-search-preserves-other-shot-input',async()=>{
    const {ctx,node}=harness();
    ctx.state.formalImageWorkspace={base_version_id:'v1',project_version:1,shots:[{shot_id:'shot-1',shot_no:'1',decision_kind:'temporary_text_basis',temporary_reason:'',confirmed:false,candidates:[]},{shot_id:'shot-2',shot_no:'2',decision_kind:'image_confirmed',candidates:[]}]};
    ctx.state.formalImageProjectId='a';
    node('[data-image-shot-reason="0"]').value='现场图片等待客户确认';
    node('[data-image-shot-reuse-reason="0"]').value='跨镜复用依据';
    node('[data-image-shot-confirm="0"]').checked=true;
    node('[data-image-shot-temporary="0"]').checked=true;
    ctx.api=async()=>({candidates:[],candidate_snapshot_digest:'digest',gap_reason:'暂无匹配'});
    ctx.renderFormalImageWorkspace=noop;ctx.loadFormalShotCandidatePreview=async()=>{};ctx.showToast=noop;
    ctx.state.formalScript={shots:ctx.state.formalImageWorkspace.shots};
    await ctx.searchFormalImageShot({dataset:{imageShotSearch:'1'},disabled:false});
    assert.equal(ctx.state.formalImageWorkspace.shots[0].temporary_reason,'现场图片等待客户确认');
    assert.equal(ctx.state.formalImageWorkspace.shots[0].reuse_reason,'跨镜复用依据');
    assert.equal(ctx.state.formalImageWorkspace.shots[0].confirmed,true);
  });
  await check('I03-no-results-never-selects-temporary-for-user',async()=>{
    const {ctx}=harness();
    ctx.state.formalImageWorkspace={base_version_id:'v1',project_version:1,shots:[{shot_id:'shot-1',shot_no:'1',decision_kind:'image_confirmed',candidates:[]}]};ctx.state.formalImageProjectId='a';
    ctx.api=async()=>({candidates:[],candidate_snapshot_digest:'digest'});ctx.renderFormalImageWorkspace=noop;ctx.loadFormalShotCandidatePreview=async()=>{};ctx.showToast=noop;
    ctx.state.formalScript={shots:ctx.state.formalImageWorkspace.shots};
    await ctx.searchFormalImageShot({dataset:{imageShotSearch:'0'}});
    assert.notEqual(ctx.state.formalImageWorkspace.shots[0].decision_kind,'temporary_text_basis');
  });
  await check('A01-zero-candidates-have-recovery-actions',()=>{
    const {ctx}=harness();ctx.state.agentProjectId='a';
    const html=ctx.agentImageCandidatesHtml({candidates:[],shot_no:'1',next_steps:[]},'t1');
    assert.match(html,/data-agent-temporary=/);
    assert.match(html,/href="\/projects\/a\/script\?/);
  });
  await check('A02-conversation-prioritizes-latest-result-and-retains-history',()=>{
    const {ctx}=harness();
    ctx.state.agentLocalMessages=[{content:'第一问',created_at:'2026-01-01T01:00:00Z'},{content:'第二问',created_at:'2026-01-01T01:02:00Z'}];
    ctx.renderAgentConversation({timeline:[{created_at:'2026-01-01T01:01:00Z',task_id:'t1',response:{summary:'第一答'}},{created_at:'2026-01-01T01:03:00Z',task_id:'t2',response:{summary:'第二答'}}]});
    const html=ctx.elements.agentFeed.innerHTML;
    assert.ok(html.indexOf('第二答') < html.indexOf('第二问'));
    assert.ok(html.indexOf('第二问') < html.indexOf('第一答'));
    assert.match(html, /历史对话/);
    assert.match(html, /第一问/);
  });

  await check('U01-global-navigation-never-appends-project',()=>{
    const {ctx}=harness();
    for (const [route,href] of [['projects','/projects'],['tasks','/tasks'],['tools','/tools'],['admin','/admin']]) {
      assert.equal(ctx.formalNavigationTarget({dataset:{route},getAttribute:()=>href}),href);
    }
    assert.equal(ctx.formalNavigationTarget({dataset:{route:'materials'},getAttribute:()=>'/projects'}),'/projects/a/script?panel=images&aux=materials');
  });
  await check('U02-global-assistant-retains-last-project',()=>{
    const {ctx}=harness();ctx.window.location.pathname='/tools';ctx.state.lastProjectId='a';
    assert.equal(ctx.formalNavigationTarget({dataset:{route:'assistant'},getAttribute:()=>'/assistant'}),'/projects/a/assistant');
  });
  await check('I02-drafts-are-project-and-base-version-scoped',()=>{
    const {ctx,storage}=harness();ctx.showToast=noop;
    storage.set(ctx.formalImageInputKey('a','v1'),JSON.stringify({project_version:3,shots:[{shot_id:'shot-1',shot_no:'1',shot_id:'s1',temporary_reason:'甲项目理由',reuse_reason:'甲项目复用',decision_kind:'temporary_text_basis',confirmed:true,candidates:[],query:'甲项目查找',candidate_snapshot_digest:'stable-digest'}]}));
    const read=(project,version,pv)=>{const value={base_version_id:version,project_version:pv,shots:[{shot_id:'shot-1',shot_no:'1',shot_id:'s1',candidates:[],confirmed:false,query:'甲项目查找',candidate_snapshot_digest:'stable-digest'}]};ctx.restoreFormalImageInputs(value,project);return value.shots[0];};
    assert.equal(read('a','v1',3).confirmed,true);
    assert.equal(read('a','v1',3).temporary_reason,'甲项目理由');
    assert.equal(read('b','v1',3).temporary_reason,undefined);
    assert.equal(read('a','v2',3).temporary_reason,undefined);
    assert.equal(read('a','v1',4).temporary_reason,'甲项目理由');
    assert.equal(read('a','v1',4).confirmed,false);
  });
  await check('I02-late-search-cannot-replace-another-workspace',async()=>{
    const {ctx}=harness();let release;
    const old={base_version_id:'v1',project_version:1,shots:[{shot_id:'shot-1',shot_no:'1',decision_kind:'image_confirmed',candidates:[]}]};
    ctx.state.formalImageWorkspace=old;ctx.state.formalImageProjectId='a';
    ctx.api=()=>new Promise(resolve=>{release=resolve;});ctx.renderFormalImageWorkspace=()=>{throw Error('late render');};ctx.showToast=noop;
    ctx.state.formalScript={shots:ctx.state.formalImageWorkspace.shots};
    const pending=ctx.searchFormalImageShot({dataset:{imageShotSearch:'0'}});
    await new Promise(resolve=>setImmediate(resolve));assert.equal(typeof release,'function');
    const current={base_version_id:'v2',project_version:2,shots:[{shot_id:'shot-1',shot_no:'1',candidates:[],query:'乙输入'}]};
    ctx.state.formalImageWorkspace=current;ctx.state.formalImageProjectId='b';ctx.window.location.pathname='/projects/b/shot-images';
    release({candidates:[{image_key:'unexpected'}],candidate_snapshot_digest:'late'});await pending;
    assert.equal(ctx.state.formalImageWorkspace,current);assert.equal(current.shots[0].query,'乙输入');assert.equal(old.shots[0].candidate_snapshot_digest,undefined);
  });
  await check('A03-global-and-project-assistants-poll-running-tasks',async()=>{
    for(const route of ['/assistant','/projects/a/assistant']){
      const {ctx}=harness();ctx.window.location.pathname=route;ctx.state.agentProjectId='a';ctx.state.agentEpoch=1;let scheduled=0;
      ctx.ensureAgentProjectSession=async()=>({session:{agent_session_id:'session-a'}});
      ctx.api=async()=>({tasks:[{task_id:'t1',status:'executing'}],timeline:[]});
      ctx.loadAgentCandidatePreviews=async()=>{};ctx.setTimeout=()=>++scheduled;
      await ctx.loadAgentSession('a',1);assert.equal(scheduled,1);assert.equal(ctx.state.agentSessionId,'session-a');
    }
  });
  await check('A04-late-session-does-not-cross-projects',async()=>{
    const {ctx}=harness();ctx.window.location.pathname='/assistant';ctx.state.agentProjectId='a';ctx.state.agentEpoch=1;let release;
    ctx.ensureAgentProjectSession=()=>new Promise(resolve=>{release=resolve;});ctx.api=async()=>{throw Error('stale request should stop');};
    const pending=ctx.loadAgentSession('a',1);
    ctx.state.agentProjectId='b';ctx.state.agentEpoch=2;ctx.state.agentSessionId='session-b';ctx.elements.agentFeed.innerHTML='乙项目会话';
    release({session:{agent_session_id:'session-a'}});await pending;
    assert.equal(ctx.state.agentSessionId,'session-b');assert.equal(ctx.elements.agentFeed.innerHTML,'乙项目会话');
  });
  await check('A05-late-message-keeps-new-project-composer',async()=>{
    const {ctx}=harness();ctx.window.location.pathname='/assistant';ctx.state.agentProjectId='a';ctx.state.agentSessionId='session-a';ctx.state.agentEpoch=1;
    ctx.elements.agentMessage.value='甲项目修改请求';let release;const requested=[];
    ctx.api=(url,options)=>{requested.push([url,JSON.parse(options.body)]);return new Promise(resolve=>{release=resolve;});};
    ctx.loadAgentSession=()=>{throw Error('late reload');};
    const pending=ctx.submitAgentMessage({preventDefault:noop});
    ctx.state.agentProjectId='b';ctx.state.agentSessionId='session-b';ctx.state.agentEpoch=2;ctx.elements.agentMessage.value='乙项目独立输入';
    release({});await pending;
    assert.equal(requested.length,1);assert.equal(requested[0][0],'/api/agent/sessions/session-a/messages');assert.equal(requested[0][1].content,'甲项目修改请求');
    assert.equal(ctx.elements.agentMessage.value,'乙项目独立输入');assert.equal(ctx.state.agentSessionId,'session-b');
  });
  await check('A06-switch-project-isolates-unsubmitted-input',async()=>{
    const {ctx}=harness();ctx.window.location.pathname='/assistant';ctx.state.agentProjectId='a';ctx.elements.agentMessage.value='甲未提交输入';
    ctx.api=async()=>({project:{},stages:[]});ctx.renderAgentState=noop;ctx.loadAgentSession=async()=>{};
    await ctx.selectAgentProject('b');assert.equal(ctx.elements.agentMessage.value,'');
    ctx.elements.agentMessage.value='乙未提交输入';await ctx.selectAgentProject('a');
    assert.equal(ctx.elements.agentMessage.value,'甲未提交输入');assert.equal(ctx.state.agentInputDrafts.get('b'),'乙未提交输入');
  });
  await check('A07-proposal-renders-returned-before-after',()=>{
    const {ctx}=harness();ctx.state.agentProjectId='a';
    const html=ctx.agentProposalHtml({proposal_digest:'a'.repeat(64),preview:{changes:[{label:'第2镜字幕',before:'冗长的原文',after:'精简文案'}]}},'task1');
    assert.match(html,/<del>冗长的原文<\/del>/);assert.match(html,/<ins>精简文案<\/ins>/);assert.equal(ctx.state.agentPendingActions.size,1);
  });
  await check('A08-clarification-is-explicit-composer-action',()=>{
    const {ctx}=harness();ctx.window.location.pathname='/assistant';ctx.state.agentProjectId='a';ctx.state.agentEpoch=2;
    ctx.agentClarificationHtml({choices:[{label:'删除整镜',instruction:'删除第2镜'}],questions:['需要删除整镜吗？']},'clarify');
    ctx.chooseAgentClarification('clarify',0);
    assert.equal(ctx.elements.agentMessage.value,'删除第2镜');assert.equal(ctx.elements.agentMessageSubmit.disabled,false);
    assert.equal(ctx.state.agentInputDrafts.get('a'),'删除第2镜');
  });
  await check('A09-formal-overview-controls-assistant-status',()=>{
    const {ctx}=harness();
    ctx.elements.formalProjectCategory.options=[{value:'launch',textContent:'发布会'}];
    ctx.renderAgentState({project:{name:'甲项目',category_id:'launch',status:'active'},versions:{},blocker:{code:'BRIEF_REQUIRED',title:'缺少 Brief',detail:'旧入口缺少快照'}},
      {stages:[{id:'script',state:'已完成',detail:'脚本已保存'},{id:'review',state:'未开始',detail:'逐项审核并确认整片。'}],blockers:[],next_action:{label:'进入逐镜配图',reason:'请确认镜头配图'}});
    assert.equal(ctx.elements.agentProjectMeta.textContent,'发布会 · 进行中');
    assert.equal(ctx.elements.agentVersionState.textContent,'已有版本');
    assert.equal(ctx.elements.agentReviewState.textContent,'尚未审核');
    assert.equal(ctx.elements.agentBlockerTitle.textContent,'进入逐镜配图');
    assert.equal(ctx.elements.agentBlockerDetail.textContent,'请确认镜头配图');
  });
  await check('I04-late-manual-analysis-never-renders-or-navigates-another-project',async()=>{
    const {ctx,renders,navigations,event}=imageAnalysisHarness();let release;
    ctx.api=(url,options)=>options?.method==='POST'?new Promise(resolve=>{release=resolve;}):Promise.resolve({project_version:1});
    const pending=ctx.submitFormalImageAnalysis(event);await new Promise(resolve=>setImmediate(resolve));
    assert.equal(typeof release,'function');
    ctx.window.location.pathname='/projects/b/materials';ctx.state.formalImageEpoch++;
    const current={pending:1,images:[{image_key:'image-b'}]};ctx.state.formalImages=current;
    release({pending:0,images:[{image_key:'image-a'}]});await pending;
    assert.equal(renders.length,0);assert.equal(navigations.length,0);assert.equal(ctx.state.formalImages,current);
  });
  await check('I05-late-auto-analysis-never-renders-or-updates-other-project',async()=>{
    const {ctx,renders}=imageAnalysisHarness();let release;
    ctx.api=(url,options)=>options?.method==='POST'?new Promise(resolve=>{release=resolve;}):Promise.resolve({project_version:1});
    const pending=ctx.understandFormalImages();await new Promise(resolve=>setImmediate(resolve));
    ctx.window.location.pathname='/projects/b/materials';ctx.state.formalImageEpoch++;ctx.state.formalImages={pending:3,images:[]};
    ctx.elements.formalImageUnderstand.textContent='乙正在分析';ctx.state.formalImageAutoBusy=true;
    release({pending:0,images:[{image_key:'image-a'}],automatic_understanding:{processed:1}});await pending;
    assert.equal(renders.length,0);assert.equal(ctx.elements.formalImageUnderstand.textContent,'乙正在分析');assert.equal(ctx.state.formalImageAutoBusy,true);
  });
  await check('I06-leaving-during-preflight-never-sends-analysis-post',async()=>{
    for(const mode of ['manual','auto']){
      const {ctx,event}=imageAnalysisHarness();let release;let posts=0;
      ctx.api=(url,options)=>{if(options?.method==='POST'){posts++;return Promise.resolve({images:[]});}return new Promise(resolve=>{release=resolve;});};
      const pending=mode==='manual'?ctx.submitFormalImageAnalysis(event):ctx.understandFormalImages();
      ctx.window.location.pathname='/projects/b/materials';ctx.state.formalImageEpoch++;ctx.state.formalImages={pending:1,images:[]};
      release({project_version:1});await pending;assert.equal(posts,0);
    }
  });
  await check('I07-image-change-invalidates-previous-proposal',async()=>{
    const {ctx}=imageProposalHarness();
    const select={dataset:{imageShotSelect:'0'},value:'image-b'};
    ctx.changeFormalImageSelection({target:{closest:()=>select}});
    assert.equal(ctx.state.formalImageWorkspace.shots[0].selected_image_key,'image-b');
    assert.equal(ctx.state.formalImageVersionProposal,null);assert.equal(ctx.elements.formalImageVersionProposal.hidden,true);
    let confirms=0;ctx.api=async()=>{confirms++;return {};};await ctx.confirmFormalImageVersion();assert.equal(confirms,0);
  });
  await check('I08-form-edits-and-temporary-choice-invalidate-proposal',()=>{
    for(const field of ['reason','reuse-reason','confirm']){
      const {ctx,node}=imageProposalHarness();const input=node(`[data-image-shot-${field}="0"]`);
      if(field==='confirm')input.checked=false;else input.value='修改后的理由';
      ctx.captureFormalImageInputs();assert.equal(ctx.state.formalImageVersionProposal,null,field);
    }
    const {ctx}=imageProposalHarness();ctx.toggleFormalImageTemporary({dataset:{imageShotTemporary:'0'},checked:true});
    assert.equal(ctx.state.formalImageVersionProposal,null);
  });
  await check('I09-late-unified-proposal-cannot-restore-an-outdated-confirmation',async()=>{
    const {ctx,node}=imageProposalHarness();let release;
    ctx.state.formalScript={revision:1,project_version:1,shots:ctx.state.formalImageWorkspace.shots};
    ctx.flushCreativeProgress=async()=>true;ctx.openCreativeTool=async()=>true;
    ctx.elements.formalScriptSaveForm.reportValidity=()=>true;
    let rendered=0;ctx.renderFormalScriptRevisionProposal=()=>{rendered++;};
    ctx.api=()=>new Promise(resolve=>{release=resolve;});
    const pending=ctx.saveFormalImageVersion({preventDefault:noop});
    await new Promise(resolve=>setImmediate(resolve));assert.equal(typeof release,'function');
    assert.equal(ctx.elements.formalScriptSave.disabled,true);
    node('[data-image-shot-reuse-reason="0"]').value='等待期间修改了复用依据';ctx.captureFormalImageInputs();
    release({task_id:'outdated',response:{proposal_digest:'b'.repeat(64)}});await pending;
    assert.equal(rendered,0);
    assert.equal(ctx.state.formalScriptRevisionProposal,null);
    assert.equal(ctx.elements.formalScriptSave.disabled,false);
    assert.match(ctx.elements.formalScriptSaveProposal.textContent,/配图已变化，请重新预览版本影响/);
    ctx.state.formalScriptRevisionProposal={task_id:'visible-proposal',response:{type:'action_proposal',draft_only:false}};
    ctx.elements.formalScriptSaveProposal.hidden=false;
    node('[data-image-shot-reuse-reason="0"]').value='预览出现后再次修改复用依据';ctx.captureFormalImageInputs();
    assert.equal(ctx.state.formalScriptRevisionProposal,null);
    assert.equal(ctx.elements.formalScriptSaveProposal.hidden,true);
    assert.equal(ctx.elements.formalScriptSave.disabled,false);
  });
  await check('I10-restore-never-overwrites-fresh-candidates-or-digest',()=>{
    const {ctx,storage}=harness();ctx.showToast=noop;
    storage.set(ctx.formalImageInputKey('a','v1'),JSON.stringify({project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',selected_image_key:'kept',confirmed:true,decision_kind:'image_confirmed',query:'',temporary_reason:'用户理由',candidates:[{image_key:'kept'}],candidate_snapshot_digest:'old-digest'}]}));
    const freshCandidates=[{image_key:'kept'},{image_key:'new-supplement'}];
    const fresh={base_version_id:'v1',project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',candidates:freshCandidates,selected_image_key:'new-supplement',candidate_snapshot_digest:'fresh-digest',query:''}]};
    ctx.restoreFormalImageInputs(fresh,'a');
    assert.equal(fresh.shots[0].candidates,freshCandidates);assert.equal(fresh.shots[0].candidate_snapshot_digest,'fresh-digest');
    assert.equal(fresh.shots[0].selected_image_key,'kept');assert.equal(fresh.shots[0].confirmed,false);assert.equal(fresh.shots[0].temporary_reason,'用户理由');
  });
  await check('I11-custom-query-roundtrip-reloads-authoritative-candidates',async()=>{
    const {ctx,storage}=harness();ctx.showToast=noop;const requests=[];
    storage.set(ctx.formalImageInputKey('a','v1'),JSON.stringify({project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',selected_image_key:'custom-image',confirmed:true,decision_kind:'image_confirmed',query:'舞台人群',candidate_snapshot_digest:'custom-old',candidates:[{image_key:'custom-image'}]}]}));
    ctx.api=async(url)=>{requests.push(url);if(url.includes('/progress?'))return {base_version_id:'v1',project_version:5,revision:0,script_revision:0,selections:[]};return url.includes('/candidates?')?{candidates:[{image_key:'custom-image'},{image_key:'new-custom'}],candidate_snapshot_digest:'custom-new',query:'舞台人群'}:{base_version_id:'v1',project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',candidates:[{image_key:'default-image'}],selected_image_key:'default-image',candidate_snapshot_digest:'default-new',query:''}]};};
    ctx.renderFormalImageWorkspace=(value)=>{ctx.state.formalImageWorkspace=value;};
    await ctx.loadFormalImageWorkspace('a',5);
    const shot=ctx.state.formalImageWorkspace.shots[0];
    assert.deepEqual(requests.map(url=>new URL(url,'http://localhost').pathname),['/api/formal/projects/a/image-workspace','/api/formal/projects/a/image-workspace/progress','/api/formal/projects/a/image-workspace/shots/1/candidates','/api/formal/projects/a/image-workspace/assistant']);assert.equal(new URL(requests[2],'http://localhost').searchParams.get('query'),'舞台人群');
    assert.equal(shot.selected_image_key,'custom-image');assert.equal(shot.candidate_snapshot_digest,'custom-new');assert.equal(shot.query,'舞台人群');assert.equal(shot.confirmed,false);
  });
  await check('I12-typing-query-does-not-change-authoritative-query',()=>{
    const {ctx,node,storage}=imageProposalHarness();ctx.state.formalImageWorkspace.shots[0].query='已查找舞台';
    node('[data-image-shot-query="0"]').value='尚未查找的人群';ctx.captureFormalImageInputs();
    assert.equal(ctx.state.formalImageWorkspace.shots[0].query,'已查找舞台');
    assert.equal(ctx.state.formalImageWorkspace.shots[0].query_input,'尚未查找的人群');
    const saved=JSON.parse(storage.get(ctx.formalImageInputKey('a','v1')));
    assert.equal(saved.shots[0].candidates,undefined);assert.equal(saved.shots[0].query,'已查找舞台');assert.equal(saved.shots[0].query_input,'尚未查找的人群');
  });
  await check('A10-action-completion-refreshes-version-sidebar',async()=>{
    const {ctx}=harness();ctx.window.location.pathname='/assistant';ctx.state.agentProjectId='a';ctx.state.agentEpoch=1;ctx.elements.agentVersionMeta.textContent='共 2 个版本';
    ctx.ensureAgentProjectSession=async()=>({session:{agent_session_id:'session-a'}});ctx.loadAgentCandidatePreviews=async()=>{};
    ctx.api=async(url)=>url.endsWith('/state')?{project:{name:'甲',status:'active'},versions:{latest_version_id:'v3',count:3},blocker:{}}:url.endsWith('/overview')?{stages:[{id:'script',state:'已完成'},{id:'review',state:'未开始'}],next_action:{label:'进入逐镜配图'}}:{tasks:[],timeline:[]};
    await ctx.runAgentTaskAction({projectId:'a',epoch:1,taskId:'t1',digest:'a'.repeat(64)},'confirm',{proposal_digest:'a'.repeat(64)},'action-key');
    assert.equal(ctx.elements.agentVersionMeta.textContent,'共 3 个版本');
  });
  await check('I13-confirm-button-shows-pending-and-recovers-on-failure',async()=>{
    const {ctx,node}=imageProposalHarness();let reject;
    const button=node('[data-confirm-image-version]');button.textContent='确认并创建新版本';
    ctx.api=()=>new Promise((_resolve,fail)=>{reject=fail;});
    const pending=ctx.confirmFormalImageVersion();
    assert.equal(button.disabled,true);assert.equal(button.textContent,'保存中…');
    reject(Error('temporary failure'));await pending;
    assert.equal(button.disabled,false);assert.equal(button.textContent,'确认并创建新版本');
    assert.equal(ctx.state.formalImageVersionProposal.proposal_id,'proposal-a');
  });
  await check('I14-removed-selection-requeries-without-silently-selecting-replacement',async()=>{
    const {ctx,storage}=harness();ctx.showToast=noop;const requests=[];
    storage.set(ctx.formalImageInputKey('a','v1'),JSON.stringify({project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',selected_image_key:'removed',confirmed:true,decision_kind:'image_confirmed',query:'',candidate_snapshot_digest:'old'}]}));
    ctx.api=async(url)=>{requests.push(url);if(url.includes('/progress?'))return {base_version_id:'v1',project_version:5,revision:0,script_revision:0,selections:[]};return url.includes('/candidates?')?{candidates:[{image_key:'replacement'}],candidate_snapshot_digest:'searched'}:{base_version_id:'v1',project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',candidates:[{image_key:'replacement'}],selected_image_key:'replacement',candidate_snapshot_digest:'fresh',query:''}]};};
    ctx.renderFormalImageWorkspace=(value)=>{ctx.state.formalImageWorkspace=value;};
    await ctx.loadFormalImageWorkspace('a',5);
    assert.deepEqual(requests.map(url=>new URL(url,'http://localhost').pathname),['/api/formal/projects/a/image-workspace','/api/formal/projects/a/image-workspace/progress','/api/formal/projects/a/image-workspace/shots/1/candidates','/api/formal/projects/a/image-workspace/assistant']);assert.equal(ctx.state.formalImageWorkspace.shots[0].selected_image_key,'');
    assert.equal(ctx.state.formalImageWorkspace.shots[0].confirmed,false);assert.equal(ctx.state.formalImageWorkspace.shots[0].candidate_snapshot_digest,'searched');
  });
  await check('I15-restored-query-late-response-cannot-cross-projects',async()=>{
    const {ctx,storage}=harness();ctx.showToast=noop;let release;
    storage.set(ctx.formalImageInputKey('a','v1'),JSON.stringify({project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',selected_image_key:'custom',query:'舞台',candidate_snapshot_digest:'prior'}]}));
    ctx.api=async(url)=>url.includes('/progress?')?{base_version_id:'v1',project_version:5,revision:0,script_revision:0,selections:[]}:url.includes('/candidates?')?new Promise(resolve=>{release=resolve;}):{base_version_id:'v1',project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',candidates:[],query:'',candidate_snapshot_digest:'fresh'}]};
    ctx.renderFormalImageWorkspace=()=>{throw Error('late restoration rendered another project');};
    const pending=ctx.loadFormalImageWorkspace('a',5);
    await new Promise(resolve=>setImmediate(resolve));assert.equal(typeof release,'function');
    const current={base_version_id:'b-version',shots:[]};ctx.state.formalImageWorkspace=current;ctx.state.formalImageEpoch++;ctx.window.location.pathname='/projects/b/shot-images';
    release({candidates:[{image_key:'custom'}],candidate_snapshot_digest:'late'});await pending;
    assert.equal(ctx.state.formalImageWorkspace,current);
  });
  await check('I16-custom-query-confirmation-restores-only-matching-fresh-digest',async()=>{
    const {ctx,storage}=harness();ctx.showToast=noop;
    storage.set(ctx.formalImageInputKey('a','v1'),JSON.stringify({project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',selected_image_key:'custom',confirmed:true,decision_kind:'image_confirmed',query:'舞台',query_input:'尚未查找的人群',candidate_snapshot_digest:'same'}]}));
    ctx.api=async(url)=>url.includes('/progress?')?{base_version_id:'v1',project_version:5,revision:0,script_revision:0,selections:[]}:url.includes('/candidates?')?{candidates:[{image_key:'custom'}],candidate_snapshot_digest:'same'}:{base_version_id:'v1',project_version:5,shots:[{shot_id:'shot-1',shot_no:'1',candidates:[],candidate_snapshot_digest:'default',query:''}]};
    ctx.renderFormalImageWorkspace=(value)=>{ctx.state.formalImageWorkspace=value;};
    await ctx.loadFormalImageWorkspace('a',5);
    const shot=ctx.state.formalImageWorkspace.shots[0];
    assert.equal(shot.confirmed,true);assert.equal(shot.query,'舞台');assert.equal(shot.query_input,'尚未查找的人群');
  });
  await check('I17-search-results-preserve-adopted-image-and-its-confirmation',async()=>{
    const {ctx,node}=imageProposalHarness();let releaseSearch;
    ctx.state.formalScript={shots:ctx.state.formalImageWorkspace.shots};
    ctx.api=()=>new Promise(resolve=>{releaseSearch=resolve;});
    ctx.loadCreativePickerCard=async()=>{};ctx.loadCreativePickerDetail=async()=>{};
    const search=ctx.searchFormalImageShot({dataset:{imageShotSearch:'0'}});
    await new Promise(resolve=>setImmediate(resolve));assert.equal(typeof releaseSearch,'function');
    releaseSearch({candidates:[{image_key:'image-b'}],candidate_snapshot_digest:'digest-b'});await search;
    const shot=ctx.state.formalImageWorkspace.shots[0];
    assert.equal(shot.selected_image_key,'image-a');assert.equal(shot.confirmed,true);
    assert.equal(shot.candidate_snapshot_digest,'digest');
    assert.equal(ctx.state.formalImageVersionProposal.proposal_id,'proposal-a');
    const picker=vm.runInContext('creativeImagePicker',ctx);
    assert.equal(picker.candidates[0].image_key,'image-b');
    assert.equal(picker.selectedContext.digest,'digest');
  });
  await check('I18-upload-return-scrolls-only-the-supplement-card',async()=>{
    for(const supplement of ['image-new','','missing','stale']){
      const {ctx,node}=harness();const actions=[];
      ctx.window.location.pathname='/projects/a/materials';
      ctx.window.location.search=supplement?`?return_to=shot-images&focus_shot=1&supplement_image_key=${supplement==='stale'?'image-new':supplement}`:'';
      if(supplement==='stale')ctx.window.location.search+='&pick=library';
      ctx.state.formalBriefAvailable=true;ctx.state.agentAuth='test-auth';ctx.state.agentWorkspaceId='test-workspace';
      const cards=['image-old','image-new'].map(key=>{
        const card=node(`card-${key}`);card.dataset.formalImageKey=key;
        card.querySelector=()=>node(`review-${key}`);
        card.scrollIntoView=()=>actions.push(`scroll:${key}`);card.focus=()=>actions.push(`focus:${key}`);
        return card;
      });
      ctx.elements.formalImageGrid.querySelectorAll=()=>cards;
      ctx.api=async()=>({project_version:1,images:[]});ctx.renderFormalImages=noop;
      let initialActions;
      ctx.loadFormalImagePreviews=async()=>{initialActions=[...actions];if(supplement==='stale')ctx.state.formalImageEpoch++;};
      ctx.loadFormalDefaultImages=async()=>actions.push('library');
      await ctx.loadFormalImages('a');
      assert.deepEqual(initialActions,['image-new','stale'].includes(supplement)?['scroll:image-new','focus:image-new']:[]);
      assert.deepEqual(actions,initialActions,'preview completion must not repeat positioning or act on an expired page');
      if(supplement==='image-new')assert.equal(node('review-image-new').open,true);
    }
  });
  await check('I19-initial-previews-survive-replaced-targets-in-same-workspace',async()=>{
    const {ctx,targets,requests,replaceTargets}=imagePreviewHarness();
    const pending=ctx.loadFormalShotImagePreviews('a',1);replaceTargets();
    for(const [key,request]of requests)request.resolve({key});await pending;
    assert.equal(ctx.state.formalImagePreviewUrls.size,2);
    assert.equal(targets.get('#formal-image-shot-preview-0').images[0]?.src,'blob:image-a');
    assert.equal(targets.get('#formal-image-shot-preview-1').images[0]?.src,'blob:image-b');
  });
  await check('I20-initial-preview-discards-response-after-image-selection-changes',async()=>{
    const {ctx,targets,requests}=imagePreviewHarness();
    const pending=ctx.loadFormalShotImagePreviews('a',1);
    ctx.state.formalImageWorkspace.shots[1].selected_image_key='image-c';
    for(const [key,request]of requests)request.resolve({key});await pending;
    assert.equal(ctx.state.formalImagePreviewUrls.has('image-a'),true);assert.equal(ctx.state.formalImagePreviewUrls.has('image-b'),false);
    assert.equal(targets.get('#formal-image-shot-preview-1').images.length,0);
  });
  await check('I21-initial-preview-errors-use-current-target-and-image-selection',async()=>{
    const {ctx,targets,requests,replaceTargets}=imagePreviewHarness();
    const pending=ctx.loadFormalShotImagePreviews('a',1);replaceTargets();
    ctx.state.formalImageWorkspace.shots[1].selected_image_key='image-c';
    for(const request of requests.values())request.reject(Error('preview failed'));await pending;
    assert.match(targets.get('#formal-image-shot-preview-0').innerHTML,/图片读取失败/);
    assert.equal(targets.get('#formal-image-shot-preview-1').innerHTML,'');
  });
  await check('I22-late-initial-error-preserves-successful-same-image-preview',async()=>{
    const {ctx,targets,requests,replaceTargets}=imagePreviewHarness();
    ctx.updateFormalImageShotPreview=index=>{
      targets.get(`#formal-image-shot-preview-${index}`).innerHTML=ctx.formalImageDecisionPreview(ctx.state.formalImageWorkspace.shots[index],index);
    };
    const initial=ctx.loadFormalShotImagePreviews('a',1);
    const initialA=requests.get('image-a'),initialB=requests.get('image-b');
    replaceTargets();
    const refreshed=ctx.loadFormalShotCandidatePreview(0);
    assert.equal(requests.get('image-a'),initialA,'same image preview must share the in-flight request');
    requests.get('image-a').resolve({key:'refreshed-image-a'});await refreshed;
    assert.match(targets.get('#formal-image-shot-preview-0').innerHTML,/<img src="blob:refreshed-image-a"/);
    initialA.reject(Error('earlier request failed'));initialB.resolve({key:'image-b'});await initial;
    assert.equal(ctx.state.formalImagePreviewUrls.get('image-a'),'blob:refreshed-image-a');
    assert.match(targets.get('#formal-image-shot-preview-0').innerHTML,/<img src="blob:refreshed-image-a"/);
  });
  console.log(checks.join('\n'));if(checks.some(x=>x.startsWith('STEP_FAIL')))process.exitCode=1;
})();
