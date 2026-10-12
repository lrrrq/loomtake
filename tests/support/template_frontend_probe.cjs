const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};
function harness(page = 'brief') {
  const nodes = new Map();
  function node(key) {
    if (!nodes.has(key)) nodes.set(key, {value:'',files:[],textContent:'',innerHTML:'',dataset:{},hidden:false,disabled:false,checked:false,open:false,children:[],isConnected:true,classList:{add:noop,remove:noop,toggle:noop},querySelector:node,querySelectorAll:()=>[],setAttribute:noop,removeAttribute:noop,replaceChildren(...items){this.children=items;},append(item){this.children.push(item);},addEventListener:noop,focus:noop,scrollIntoView:noop,reportValidity:()=>true});
    return nodes.get(key);
  }
  const storage={getItem:()=>null,setItem:noop,removeItem:noop};
  const ctx=vm.createContext({URL,URLSearchParams,Headers,FormData,Blob,console,Map,Set,Date,JSON,localStorage:storage,sessionStorage:storage,document:{querySelector:node,querySelectorAll:()=>[],createElement:()=>({}),addEventListener:noop},window:{location:{pathname:`/projects/a/${page}`,search:'',origin:'http://localhost'},addEventListener:noop},setTimeout:()=>1,clearTimeout:noop});
  vm.runInContext(source.slice(0,source.indexOf('\nelements.formalOwnerConfirm.addEventListener')),ctx);
  ctx.state=vm.runInContext('state',ctx);ctx.elements=vm.runInContext('elements',ctx);
  ctx.state.agentWorkspaceId='w';ctx.state.agentAuth='auth';ctx.state.formalBriefAvailable=true;ctx.showToast=noop;
  return {ctx,node};
}
const catalog=(id='a',version=4)=>({project_id:id,project_version:version,category_id:'annual',selected_ref:'ref-a',can_trial:true,notice:'',items:[{ref:'ref-a',label:'原模板',version:1,source_name:'原模板.xlsx',shot_count:3,structures:[{name:'开场',start:1,end:3,shot_count:3}],warnings:[],is_default:true,selected:true},{ref:'ref-b',label:'新模板',version:2,source_name:'新模板.xlsx',shot_count:5,structures:[{name:'启动',start:1,end:5,shot_count:5}],warnings:[],selected:false}]});
async function ready() {const h=harness();h.ctx.api=async()=>catalog();await h.ctx.loadFormalTemplates('a','brief');return h;}
const checks=[];
async function check(name,fn){try{await fn();checks.push(`STEP_PASS|${name}`);}catch(error){checks.push(`STEP_FAIL|${name}|${error.message}`);}}
(async()=>{
  await check('T01-categories-use-server-labels-and-retry-without-fallback',async()=>{
    const {ctx}=harness('projects');let calls=0;ctx.api=async p=>{assert.equal(p,'/api/formal/categories');calls++;if(calls===1)throw Error('offline');return {items:[{category_id:'future',label:'新增类别'}]};};
    await ctx.loadFormalCategories();assert.equal(ctx.elements.formalProjectCategory.disabled,true);assert.equal(ctx.elements.formalCategoryRetry.hidden,false);assert.equal(ctx.state.formalCategories.length,0);
    await ctx.loadFormalCategories();assert.equal(ctx.elements.formalProjectCategory.disabled,false);assert.equal(ctx.state.formalCategories[0].category_id,'future');assert.match(ctx.elements.formalProjectCategory.innerHTML,/新增类别/);assert.doesNotMatch(ctx.elements.formalProjectCategory.innerHTML,/value="launch"/);
  });
  await check('T02-catalog-late-response-cannot-enter-another-project',async()=>{
    const {ctx}=harness();let done;ctx.api=()=>new Promise(r=>done=r);const pending=ctx.loadFormalTemplates('a','brief');ctx.window.location.pathname='/projects/b/brief';ctx.syncFormalTemplateRoute();done(catalog());await pending;assert.equal(ctx.state.formalTemplates,null);
  });
  await check('T03-selection-invalidates-trial-and-confirmation',async()=>{
    const {ctx}=await ready();ctx.state.formalTemplateTrial={plan:{shot_count:3}};ctx.elements.formalTemplateConfirm.checked=true;const before=ctx.state.formalTemplateInputRevision;ctx.elements.formalTemplateSelect.value='ref-b';ctx.changeFormalTemplate();assert.equal(ctx.state.formalTemplateTrial,null);assert.equal(ctx.elements.formalTemplateConfirm.checked,false);assert.equal(ctx.elements.formalTemplateApply.disabled,true);assert.ok(ctx.state.formalTemplateInputRevision>before);
  });
  await check('T04-trial-late-response-cannot-restore-old-choice',async()=>{
    const {ctx}=await ready();let done;const requests=[];ctx.api=(p,o)=>{requests.push([p,JSON.parse(o.body)]);return new Promise(r=>done=r);};const pending=ctx.trialFormalTemplate();ctx.elements.formalTemplateSelect.value='ref-b';ctx.changeFormalTemplate();done({template:catalog().items[0],plan:{shot_count:3,shots:[]}});await pending;assert.equal(ctx.state.formalTemplateTrial,null);assert.equal(ctx.state.formalPlan,null);assert.equal(requests[0][1].template_ref,'ref-a');assert.equal(requests[0][1].generation_mode,'text_only_draft');
  });
  await check('T05-bind-requires-confirmed-current-choice-and-server-version',async()=>{
    const {ctx}=await ready();const calls=[];ctx.api=async(p,o)=>{calls.push([p,JSON.parse(o.body)]);return {...catalog('a',5),selected_ref:'ref-b',items:catalog().items.map(i=>({...i,selected:i.ref==='ref-b'})),notice:'模板已更新'};};ctx.elements.formalTemplateSelect.value='ref-b';ctx.changeFormalTemplate();await ctx.selectFormalTemplate();assert.equal(calls.length,0);ctx.elements.formalTemplateConfirm.checked=true;await ctx.selectFormalTemplate();assert.equal(calls.length,1);assert.equal(calls[0][1].expected_project_version,4);assert.equal(calls[0][1].confirmed,true);assert.equal(calls[0][1].template_ref,'ref-b');assert.equal(ctx.state.formalTemplates.project_version,5);assert.equal(ctx.state.formalPlan,null);assert.match(ctx.elements.formalTemplateCurrent.textContent,/新模板/);
  });
  await check('T06-unconfirmed-brief-still-allows-template-binding',async()=>{
    const {ctx}=harness();ctx.api=async()=>({...catalog(),can_trial:false});await ctx.loadFormalTemplates('a','brief');ctx.elements.formalTemplateSelect.value='ref-b';ctx.changeFormalTemplate();ctx.elements.formalTemplateConfirm.checked=true;ctx.renderFormalTemplateControls();assert.equal(ctx.elements.formalTemplateTrialButton.disabled,true);assert.equal(ctx.elements.formalTemplateApply.disabled,false);
  });
  await check('T07-import-does-not-bind-and-can-retry-after-failure',async()=>{
    const {ctx}=await ready();ctx.elements.formalTemplateFile.files=[new Blob(['xlsx'])];let count=0;ctx.api=async(p,o)=>{count++;assert.ok(o.body instanceof FormData);assert.equal(o.body.get('workspace_id'),'w');assert.equal(o.body.get('expected_project_version'),'4');if(count===1)throw Error('bad file');return {...catalog(),preview:catalog().items[1]};};await ctx.importFormalTemplate();assert.equal(ctx.state.formalTemplateBusy,'');assert.equal(ctx.elements.formalTemplateImport.disabled,false);await ctx.importFormalTemplate();assert.equal(ctx.state.formalTemplates.project_version,4);assert.equal(ctx.state.formalTemplates.selected_ref,'ref-a');assert.equal(ctx.state.formalTemplateRef,'ref-b');assert.equal(ctx.elements.formalTemplateConfirm.checked,false);
  });
  await check('T08-old-import-cannot-render-after-leave-and-return',async()=>{
    const {ctx}=await ready();ctx.elements.formalTemplateFile.files=[new Blob(['xlsx'])];let done;ctx.api=()=>new Promise(r=>done=r);const pending=ctx.importFormalTemplate();ctx.window.location.pathname='/projects/a/materials';ctx.syncFormalTemplateRoute();ctx.window.location.pathname='/projects/a/brief';ctx.syncFormalTemplateRoute();done({...catalog(),preview:catalog().items[1]});await pending;assert.equal(ctx.state.formalTemplates,null);assert.equal(ctx.state.formalTemplateRef,'');
  });
  await check('T09-trial-renders-server-shots-without-enabling-generation',async()=>{
    const {ctx}=await ready();ctx.api=async()=>({template:catalog().items[0],plan:{shot_count:2,total_duration_seconds:30,shots:[{shot_no:1,structure:'开场',primary_event:'介绍',duration_seconds:10},{shot_no:2,structure:'结束',primary_event:'谢幕',duration_seconds:20}]}});await ctx.trialFormalTemplate();assert.match(ctx.elements.formalTemplateTrialResult.innerHTML,/介绍/);assert.match(ctx.elements.formalTemplateTrialResult.innerHTML,/谢幕/);assert.equal(ctx.state.formalPlan,null);assert.equal(ctx.elements.formalTemplateTrialResult.hidden,false);
  });
  await check('T10-archived-project-controls-remain-readonly',async()=>{
    const {ctx}=await ready();ctx.state.formalProjects=[{project_id:'a',status:'archived'}];ctx.elements.formalTemplateSelect.value='ref-b';ctx.changeFormalTemplate();ctx.elements.formalTemplateConfirm.checked=true;ctx.renderFormalTemplateControls();assert.equal(ctx.elements.formalTemplateApply.disabled,true);assert.equal(ctx.elements.formalTemplateImport.disabled,true);let calls=0;ctx.api=async()=>{calls++;};await ctx.selectFormalTemplate();await ctx.trialFormalTemplate();assert.equal(calls,0);
  });
  await check('T11-unbound-default-can-be-fixed-with-explicit-confirmation',async()=>{
    const {ctx}=harness();ctx.api=async()=>({...catalog(),is_bound:false});await ctx.loadFormalTemplates('a','brief');ctx.elements.formalTemplateConfirm.checked=true;ctx.renderFormalTemplateControls();assert.equal(ctx.elements.formalTemplateApply.disabled,false);let sent;ctx.api=async(p,o)=>{sent=JSON.parse(o.body);return {...catalog('a',5),is_bound:true};};await ctx.selectFormalTemplate();assert.equal(sent.template_ref,'ref-a');assert.equal(sent.confirmed,true);assert.equal(ctx.state.formalTemplates.is_bound,true);assert.equal(ctx.elements.formalTemplateApply.disabled,true);
  });
  await check('T12-unavailable-bound-ref-is-not-displayed-as-another-default',async()=>{
    const {ctx}=harness();ctx.api=async()=>({...catalog(),is_bound:true,selected_ref:'missing-ref',items:catalog().items.map(item=>({...item,selected:false}))});await ctx.loadFormalTemplates('a','brief');assert.equal(ctx.formalTemplateSelected(),undefined);assert.equal(ctx.state.formalTemplateRef,'');assert.match(ctx.elements.formalTemplateCurrent.textContent,/不可用/);ctx.elements.formalTemplateSelect.value='ref-a';ctx.changeFormalTemplate();ctx.elements.formalTemplateConfirm.checked=true;ctx.renderFormalTemplateControls();assert.equal(ctx.elements.formalTemplateApply.disabled,false);
  });
  process.stdout.write(checks.join('\n')+'\n');if(checks.some(x=>x.startsWith('STEP_FAIL')))process.exitCode=1;
})();
