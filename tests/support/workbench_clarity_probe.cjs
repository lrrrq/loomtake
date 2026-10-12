const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { TextEncoder } = require('node:util');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};
function harness(page = 'plan') {
  const nodes = new Map();
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, {value:'',files:[],textContent:'',innerHTML:'',dataset:{},hidden:false,disabled:false,checked:false,open:false,isConnected:true,options:[],classList:{add:noop,remove:noop,toggle:noop},querySelector:node,querySelectorAll:()=>[],getAttribute:()=>'',setAttribute:noop,removeAttribute:noop,remove:noop,replaceChildren:noop,addEventListener:noop,focus:noop,scrollIntoView:noop,reportValidity:()=>true,insertAdjacentHTML(_position,html){this.innerHTML+=html;}});
    return nodes.get(key);
  };
  const saved = new Map();
  const storage = {getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value),removeItem:key=>saved.delete(key)};
  const ctx = vm.createContext({URL,URLSearchParams,Headers,FormData,Blob,TextEncoder,console,Map,Set,Date,JSON,localStorage:storage,sessionStorage:storage,document:{querySelector:node,querySelectorAll:()=>[],createElement:()=>({}),addEventListener:noop},window:{location:{pathname:`/projects/a/${page}`,search:'',origin:'http://localhost',reload:noop},addEventListener:noop},setTimeout:()=>1,clearTimeout:noop});
  vm.runInContext(source.slice(0,source.indexOf('\nelements.formalOwnerConfirm.addEventListener')),ctx);
  ctx.state=vm.runInContext('state',ctx);ctx.elements=vm.runInContext('elements',ctx);
  ctx.syncCreativePrimaryAction=noop;ctx.syncCreativeShotVisibility=noop;ctx.renderCreativeOutline=noop;ctx.syncCreativeNextAction=noop;ctx.creativeState().imageMode='all';ctx.state.formalScript={shots:[{shot_id:'shot-1',shot_no:'1'},{shot_id:'shot-2',shot_no:'2'}]};
  ctx.state.agentWorkspaceId='w';ctx.state.agentAuth='auth';ctx.showToast=noop;
  return {ctx,node};
}
const checks=[];
async function check(name,fn){try{await fn();checks.push(`STEP_PASS|${name}`);}catch(error){checks.push(`STEP_FAIL|${name}|${error.message}`);}}
(async()=>{
  await check('C01-create-project-needs-only-name-and-workspace-lock',async()=>{
    const {ctx}=harness('new');ctx.elements.formalProjectName.value='匿名项目';ctx.state.formalWorkspaceVersion=4;ctx.elements.formalProjectCategory.disabled=true;
    let payload,route;ctx.api=async(_url,options)=>{payload=JSON.parse(options.body);return {project_id:'new'};};ctx.loadFormalProjects=async()=>{};ctx.navigateFormal=value=>route=value;
    await ctx.createFormalProject({preventDefault:noop});assert.equal(payload.name,'匿名项目');assert.equal(payload.expected_workspace_version,4);assert.ok(!('category_id' in payload));assert.equal(route,'/projects/new/script');
  });
  await check('C02-plan-source-keeps-authoritative-workbook-sheet-row',()=>{
    const {ctx}=harness();assert.equal(ctx.planSourceLabel({source_file:'匿名模板.xlsx',source_sheet:'模版',source_row:7}),'匿名模板.xlsx · 模版 Sheet · 第 7 行');
    const html=ctx.planFieldDecisions([{field:'structure',decision_status:'applied',final_value:'开场'},{field:'sound',decision_status:'downgraded',final_value:'现场声'}]);assert.match(html,/已采用/);assert.match(html,/按需求调整/);assert.doesNotMatch(html,/待核对/);
  });
  await check('C03-plan-summary-uses-frozen-plan-template-not-project-selection',()=>{
    const {ctx}=harness();ctx.state.formalTemplates={items:[{label:'后选的模板',version:9,selected:true}]};
    ctx.renderFormalPlanTemplateSummary({shot_count:5,template_summary:{label:'原模板',package_version:2,selection_mode:'default',original_segments:[{structure:'开场',shot_count:1},{structure:'结束',shot_count:2}],current_segments:[{structure:'开场',shot_count:2},{structure:'结束',shot_count:3}],structure_change_count:1,position_requirement_count:2}});
    const html=ctx.elements.formalPlanTemplateSummary.innerHTML;assert.match(html,/原模板/);assert.match(html,/版本 2/);assert.match(html,/5 镜/);assert.match(html,/按 Brief 调整 1/);assert.doesNotMatch(html,/后选的模板/);
  });
  await check('C04-group-selection-only-selects-open-items-in-the-chosen-group',()=>{
    const {ctx,node}=harness('review');ctx.state.formalReview={review_started:true,issues:[{status:'open',focus_required:true},{status:'open',focus_required:false},{status:'approve',focus_required:false}]};
    ctx.selectFormalReviewGroup('others');assert.equal(node('[data-review-select="0"]').checked,false);assert.equal(node('[data-review-select="1"]').checked,true);assert.equal(node('[data-review-select="2"]').checked,false);
  });
  await check('C05-update-check-never-reloads-and-refresh-protects-pending-inputs',async()=>{
    const {ctx}=harness();ctx.state.uiBuildId='old';let reloads=0;ctx.window.location.reload=()=>reloads++;ctx.api=async()=>({ui_build_id:'new'});
    await ctx.checkUiBuild();assert.equal(ctx.elements.uiUpdateBanner.hidden,false);assert.equal(reloads,0);
    ctx.elements.formalBriefText.value='未提交的客户要求';ctx.refreshUpdatedUi();assert.equal(reloads,0);
    ctx.elements.formalBriefText.value='';ctx.refreshUpdatedUi();assert.equal(reloads,1);
  });
  await check('C06-library-page-selection-survives-search-and-proposes-only-explicit-shot',async()=>{
    const {ctx}=harness('materials');ctx.state.formalImageProjectVersion=4;
    const candidate=key=>({candidate_key:key,caption:key,preview_url:`/preview/${key}`});
    const catalog=(key='page-two')=>({selection_mode:'category_library_browse',page:2,page_size:24,total_groups:30,has_more:false,shots:[{shot_id:'shot-1',shot_no:'1',state:'ready',selected_candidate_key:'a',candidates:[candidate('a')]},{shot_id:'shot-2',shot_no:'2',state:'ready',selected_candidate_key:'b',candidates:[candidate('b')]}],library_candidates:[candidate(key)]});
    ctx.api=async()=>catalog();ctx.readFormalImagePreview=async()=>new Blob(['preview']);await ctx.loadFormalDefaultImages({view:'library',page:2,shotNo:'2'});
    ctx.useFormalLibraryCandidate('page-two');assert.equal(ctx.state.formalDefaultImages.shots[1].selected_candidate_key,'page-two');
    ctx.api=async()=>catalog('search-result');await ctx.loadFormalDefaultImages({view:'library',query:'新搜索',page:1,shotNo:'2'});assert.equal(ctx.state.formalDefaultImages.shots[1].selected_candidate_key,'page-two');
    let payload;ctx.api=async(_url,options)=>{payload=JSON.parse(options.body);return {task_id:'proposal',response:{proposal_digest:'digest'}};};await ctx.proposeFormalDefaultImages();
    assert.deepEqual(JSON.parse(JSON.stringify(payload.selections)),[{shot_no:'2',candidate_key:'page-two'}]);
  });
  await check('C07-late-library-page-never-replaces-new-search-or-another-project',async()=>{
    const {ctx}=harness('materials');ctx.state.formalImageProjectVersion=4;const pending=[];
    ctx.api=()=>new Promise(resolve=>pending.push(resolve));ctx.readFormalImagePreview=async()=>new Blob(['preview']);
    const first=ctx.loadFormalDefaultImages({view:'library',query:'旧'}),second=ctx.loadFormalDefaultImages({view:'library',query:'新'});
    pending[1]({selection_mode:'category_library_multi',query:'新',library_candidates:[]});await second;
    pending[0]({selection_mode:'category_library_multi',query:'旧',library_candidates:[]});await first;assert.equal(ctx.state.formalDefaultImages.query,'新');
    const third=ctx.loadFormalDefaultImages({view:'library',query:'离页'});ctx.window.location.pathname='/projects/b/materials';pending[2]({selection_mode:'category_library_multi',query:'离页',library_candidates:[]});await third;assert.equal(ctx.state.formalDefaultImages.query,'新');
  });
  await check('C08-explicit-subset-is-usable-without-selecting-reference-only-shots',async()=>{
    const {ctx}=harness('materials');ctx.state.formalImageProjectVersion=4;ctx.state.formalDefaultImages={shots:[{shot_id:'shot-1',shot_no:'1',state:'ready',selected_candidate_key:'a',candidates:[{candidate_key:'a'}]},{shot_id:'shot-2',shot_no:'2',state:'ready',selected_candidate_key:'',candidates:[{candidate_key:'reference',default_eligible:false}]}]};
    assert.equal(ctx.formalDefaultAdoptionReady(),false);ctx.state.formalDefaultImageSelections.set('1',{candidate_key:'a'});ctx.state.formalImagePreviewUrls.set('default:a','blob:a');assert.equal(ctx.formalDefaultAdoptionReady(),true);
    let payload;ctx.api=async(_url,options)=>{payload=JSON.parse(options.body);return {task_id:'p',response:{proposal_digest:'d'}};};await ctx.proposeFormalDefaultImages();
    assert.deepEqual(JSON.parse(JSON.stringify(payload.selections)),[{shot_no:'1',candidate_key:'a'}]);assert.equal(ctx.state.formalDefaultImages.shots[1].selected_candidate_key,'');
  });
  await check('C09-new-selection-invalidates-inflight-adoption-proposal',async()=>{
    const {ctx}=harness('materials');ctx.state.formalImageProjectVersion=4;ctx.state.formalDefaultImageShotNo='1';ctx.state.formalDefaultImages={shots:[{shot_id:'shot-1',shot_no:'1',selected_candidate_key:'a',candidates:[{candidate_key:'a'}]}],library_candidates:[{candidate_key:'b',caption:'b'}]};ctx.state.formalImagePreviewUrls.set('default:a','blob:a');ctx.state.formalImagePreviewUrls.set('default:b','blob:b');
    ctx.state.formalDefaultImageSelections.set('1',{candidate_key:'a'});let done;ctx.api=()=>new Promise(resolve=>done=resolve);const pending=ctx.proposeFormalDefaultImages();ctx.useFormalLibraryCandidate('b');done({task_id:'old',response:{proposal_digest:'d'}});await pending;assert.equal(ctx.state.formalDefaultImageProposal,null);assert.equal(ctx.state.formalDefaultImages.shots[0].selected_candidate_key,'b');
  });
  await check('C10-unsubmitted-review-notes-survive-render-for-update-protection',()=>{
    const {ctx,node}=harness('review');ctx.state.formalReviewProjectId='a';ctx.state.formalReview={issues:[{issue_key:'one',status:'open'}]};node('[data-review-note="0"]').value='仍未提交的意见';assert.equal(ctx.hasPendingUiInputs(),true);node('[data-review-note="0"]').value='';assert.equal(ctx.hasPendingUiInputs(),false);
  });
  await check('C11-supplementing-category-keeps-existing-brief-input-and-does-not-post',()=>{
    const {ctx}=harness('brief');ctx.state.formalBriefProjectId='a';ctx.state.formalCategories=[{category_id:'music',label:'音乐节'}];ctx.elements.formalProjectCategory.value='music';ctx.elements.formalBriefText.value='保留用户正在填写的要求';ctx.supplementFormalBriefCategory();assert.equal(ctx.elements.formalBriefText.value,'保留用户正在填写的要求\n脚本类型: 音乐节');assert.equal(ctx.state.formalBriefInputs.get('a').editingField,'脚本类型');
  });
  await check('C12-adoption-restores-exact-reference-choice-on-both-return-paths-without-confirming',async()=>{
    for(const returning of [false,true]) {
      const {ctx}=harness('materials');ctx.window.location.search=returning?'?return_to=shot-images&focus_shot=1':'';
      ctx.sessionStorage.setItem(ctx.formalImageInputKey('a','v1'),JSON.stringify({project_version:3,shots:[{shot_id:'shot-2',shot_no:'2',decision_kind:'temporary_text_basis',temporary_reason:'保留另一镜的理由',confirmed:true}]}));
      ctx.state.formalDefaultImageProposal={task_id:'adopt',response:{proposal_digest:'d'}};
      ctx.api=async()=>({response:{type:'formal_default_images_adopted',base_version_id:'v1',project_version:4,adopted_selections:[{shot_id:'shot-1',shot_no:'1',image_key:'reference',content_sha256:'sha-reference'}]}});
      let route;ctx.navigateFormal=async value=>route=value;ctx.loadFormalImages=async()=>{};
      await ctx.confirmFormalDefaultImages();
      const fresh={base_version_id:'v1',project_version:4,shots:[{shot_id:'shot-1',shot_no:'1',selected_image_key:'default',confirmed:true,candidate_snapshot_digest:'fresh',candidates:[{image_key:'default',content_sha256:'sha-default'},{image_key:'reference',content_sha256:'sha-reference',default_eligible:false}]},{shot_id:'shot-2',shot_no:'2',candidates:[]}]};
      ctx.restoreFormalImageInputs(fresh,'a');assert.equal(fresh.shots[0].selected_image_key,'reference');assert.equal(fresh.shots[0].confirmed,false);assert.equal(fresh.shots[0].decision_kind,'image_confirmed');assert.equal(fresh.shots[0].candidates[1].default_eligible,false);assert.equal(fresh.shots[1].temporary_reason,'保留另一镜的理由');
      if(returning)assert.equal(route,'/projects/a/script?panel=images');else assert.match(ctx.elements.formalImageUploadState.innerHTML,/继续逐镜确认/);
    }
  });
  await check('C13-adoption-never-crosses-project-base-version-or-content-identity',async()=>{
    const {ctx}=harness('materials');ctx.state.formalDefaultImageProposal={task_id:'adopt',response:{proposal_digest:'d'}};ctx.loadFormalImages=async()=>{};
    ctx.api=async()=>({response:{base_version_id:'v1',project_version:4,adopted_selections:[{shot_id:'shot-1',shot_no:'1',image_key:'reference',content_sha256:'right-sha'}]}});await ctx.confirmFormalDefaultImages();
    const workspace=base=>({base_version_id:base,project_version:4,shots:[{shot_id:'shot-1',shot_no:'1',selected_image_key:'default',confirmed:true,candidates:[{image_key:'reference',content_sha256:'wrong-sha'}]}]});
    const otherProject=workspace('v1');ctx.restoreFormalImageInputs(otherProject,'b');assert.equal(otherProject.shots[0].selected_image_key,'default');
    const otherVersion=workspace('v2');ctx.restoreFormalImageInputs(otherVersion,'a');assert.equal(otherVersion.shots[0].selected_image_key,'default');
    const wrongContent=workspace('v1');ctx.restoreFormalImageInputs(wrongContent,'a');assert.equal(wrongContent.shots[0].selected_image_key,'');assert.equal(wrongContent.shots[0].confirmed,false);
  });
  await check('C14-plan-details-start-compact-and-show-real-exceptions',()=>{
    const {ctx}=harness();const plan={shots:[],capacity_report:{status:'ready'},decision_counts:{},audit:{status:'pass'}};ctx.renderFormalPlanPreview(plan);
    assert.equal(ctx.elements.formalPlanCapacityDetails.open,false);assert.equal(ctx.elements.formalPlanShotsDetails.open,false);
    ctx.renderFormalPlanPreview({...plan,capacity_report:{status:'needs_clarification'},decision_counts:{unresolved_conflicts:1}});assert.equal(ctx.elements.formalPlanCapacityDetails.open,true);assert.equal(ctx.elements.formalPlanShotsDetails.open,true);
  });
  await check('C15-rapid-cross-shot-choices-register-immediately-and-keep-both-reversed-previews',async()=>{
    const {ctx}=harness('materials');ctx.state.formalImageProjectVersion=4;
    const candidate=key=>({candidate_key:key,preview_url:`/preview/${key}`});
    ctx.state.formalDefaultImages={shots:[{shot_id:'shot-1',shot_no:'1',selected_candidate_key:'',candidates:[candidate('a')]},{shot_id:'shot-2',shot_no:'2',selected_candidate_key:'',candidates:[candidate('b')]}]};
    const pending=[];ctx.readFormalImagePreview=()=>new Promise(resolve=>pending.push(resolve));let rendered=0;ctx.renderFormalDefaultImages=()=>rendered++;
    const first=ctx.selectFormalDefaultCandidate(0,'a'),second=ctx.selectFormalDefaultCandidate(1,'b');
    assert.equal(ctx.state.formalDefaultImageSelections.get('1')?.candidate_key,'a');assert.equal(ctx.state.formalDefaultImageSelections.get('2')?.candidate_key,'b');
    pending[1](new Blob(['b']));await second;assert.equal(ctx.formalDefaultAdoptionReady(),false);
    pending[0](new Blob(['a']));await first;assert.equal(ctx.formalDefaultAdoptionReady(),true);assert.equal(rendered,0);
    let payload;ctx.api=async(_url,options)=>{payload=JSON.parse(options.body);return{task_id:'p',response:{proposal_digest:'d'}};};await ctx.proposeFormalDefaultImages();
    assert.deepEqual(payload.selections,[{shot_no:'1',candidate_key:'a'},{shot_no:'2',candidate_key:'b'}]);
  });
  await check('C16-same-shot-old-preview-cannot-replace-latest-choice',async()=>{
    const {ctx}=harness('materials');ctx.state.formalImageProjectVersion=4;ctx.state.formalDefaultImages={shots:[{shot_id:'shot-1',shot_no:'1',candidates:['a','b'].map(key=>({candidate_key:key,preview_url:`/preview/${key}`}))}]};
    const pending=[];ctx.readFormalImagePreview=()=>new Promise(resolve=>pending.push(resolve));
    const old=ctx.selectFormalDefaultCandidate(0,'a'),latest=ctx.selectFormalDefaultCandidate(0,'b');pending[1](new Blob(['b']));await latest;pending[0](new Blob(['a']));await old;
    assert.equal(ctx.state.formalDefaultImages.shots[0].selected_candidate_key,'b');assert.equal(ctx.state.formalDefaultImageSelections.get('1').candidate_key,'b');assert.equal(ctx.state.formalImagePreviewUrls.has('default:a'),false);assert.equal(ctx.formalDefaultAdoptionReady(),true);
  });
  await check('C17-selection-preview-cannot-write-after-project-or-page-changes',async()=>{
    for(const route of ['/projects/b/materials','/projects/a/plan']) {
      const {ctx}=harness('materials');ctx.state.formalImageProjectVersion=4;ctx.state.formalDefaultImages={shots:[{shot_id:'shot-1',shot_no:'1',candidates:[{candidate_key:'a',preview_url:'/preview/a'}]}]};
      let resolve;ctx.readFormalImagePreview=()=>new Promise(done=>resolve=done);const pending=ctx.selectFormalDefaultCandidate(0,'a');ctx.window.location.pathname=route;ctx.state.formalDefaultImageSelections.clear();ctx.state.formalDefaultImages={shots:[]};resolve(new Blob(['a']));await pending;
      assert.equal(ctx.state.formalImagePreviewUrls.has('default:a'),false);assert.equal(ctx.state.formalDefaultImageSelections.size,0);assert.equal(ctx.state.formalDefaultImages.shots.length,0);
    }
  });
  await check('C18-query-change-keeps-offpage-choice-and-retires-inflight-or-ready-proposal',async()=>{
    for(const stage of ['inflight','ready'])for(const multi of [false,true]) {
      const {ctx,node}=harness('materials');ctx.state.formalImageProjectVersion=4;
      const candidate=key=>({candidate_key:key,caption:key,preview_url:`/preview/${key}`});
      const catalog=(key,query='')=>({selection_mode:multi?'category_library_multi':'category_library_browse',query,page:query?1:2,total_groups:30,
        shots:[{shot_id:'shot-1',shot_no:'1',state:'ready',selected_candidate_key:'default-one',candidates:[candidate('default-one')]},
          {shot_id:'shot-2',shot_no:'2',state:'ready',selected_candidate_key:'default-two',candidates:[candidate('default-two')]}],
        library_candidates:[candidate(key)]});
      for(const key of ['page-two-explicit','filtered-unselected','default-one','default-two'])ctx.state.formalImagePreviewUrls.set(`default:${key}`,`blob:${key}`);
      let releaseOld,proposalCount=0,confirmCalls=0,lastPayload;
      ctx.api=async(url,options)=>{
        if(url.includes('/default-candidates?'))return catalog(new URL(url,'http://localhost').searchParams.get('query')?'filtered-unselected':'page-two-explicit',new URL(url,'http://localhost').searchParams.get('query'));
        if(url.includes('/default-proposals?')){lastPayload=JSON.parse(options.body);proposalCount++;if(proposalCount===1&&stage==='inflight')return new Promise(resolve=>releaseOld=resolve);return {task_id:proposalCount===1?'old-proposal':'new-proposal',response:{proposal_digest:`digest-${proposalCount}`}};}
        if(url.includes('/confirm')){confirmCalls++;throw new Error('stale confirmation must never reach transport');}
        throw new Error(`unexpected API ${url}`);
      };
      await ctx.loadFormalDefaultImages({view:'library',page:2,shotNo:'2'});
      if(multi)ctx.toggleFormalLibraryCandidate('page-two-explicit',true);else ctx.useFormalLibraryCandidate('page-two-explicit');
      const old=ctx.proposeFormalDefaultImages();if(stage==='ready')await old;
      await ctx.loadFormalDefaultImages({view:'library',query:'新的筛选',page:1,shotNo:'2'});
      assert.equal(ctx.state.formalDefaultImageQuery,'新的筛选',`${stage}: search must run while proposal is pending or displayed`);
      if(releaseOld)releaseOld({task_id:'old-proposal',response:{proposal_digest:'old-digest'}});await old;
      assert.equal(ctx.state.formalDefaultImageProposal,null,`${stage}: old proposal must stay invalid after response`);
      const html=node('[data-default-image-panel]').innerHTML;
      assert.match(html,/当前页外 1/);assert.match(html,/page-two-explicit/);assert.doesNotMatch(html,/data-confirm-default-images/);
      await ctx.confirmFormalDefaultImages();assert.equal(confirmCalls,0);
      await ctx.proposeFormalDefaultImages();
      assert.equal(ctx.state.formalDefaultImageProposal.task_id,'new-proposal');
      assert.deepEqual(lastPayload.selections,[{shot_no:multi?'library:1':'2',candidate_key:'page-two-explicit'}]);
      assert.equal(confirmCalls,0);
    }
  });
  process.stdout.write(checks.join('\n')+'\n');if(checks.some(x=>x.startsWith('STEP_FAIL')))process.exitCode=1;
})();
module.exports={harness};
