const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../mvp_v2/ui/app.js'), 'utf8');
const noop = () => {};
const nodes = new Map();
const node = key => { if (!nodes.has(key)) nodes.set(key, {innerHTML:'',value:'',dataset:{},addEventListener:noop,classList:{toggle:noop},querySelector:node,querySelectorAll:()=>[]}); return nodes.get(key); };
const storage = {getItem:()=>null,setItem:noop};
const ctx = vm.createContext({console, URL, URLSearchParams, Headers, FormData, Map, Set, Date, JSON,
  localStorage:storage, sessionStorage:storage, document:{querySelector:node,querySelectorAll:()=>[],addEventListener:noop},
  window:{location:{pathname:'/projects/a/script',search:'',origin:'http://localhost'},addEventListener:noop},setTimeout:()=>1,clearTimeout:noop});
vm.runInContext(source.slice(0, source.indexOf('\nelements.formalOwnerConfirm.addEventListener')), ctx);
ctx.state = vm.runInContext('state', ctx); ctx.elements = vm.runInContext('elements', ctx);
const currentImage = ctx.elements.creativeCurrentImage;
// An empty container has no descendants; later renders patch these children in place.
currentImage.querySelector = selector => currentImage.innerHTML ? node(selector) : null;
const currentImageMarkup = () => currentImage.innerHTML ? currentImage.innerHTML
  + node('.creative-image-thumbnail').innerHTML + node('[data-current-image-info]').innerHTML : '';
const shot = {shot_id:'stable-shot-2',shot_no:'2',visual:'当前镜头展示观众入场'};
const candidate = {image_key:'recommended',caption:'旧模板的使用描述',actual_caption:'观众在室内签到',template_usage:'签到镜头参考'};
const imageShot = {...shot,selected_image_key:'recommended',candidates:[candidate],confirmed:false,
  current_image:null,reference_image:{inline_preview:'data:image/png;base64,REF',actual_caption:'',template_usage:'旧模板用途'},
  explicitly_changed:false};
ctx.state.formalImageWorkspace = {shots:[imageShot]};
ctx.state.formalImagePreviewUrls.set('recommended', 'blob:recommendation');
ctx.creativeCurrentShot = () => shot;
ctx.creativeState = () => ({imageChanges:new Set()});
ctx.renderCreativeCurrentImage();
let html = currentImageMarkup();
assert.ok(html.includes('data:image/png;base64,REF'), '未确认时主图必须是当前版本冻结参考');
assert.ok(!html.includes('blob:recommendation'), '默认候选不得冒充当前图');
assert.ok(html.includes('版本参考图'), '参考状态必须明示');
assert.ok(html.includes('图片内容尚未单独识别'), '模板文字不能冒充图片实况');
imageShot.current_image = {...candidate,image_key:'confirmed',actual_caption:'已确认的入口人群'};
ctx.state.formalImagePreviewUrls.set('confirmed','blob:confirmed');
imageShot.confirmed = true;
ctx.renderCreativeCurrentImage(); html=currentImageMarkup();
assert.ok(html.includes('blob:confirmed') && html.includes('当前确认图'));
assert.ok(!html.includes('blob:recommendation'));
imageShot.explicitly_changed = true;
ctx.renderCreativeCurrentImage(); html=currentImageMarkup();
assert.ok(html.includes('blob:recommendation') && html.includes('待保存配图版本'));
assert.ok(html.includes('观众在室内签到') && html.includes('当前镜头意图'));
imageShot.explicitly_changed = false;
ctx.renderCreativeCurrentImage(); html=currentImageMarkup();
assert.ok(html.includes('blob:confirmed') && !html.includes('待保存配图版本'));
process.stdout.write('PASS default reference / confirmed / pending selection / cancellation / separate content and intent\n');
