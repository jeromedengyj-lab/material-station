import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import {Readable} from 'node:stream';
import {finished} from 'node:stream/promises';
import {matchRejectText, REJECT_MATCH_JS} from './reject-patterns.mjs';
import {validateCandidates} from './candidate-validation.mjs';
import {CONTENT_TYPE_LABELS, bookMatchesType, bookEpisodes, bookTypeLabels, pickUniqueBook} from './book-matching.mjs';
import {resolveSharedRoot} from './shared-root.mjs';

let ROOT;
try{ROOT=resolveSharedRoot()}catch(e){console.error(`\n错误：${e.message}`);process.exit(3)}
const argv=process.argv.slice(2);
const argValue=n=>{const i=argv.indexOf(n);return i<0?'':argv[i+1]||''};
const requestedTaskFile=argValue('--task-file').trim();
let taskManifest={};
if(requestedTaskFile){
 try{taskManifest=JSON.parse(fs.readFileSync(path.resolve(requestedTaskFile),'utf8'))}
 catch(e){throw new Error(`无法读取三端别名任务文件：${requestedTaskFile}；${e.message}`)}
}
const manifestRows=Array.isArray(taskManifest.candidate_rows)?taskManifest.candidate_rows:[];
const browserRoleIndex=argv.indexOf('--browser-role');
const requestedBrowserRole=browserRoleIndex>=0?String(argv[browserRoleIndex+1]||'').trim().toLowerCase():'';
const BROWSER_ROLE=requestedBrowserRole||((argv.includes('--triple-platform'))?'alias':'download');
if(!['download','alias'].includes(BROWSER_ROLE))throw new Error('browser-role 必须为 download 或 alias');
// 下载保留既有登录目录；别名使用独立目录和端口，避免两个并行任务争用同一 Chrome profile。
const PROFILE_NAME=BROWSER_ROLE==='alias'?`任务台Chrome-${safe(os.hostname())}-别名`:`任务台Chrome-${safe(os.hostname())}`;
const APP_ROOT=String(process.env.MANJU_TOOL_ROOT||'D:\\漫剧剪辑工具').trim();
const TOOL=APP_ROOT;
const PROFILE=path.join(APP_ROOT,'runtime','browser_profiles',PROFILE_NAME);
const requestedOutputDir=argValue('--output-dir').trim();
const requestedCoverDir=argValue('--cover-output-dir').trim();
const OUTPUT=path.resolve(requestedOutputDir||path.join(ROOT,'选剧文件夹','原剧视频'));
const COVER_OUTPUT=path.resolve(requestedCoverDir||path.join(ROOT,'封面-原图'));
const WORKFLOW_STATE=path.join(ROOT,'选剧文件夹','工作流状态');
const ALIAS_LEDGER=path.join(WORKFLOW_STATE,'别名全局账本.jsonl');
const ALIAS_PERF_LOG=path.join(WORKFLOW_STATE,'别名性能日志.jsonl');
const REVIEW_POLL_SCHEDULE=[15_000,15_000,30_000,60_000];
const ALIAS_ACTION_MIN_DELAY=3_000,ALIAS_ACTION_JITTER=2_000;
// 该入口必须保留机构邀请参数；基础地址会被平台拒绝并提示使用机构链接。
const INVITE='LH1jwGOWSWOrFXIB4-gnuJN02QvPPRX0qax3sjOl7ug=';
const ENTRY_URL=`https://koc.fqopenplatform.com/page/member/task?invite_user_share_token=${encodeURIComponent(INVITE)}`;
const CONTENT_URL=`https://koc.fqopenplatform.com/page/member/content?invite_user_share_token=${encodeURIComponent(INVITE)}`;
const PORT=BROWSER_ROLE==='alias'?9223:9222;
const ROLE_LOCK_DIR=path.join(APP_ROOT,'runtime','platform_adapter');
const ROLE_LOCK=path.join(ROLE_LOCK_DIR,`${BROWSER_ROLE}.lock`);
const val=argValue;
let title=String(taskManifest.title||val('--title')).trim(), loginOnly=argv.includes('--login-only'),inspectOnly=argv.includes('--inspect-current'),infoOnly=argv.includes('--info-only');
const requestedContentType=(val('--content-type').trim().toLowerCase()||'manju');
if(!Object.prototype.hasOwnProperty.call(CONTENT_TYPE_LABELS,requestedContentType))throw new Error(`--content-type 只支持 ${Object.keys(CONTENT_TYPE_LABELS).join('/')}，收到：${requestedContentType}`);
const DOWNLOAD_TAB=16;
let ACTIVE_TAB=DOWNLOAD_TAB; // 实际内容 tab：由搜索结果的 content_tab 动态决定（网文/漫剧/短剧各不相同）
const batchId=String(taskManifest.batch_id||val('--batch-id')).trim();
const expectedEpisodesRaw=val('--expected-episodes').trim();
let expectedEpisodes=expectedEpisodesRaw?Number(expectedEpisodesRaw):0;
const expectedBookId=val('--expected-book-id').trim();
const directBookId=val('--book-id').trim();
if(directBookId&&!/^\d{16,20}$/.test(directBookId))die('--book-id 必须是完整数字BookID。',2);
const manifestAliases=manifestRows.map(row=>String(row?.alias||'').trim()).filter(Boolean);
const aliases=manifestAliases;
const aliasPrefix=String(taskManifest.alias_prefix||manifestAliases[0]?.slice(0,2)||'知夏').trim();
const aliasMode=String(taskManifest.alias_mode||'prefix').trim();
const manualMode=taskManifest.manual===true; // 手动别名：跳过固定字校验，按提供的原样申请
const _escapedPrefix=aliasPrefix.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
const aliasPattern=aliasMode==='suffix'
    ? new RegExp(`^[\\u4e00-\\u9fff]{2}${_escapedPrefix}$`)
    : new RegExp(`^${_escapedPrefix}[\\u4e00-\\u9fff]{2}$`);
const tripleMode=argv.includes('--triple-platform'),watchMode=argv.includes('--watch');
function safe(s){return String(s).replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').replace(/[. ]+$/g,'').slice(0,120)||'未命名'}
const normalizeTitle=s=>String(s||'').normalize('NFKC').replace(/[\s,，.。!！?？:：;；、《》「」『』（）()【】\[\]~～\-—_]/g,'').trim();
function die(s,n=1){console.error(`\n错误：${s}`);process.exit(n)}
const sleep=n=>new Promise(r=>setTimeout(r,n));
function processAlive(pid){if(!Number.isInteger(pid)||pid<1)return false;try{process.kill(pid,0);return true}catch{return false}}
async function acquireRoleLock(){
 await fsp.mkdir(ROLE_LOCK_DIR,{recursive:true});
 try{
  const old=JSON.parse(await fsp.readFile(ROLE_LOCK,'utf8'));
  if(old.device===os.hostname()&&processAlive(Number(old.pid)))die(`${BROWSER_ROLE==='alias'?'别名审核':'原剧下载'}已有任务正在运行，请等待当前任务结束。`,28);
 }catch(error){if(error?.code!=='ENOENT'&&!(error instanceof SyntaxError))throw error}
 try{await fsp.rm(ROLE_LOCK,{force:true})}catch{}
 let handle;
 try{handle=await fsp.open(ROLE_LOCK,'wx');await handle.writeFile(JSON.stringify({device:os.hostname(),pid:process.pid,role:BROWSER_ROLE,title,started_at:new Date().toISOString()}));await handle.close()}
 catch{die(`无法取得${BROWSER_ROLE==='alias'?'别名审核':'原剧下载'}执行锁，请等待当前任务结束。`,28)}
 const release=()=>{try{fs.rmSync(ROLE_LOCK,{force:true})}catch{}};
 process.once('exit',release);process.once('SIGINT',()=>process.exit(130));process.once('SIGTERM',()=>process.exit(143));
 return release;
}
async function appendJsonLine(file,payload){await fsp.mkdir(path.dirname(file),{recursive:true});await fsp.appendFile(file,`${JSON.stringify(payload)}\n`,'utf8')}
async function perf(stage,data={}){try{await appendJsonLine(ALIAS_PERF_LOG,{time:new Date().toISOString(),device:os.hostname(),batch_id:batchId,title,stage,...data})}catch{}}
async function loadAliasLedger(){const latest=new Map;try{for(const line of (await fsp.readFile(ALIAS_LEDGER,'utf8')).split(/\r?\n/)){if(!line.trim())continue;try{const row=JSON.parse(line);if(row?.alias)latest.set(row.alias,row)}catch{}}}catch{}return latest}
async function recordAliasLedger(ledger,alias,status,bookId,extra={}){const row={time:new Date().toISOString(),alias,status,book_id:String(bookId),title,batch_id:batchId,device:os.hostname(),...extra};await appendJsonLine(ALIAS_LEDGER,row);ledger.set(alias,row);return row}
if(!loginOnly&&!inspectOnly&&!title&&!directBookId)die('没有输入剧名。',2);
if(expectedEpisodesRaw&&(!Number.isInteger(expectedEpisodes)||expectedEpisodes<1))die('全集数量必须是正整数。',2);
if(expectedBookId&&!/^\d{16,20}$/.test(expectedBookId))die('expected-book-id 必须是完整数字BookID。',2);
await fsp.mkdir(PROFILE,{recursive:true}); await fsp.mkdir(OUTPUT,{recursive:true}); await fsp.mkdir(COVER_OUTPUT,{recursive:true});

const chrome=()=>[
 path.join(process.env.PROGRAMFILES||'','Google','Chrome','Application','chrome.exe'),
 path.join(process.env['PROGRAMFILES(X86)']||'','Google','Chrome','Application','chrome.exe'),
 path.join(process.env.LOCALAPPDATA||'','Google','Chrome','Application','chrome.exe'),
 path.join(process.env.PROGRAMFILES||'','Microsoft','Edge','Application','msedge.exe'),
 path.join(process.env['PROGRAMFILES(X86)']||'','Microsoft','Edge','Application','msedge.exe'),
 path.join(process.env.LOCALAPPDATA||'','Microsoft','Edge','Application','msedge.exe')].find(fs.existsSync);
async function jget(u,o){const r=await fetch(u,o);if(!r.ok)throw Error(`${r.status}`);return r.json()}
async function startChrome(){
 try{await jget(`http://127.0.0.1:${PORT}/json/version`);return}catch{}
 const exe=chrome(); if(!exe)die('找不到 Google Chrome 或 Microsoft Edge。',4);
 console.log(`启动本机浏览器；用途：${BROWSER_ROLE==='alias'?'别名申请':'原剧下载'}；端口：${PORT}；本机登录配置：${PROFILE}`);
 const p=spawn(exe,[`--remote-debugging-port=${PORT}`,`--user-data-dir=${PROFILE}`,'--new-window','--no-first-run','--no-default-browser-check',ENTRY_URL],{detached:true,stdio:'ignore'});p.unref();
 for(let i=0;i<40;i++){await sleep(500);try{await jget(`http://127.0.0.1:${PORT}/json/version`);return}catch{}}
 die('无法连接 Chrome 端口 9222；请关闭专用 Chrome 后重试。',5);
}
class CDP{
 constructor(u){this.u=u;this.i=0;this.p=new Map;this.h=new Map}
 async open(){this.w=new WebSocket(this.u);await new Promise((r,j)=>{this.w.onopen=r;this.w.onerror=j});this.w.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=this.p.get(m.id);if(!p)return;this.p.delete(m.id);m.error?p.j(Error(m.error.message)):p.r(m.result)}else for(const f of this.h.get(m.method)||[])f(m.params)}}
 send(method,params={}){const id=++this.i;return new Promise((r,j)=>{this.p.set(id,{r,j});this.w.send(JSON.stringify({id,method,params}))})}
 on(m,f){if(!this.h.has(m))this.h.set(m,[]);this.h.get(m).push(f)}
 off(m,f){const list=this.h.get(m)||[];this.h.set(m,list.filter(x=>x!==f))}
 close(){this.w?.close()}
}
async function client(){
 // Each role owns a dedicated Chrome profile and is protected by ROLE_LOCK, so
 // one reusable platform tab is sufficient.  Older versions opened a new tab
 // per drama and only disconnected CDP, leaving dozens of live pages behind.
 const pages=await jget(`http://127.0.0.1:${PORT}/json/list`);
 const platformPages=pages.filter(x=>x.type==='page'&&x.url.includes('koc.fqopenplatform.com'));
 let p=platformPages[0];
 for(const stale of platformPages.slice(1)){
  try{await fetch(`http://127.0.0.1:${PORT}/json/close/${encodeURIComponent(stale.id)}`)}catch{}
 }
 if(!p?.webSocketDebuggerUrl){try{p=await jget(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(ENTRY_URL)}`,{method:'PUT'})}catch{}}
 if(!p)die('找不到任务台标签页。',6);const c=new CDP(p.webSocketDebuggerUrl);await c.open();for(const x of ['Runtime.enable','Page.enable','Network.enable'])await c.send(x);try{await c.send('Network.setCacheDisabled',{cacheDisabled:true})}catch{}return c;
}
async function ev(c,x){const r=await c.send('Runtime.evaluate',{expression:x,awaitPromise:true,returnByValue:true,userGesture:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value}
async function verificationChallenge(c){
 try{return await ev(c,`(()=>{const visible=e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2},text=[...document.querySelectorAll('body *')].filter(visible).map(e=>(e.innerText||e.textContent||'').trim()).filter(Boolean).join('\\n');return /请完成下列验证后继续|拖动完成上方拼图|滑块验证|安全验证/.test(text)})()`)}catch{return false}
}
const hasSearch=c=>ev(c,`[...document.querySelectorAll('input')].some(e=>{let r=e.getBoundingClientRect(),t=e.placeholder||e.getAttribute('aria-label')||'';return r.width>40&&r.height>10&&!e.disabled&&/搜索|剧名|书名|关键词/.test(t)})`);
async function waitSearch(c){for(let i=0;i<180;i++){if(await hasSearch(c))return true;if(i===3)console.log('请在普通 Chrome 中登录，并进入可按剧名搜索的内容库页面，工具会自动继续。');await sleep(1000)}return false}
function collect(c){const a=[];c.on('Network.responseReceived',async p=>{if(!/\/api\/platform\/content\//.test(p.response.url))return;try{await sleep(100);const b=await c.send('Network.getResponseBody',{requestId:p.requestId}),s=b.base64Encoded?Buffer.from(b.body,'base64').toString():b.body;a.push({url:p.response.url,status:p.response.status,json:JSON.parse(s)})}catch{}});return a}
async function wait(a,f,ms=30000,startIndex=0){const t=Date.now();while(Date.now()-t<ms){for(let i=a.length-1;i>=startIndex;i--)if(f(a[i]))return a[i];await sleep(250)}return null}
const unwrap=x=>x?.data?.data??x?.data??x;
async function search(c,query=title){return ev(c,`(()=>{const V=e=>{let r=e.getBoundingClientRect();return r.width>40&&r.height>10&&!e.disabled},q=${JSON.stringify(String(query||'').trim())},i=[...document.querySelectorAll('input')].find(e=>V(e)&&/搜索|剧名|书名|关键词/.test(e.placeholder||e.getAttribute('aria-label')||''));if(!i)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i,q);i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));i.focus();let b=[...document.querySelectorAll('button,[role=button]')].find(e=>V(e)&&/搜索/.test((e.innerText||'').trim()));if(b)b.click();else i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,bubbles:true}));return true})()`)}
async function clickTitle(c){return ev(c,`(()=>{let norm=s=>String(s||'').normalize('NFKC').replace(/[\\s,，.。!！?？:：;；、《》「」『』（）()【】\\[\\]~～\\-—_]/g,'').trim(),q=norm(${JSON.stringify(title)}),V=e=>{let r=e.getBoundingClientRect();return r.width>2&&r.height>2},a=[...document.querySelectorAll('body *')].filter(e=>V(e)&&norm(e.innerText||e.textContent||'')===q).sort((x,y)=>x.children.length-y.children.length),e=a[0];if(!e)return false;e=(e.closest('a,button,[role=button],[class*=card],[class*=item]')||e);e.scrollIntoView({block:'center'});e.click();return true})()`)}
async function titleDiagnostics(c){return ev(c,`(()=>{let norm=s=>String(s||'').normalize('NFKC').replace(/[\\s,，.。!！?？:：;；、《》「」『』（）()【】\\[\\]~～\\-—_]/g,'').trim(),q=norm(${JSON.stringify(title)}),V=e=>{let r=e.getBoundingClientRect();return r.width>2&&r.height>2},m=[...document.querySelectorAll('body *')].filter(e=>V(e)&&norm(e.innerText||e.textContent||'')===q).slice(0,10);return m.map(e=>{let a=[],x=e;for(let i=0;x&&i<7;i++,x=x.parentElement)a.push({tag:x.tagName,cls:String(x.className||'').slice(0,300),role:x.getAttribute('role'),text:(x.innerText||'').trim().slice(0,500),html:x.outerHTML.slice(0,1200)});return a})})()`)}
async function visibleCardTypeEvidence(c,typeLabel='漫剧'){return ev(c,`(()=>{const norm=s=>String(s||'').normalize('NFKC').replace(/[\\s,，.。!！?？:：;；、《》「」『』（）()【】\\[\\]~～\\-—_]/g,'').trim(),q=norm(${JSON.stringify(title)}),episodes=${JSON.stringify(String(expectedEpisodes||''))},typeLabel=${JSON.stringify(typeLabel)},visible=e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2},exact=[...document.querySelectorAll('body *')].filter(e=>visible(e)&&norm(e.innerText||e.textContent||'')===q).sort((a,b)=>a.children.length-b.children.length);for(const node of exact){let e=node;for(let depth=0;e&&depth<9;depth++,e=e.parentElement){const text=(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();if(text.length>1600)continue;const typeOk=/(^|\\s)${typeLabel.replace(/[.*+?^${}()|[\\]\\]/g,'\\$&')}($|\\s)/.test(text)||text.includes(typeLabel);const episodeOk=!episodes||new RegExp(episodes+'\\s*集').test(text);if(typeOk&&episodeOk)return {ok:true,text:text.slice(0,800),depth}}}return {ok:false,exactTitleNodes:exact.length}})()`)}
async function download(u,file){const part=file+'.part';let n=0;try{n=(await fsp.stat(part)).size}catch{}const r=await fetch(u,{headers:n?{Range:`bytes=${n}-`}:{},redirect:'follow'});if(!(r.ok||r.status===206)||!r.body)throw Error(`HTTP ${r.status}`);const out=fs.createWriteStream(part,{flags:n&&r.status===206?'a':'w'});await finished(Readable.fromWeb(r.body).pipe(out));await fsp.rename(part,file)}
async function requestBatchExport(c,startEpisode=1,endEpisode=0){
 const activeTab=await ev(c,`(()=>{try{return Number(new URL(location.href).searchParams.get('tab_type')||0)}catch{return 0}})()`);
 if(activeTab!==ACTIVE_TAB){
   console.error(`BATCH_EXPORT_CONTROL_STAGE=content_type_mismatch expected_tab=${ACTIVE_TAB} actual_tab=${activeTab}`);
   return 0;
 }
 const dialogState=()=>ev(c,`(()=>{const visible=e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2},dialogs=[...document.querySelectorAll('[role=dialog],.arco-modal')].filter(visible),dialog=dialogs.find(e=>(e.innerText||'').includes('批量下载'));if(!dialog)return {open:false};const inputs=[...dialog.querySelectorAll('input')].filter(e=>['开始集数','结束集数'].includes(e.placeholder)),button=[...dialog.querySelectorAll('button,[role=button]')].find(e=>(e.innerText||e.textContent||'').trim()==='下载');return {open:true,inputCount:inputs.length,values:inputs.map(e=>e.value),ariaNow:inputs.map(e=>e.getAttribute('aria-valuenow')),max:inputs.map(e=>e.getAttribute('aria-valuemax')),downloadFound:!!button,downloadDisabled:button?!!button.disabled||button.getAttribute('aria-disabled')==='true':null,text:(dialog.innerText||'').replace(/\\s+/g,' ').slice(0,500)}})()`);
 let state=await dialogState();
 if(!state.open){const opened=await ev(c,`(()=>{const visible=e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2},b=[...document.querySelectorAll('button,[role=button]')].find(e=>visible(e)&&(e.innerText||e.textContent||'').trim()==='批量导出');if(!b)return false;b.click();return true})()`);if(!opened){console.error('BATCH_EXPORT_CONTROL_STAGE=open_button_missing');return 0}}
 for(let attempt=0;attempt<40;attempt++){state=await dialogState();if(state.open&&state.inputCount>=2)break;await sleep(150)}
 if(!state.open||state.inputCount<2){console.error(`BATCH_EXPORT_CONTROL_STAGE=dialog_inputs_missing state=${JSON.stringify(state)}`);return 0}
 const range=await ev(c,`(()=>{const visible=e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2},dialog=[...document.querySelectorAll('[role=dialog],.arco-modal')].filter(visible).find(e=>(e.innerText||'').includes('批量下载')),a=dialog?[...dialog.querySelectorAll('input')].filter(e=>['开始集数','结束集数'].includes(e.placeholder)):[];if(a.length<2)return null;const text=(dialog.innerText||'').replace(/\\s+/g,' '),limit=text.match(/仅支持输入\\s*1\\s*[～~\\-至]\\s*(\\d+)\\s*的整数/),attrMax=Number(a[1].getAttribute('aria-valuemax')||a[0].getAttribute('aria-valuemax')||a[1].max||a[0].max||0),max=attrMax||Number(limit?.[1]||0),start=${Number(startEpisode)},requestedEnd=${Number(endEpisode)},end=Math.min(requestedEnd||max,max,start+59),set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;if(!max||start<1||start>max||start>end)return null;for(const [e,v] of [[a[0],String(start)],[a[1],String(end)]]){e.focus();e.select();set.call(e,v);e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:String(v)}));e.dispatchEvent(new Event('change',{bubbles:true}));e.dispatchEvent(new KeyboardEvent('keyup',{key:'Tab',code:'Tab',bubbles:true}));e.blur()}return {max,start,end}})()`);
 if(!range?.max||range.start>range.end){console.error(`BATCH_EXPORT_CONTROL_STAGE=range_invalid range=${JSON.stringify(range)}`);return 0}
 let clicked=false;
 for(let attempt=0;attempt<40&&!clicked;attempt++){
   state=await dialogState();
   const valuesOk=String(state.values?.[0])===String(range.start)&&String(state.values?.[1])===String(range.end);
   if(valuesOk&&state.downloadFound&&!state.downloadDisabled){clicked=await ev(c,`(()=>{const visible=e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2},dialog=[...document.querySelectorAll('[role=dialog],.arco-modal')].filter(visible).find(e=>(e.innerText||'').includes('批量下载')),b=dialog&&[...dialog.querySelectorAll('button,[role=button]')].find(e=>(e.innerText||e.textContent||'').trim()==='下载'&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');if(!b)return false;b.click();return true})()`)}
   if(!clicked)await sleep(150)
 }
 if(!clicked)console.error(`BATCH_EXPORT_CONTROL_STAGE=download_not_clicked state=${JSON.stringify(state)} range=${JSON.stringify(range)}`);
 return clicked?range:0;
}
async function submitAlias(c,alias,bookId){
 if(await verificationChallenge(c))return {ok:false,blocked:true,reason:'检测到任务台滑块/安全验证，需要人工完成后从当前候选恢复'};
 // BookID is the canonical identity already verified by openBookForPlatform.
 // The platform may render punctuation in the title differently (for example
 // ： versus :), so rechecking raw title text here can reject the correct,
 // already-open modal and repeatedly click its launch button.
 const visibleDialog=`[...document.querySelectorAll('[role=dialog],.arco-modal')].find(e=>{let r=e.getBoundingClientRect(),t=e.innerText||'';return r.width>2&&r.height>2&&t.includes(${JSON.stringify(String(bookId))})&&!!e.querySelector('input[placeholder*="请填写别名"]')})`;
 const visibleAliasModal=`[...document.querySelectorAll('[role=dialog],.arco-modal')].find(e=>{let r=e.getBoundingClientRect(),t=e.innerText||'';return r.width>2&&r.height>2&&(!!e.querySelector('input[placeholder*="请填写别名"]')||t.includes('别名创建成功'))})`;
 let opened=await ev(c,`!!(${visibleDialog})`);
 for(let attempt=0;attempt<4&&!opened;attempt++){
   const closed=await ev(c,`(()=>{let d=${visibleAliasModal};if(!d)return false;let form=!!d.querySelector('input[placeholder*="请填写别名"]'),b=form?[...d.querySelectorAll('button')].find(e=>(e.innerText||e.textContent||'').trim()==='取消'):d.querySelector('svg[class*=close],[class*=close]');if(!b)return false;try{b.click()}catch{}return true})()`);
   if(!closed)break;
   for(let i=0;i<20;i++){if(!await ev(c,`!!(${visibleAliasModal})`))break;await sleep(100)}
 }
 for(let i=0;i<40&&!opened;i++){
   const clicked=await ev(c,`(()=>{let b=document.querySelector('button[data-codex-exact-alias="1"]')||[...document.querySelectorAll('button,[role=button]')].find(e=>{let r=e.getBoundingClientRect();return r.width>2&&r.height>2&&(e.innerText||e.textContent||'').trim()==='别名推广'});if(!b)return false;try{b.click()}catch{}return true})()`);
   if(clicked){for(let j=0;j<20&&!opened;j++){opened=await ev(c,`!!(${visibleDialog})`);if(!opened)await sleep(100)}}
   if(!opened)await sleep(400);
 }
 if(!opened)return {ok:false,reason:'等待20秒仍未打开当前剧目的“别名推广”弹窗'};
 if(await verificationChallenge(c))return {ok:false,blocked:true,reason:'检测到任务台滑块/安全验证，需要人工完成后从当前候选恢复'};
 await sleep(ALIAS_ACTION_MIN_DELAY+Math.floor(Math.random()*ALIAS_ACTION_JITTER));
 let filled=false;
 for(let attempt=0;attempt<80&&!filled;attempt++){
   if(await verificationChallenge(c))return {ok:false,blocked:true,reason:'检测到任务台滑块/安全验证，需要人工完成后从当前候选恢复'};
   const focused=await ev(c,`(()=>{let d=${visibleDialog};if(!d)return false;let i=[...d.querySelectorAll('input')].find(e=>(e.placeholder||'').includes('请填写别名'));if(!i)return false;i.focus();i.select();return document.activeElement===i})()`);
   if(!focused){await sleep(250);continue}
   await c.send('Input.insertText',{text:alias});
   await ev(c,`(()=>{let d=${visibleDialog},alias=${JSON.stringify('ALIAS')},i=d&&[...d.querySelectorAll('input')].find(e=>(e.placeholder||'').includes('请填写别名'));if(!i)return false;let set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;set.call(i,alias);i.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:alias}));i.dispatchEvent(new Event('change',{bubbles:true}));return i.value===alias})()`.replace('ALIAS',alias));
   await sleep(150);
   filled=await ev(c,`(()=>{let d=${visibleDialog},alias=${JSON.stringify('ALIAS')},i=d&&[...d.querySelectorAll('input')].find(e=>(e.placeholder||'').includes('请填写别名'));return i?.value===alias})()`.replace('ALIAS',alias));
 }
 if(!filled)return {ok:false,reason:'当前弹窗未能写入并确认别名输入值'};
 await ev(c,`(()=>{let d=${visibleDialog},i=d&&[...d.querySelectorAll('input')].find(e=>(e.placeholder||'').includes('请填写别名'));if(!i)return false;i.dispatchEvent(new Event('change',{bubbles:true}));i.blur();return true})()`);
 let selectOpened=false;
 for(let i=0;i<20&&!selectOpened;i++){
   selectOpened=await ev(c,`(()=>{let d=${visibleDialog};if(!d)return false;let i=[...d.querySelectorAll('input')].find(e=>(e.placeholder||'').includes('请选择计划发文的素材类型')),s=i?.closest('.arco-select')||i?.parentElement;if(!s)return false;try{s.click()}catch{}return true})()`);
   if(!selectOpened)await sleep(100);
 }
 if(!selectOpened)return {ok:false,reason:'当前弹窗找不到发文类型选择框'};
 let selected=false;
 for(let i=0;i<30&&!selected;i++){
   selected=await ev(c,`(()=>{let a=[...document.querySelectorAll('[role=option],.arco-select-option,body *')].filter(e=>{let r=e.getBoundingClientRect();return r.width>2&&r.height>2&&(e.innerText||e.textContent||'').trim()==='解说混剪'}).sort((x,y)=>x.children.length-y.children.length),e=a[0];if(!e)return false;try{e.click()}catch{}return true})()`);
   if(!selected)await sleep(100);
 }
 if(!selected)return {ok:false,reason:'当前可见下拉列表找不到“解说混剪”'};
 let clicked=false;
 for(let i=0;i<40&&!clicked;i++){
   const formState=await ev(c,`(()=>{let d=${visibleDialog};if(!d)return {dialog:false};let alias=${JSON.stringify('ALIAS')},input=[...d.querySelectorAll('input')].find(e=>(e.placeholder||'').includes('请填写别名')),submit=[...d.querySelectorAll('button')].find(e=>(e.innerText||e.textContent||'').trim()==='提交'),text=d.innerText||'';let reject=(${REJECT_MATCH_JS})(text);return {dialog:true,aliasOk:input?.value===alias,typeOk:text.includes('解说混剪'),submitEnabled:!!submit&&!submit.disabled,duplicate:!!reject,rejectReason:reject?reject.source:''}})()`.replace('ALIAS',alias));
   if(formState.dialog&&formState.duplicate){return {ok:false,reason:{duplicate:!/相似|热门|侵权/.test(formState.rejectReason),similarity:/相似|热门|侵权/.test(formState.rejectReason),message:`平台提示拒绝该候选：${formState.rejectReason}`,dialogText:formState.dialogText}}}
   if(formState.dialog&&!formState.aliasOk){
     const focused=await ev(c,`(()=>{let d=${visibleDialog},i=d&&[...d.querySelectorAll('input')].find(e=>(e.placeholder||'').includes('请填写别名'));if(!i)return false;i.focus();i.select();return document.activeElement===i})()`);
     if(focused)await c.send('Input.insertText',{text:alias});
     await ev(c,`(()=>{let d=${visibleDialog},alias=${JSON.stringify('ALIAS')},i=d&&[...d.querySelectorAll('input')].find(e=>(e.placeholder||'').includes('请填写别名'));if(!i)return false;let set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;set.call(i,alias);i.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:alias}));i.dispatchEvent(new Event('change',{bubbles:true}));return i.value===alias})()`.replace('ALIAS',alias));
     await sleep(150);
     continue;
   }
   if(formState.aliasOk&&formState.typeOk&&formState.submitEnabled){clicked=await ev(c,`(()=>{let d=${visibleDialog},b=d&&[...d.querySelectorAll('button')].find(e=>(e.innerText||e.textContent||'').trim()==='提交'&&!e.disabled);if(!b)return false;try{b.click()}catch{}return true})()`)}
   if(!clicked)await sleep(250);
 }
 if(!clicked){const diagnostics=await ev(c,`(()=>{let d=${visibleDialog},inputs=[...(d||document).querySelectorAll('input')].map(e=>({placeholder:e.placeholder,value:e.value,disabled:e.disabled})),buttons=[...(d||document).querySelectorAll('button')].map(e=>({text:(e.innerText||e.textContent||'').trim(),disabled:e.disabled}));return {message:'别名与发文类型校验后，等待10秒提交按钮仍未启用',inputs,buttons,dialogText:(d?.innerText||'').slice(0,2000)}})()`);return {ok:false,reason:diagnostics}}
 let state;
 for(let i=0;i<32;i++){
   if(await verificationChallenge(c))return {ok:false,blocked:true,reason:'检测到任务台滑块/安全验证，需要人工完成后从当前候选恢复'};
   try{state=await ev(c,`(()=>{let d=${visibleDialog},visible=e=>{let r=e.getBoundingClientRect();return r.width>2&&r.height>2},messages=[...document.querySelectorAll('[class*=message],[class*=notification],[role=alert],[role=dialog],.arco-modal')].filter(visible).map(e=>(e.innerText||e.textContent||'').trim()).filter(Boolean),targetText=d?.innerText||'',allText=[targetText,...messages].join('\n'),success=messages.some(x=>x.includes('别名创建成功')),reject=(${REJECT_MATCH_JS})(allText);return {messages:messages.slice(-20),dialogOpen:!!d,success,duplicate:!!reject,rejectReason:reject?reject.source:''}})()`)}catch{await sleep(250);continue}
   if(state.duplicate)return {ok:false,reason:state};
   if(state.success||state.messages.some(x=>/别名创建成功|已提交|审核中/.test(x))){try{await ev(c,`(()=>{let d=[...document.querySelectorAll('[role=dialog],.arco-modal')].find(e=>{let r=e.getBoundingClientRect();return r.width>2&&r.height>2&&(e.innerText||'').includes('别名创建成功')}),b=d?.querySelector('svg[class*=close],[class*=close]');if(!b)return false;b.click();return true})()`)}catch{}await sleep(150);return {ok:true,state}}
   await sleep(250);
 }
  // 点击提交后，平台记录页偶尔要十几秒才出现。这里不能把“尚未看见回执”
  // 当成提交失败，否则三端循环会在第一端提前结束，并可能重复提交同一别名。
  return {ok:true,pending_visibility:true,state:state||null};
}
const PLATFORMS=[{name:'红果短剧',tab:6},{name:'番茄小说',tab:2},{name:'红果漫剧',tab:16}];
const tripleApprovalValid=(state,alias,bookId)=>{
 const adopted=String(alias||state?.approved_alias||'').trim(),stateBook=String(state?.book_id||'').trim();
 if(!adopted||stateBook!==String(bookId||'').trim())return false;
 const names=new Set((state?.candidate_rows||[]).map(row=>String(row?.alias||'').trim()).filter(Boolean));
 for(const value of state?.candidates||[])if(String(value||'').trim())names.add(String(value).trim());
 if(!names.has(adopted))return false;
 const proof=state?.platforms?.[adopted];
 return !!proof&&PLATFORMS.every(platform=>proof?.[platform.name]?.state==='approved');
};
async function waitLocation(c,part,ms=15000){const t=Date.now();while(Date.now()-t<ms){if((await ev(c,'location.href')).includes(part))return true;await sleep(250)}return false}
async function clickPlatformRoute(c,route,tab){
 const ok=await ev(c,`(()=>{let part=${JSON.stringify(`/page/member/${route}?tab_type=${tab}`)},a=[...document.querySelectorAll('a[href]')].find(e=>(e.getAttribute('href')||'').includes(part));if(!a)return false;a.click();return true})()`);
 if(!ok)return false;return waitLocation(c,`/${route}?tab_type=${tab}`);
}
async function openBookForPlatform(c,platform,bookId,totalEpisodes=0){
 if(!await clickPlatformRoute(c,'content',platform.tab))return {ok:false,reason:`找不到${platform.name}内容库菜单`};
 if(!await waitSearch(c))return {ok:false,reason:`${platform.name}没有搜索框`};
 if(!await search(c,String(bookId)))return {ok:false,reason:`${platform.name}按BookID搜索失败`};
 for(let i=0;i<80;i++){
   const candidates=await ev(c,`(()=>{let norm=s=>String(s||'').normalize('NFKC').replace(/[\\s,，.。!！?？:：;；、《》「」『』（）()【】\\[\\]~～\\-—_]/g,'').trim(),q=norm(${JSON.stringify(title)}),ep=${Number(totalEpisodes)||0},buttons=[...document.querySelectorAll('button,[role=button]')].filter(b=>(b.innerText||b.textContent||'').trim()==='别名推广'),out=[];for(let n=0;n<buttons.length;n++){let b=buttons[n],x=b;for(let j=0;j<16&&x;j++,x=x.parentElement){let t=(x.innerText||'').trim();if(t.includes('漫剧')&&t.length<1200){let exact=[x,...x.querySelectorAll('*')].some(e=>norm(e.innerText||e.textContent||'')===q);out.push({index:n,title_match:exact,episode_match:!ep||t.includes(ep+'集'),text:t.slice(0,800)});break}}}return out.sort((a,b)=>Number(b.title_match)-Number(a.title_match)||Number(b.episode_match)-Number(a.episode_match))})()`);
   if(candidates.length){
     for(const candidate of candidates){
       const clicked=await ev(c,`(()=>{let b=[...document.querySelectorAll('button,[role=button]')].filter(e=>(e.innerText||e.textContent||'').trim()==='别名推广')[${Number(candidate.index)}];if(!b)return false;b.scrollIntoView({block:'center'});try{b.click()}catch{}return true})()`);
       if(!clicked)continue;
       let verified=false,modalText='';
       for(let j=0;j<30&&!verified;j++){
         const info=await ev(c,`(()=>{let d=[...document.querySelectorAll('[role=dialog],.arco-modal')].find(e=>{let r=e.getBoundingClientRect(),t=e.innerText||'';return r.width>2&&r.height>2&&t.includes('别名推广')&&t.includes('书籍信息')});return d?{text:(d.innerText||'').trim(),html:d.outerHTML.slice(0,5000)}:null})()`);
         if(info){modalText=info.text;verified=modalText.includes(String(bookId));break}
         await sleep(100);
       }
       if(verified){
         return {ok:true,platform_book_id:String(bookId),title_match:!!candidate.title_match,episode_match:!!candidate.episode_match,verified_modal_open:true};
       }
       await ev(c,`(()=>{let d=[...document.querySelectorAll('[role=dialog],.arco-modal')].find(e=>(e.innerText||'').includes('书籍信息')),close=d&&([...d.querySelectorAll('button')].find(e=>(e.innerText||e.textContent||'').trim()==='取消')||d.querySelector('svg[class*=close],[class*=close]'));if(!close)return false;try{close.click()}catch{}return true})()`);
       await sleep(200);
     }
     return {ok:false,reason:`${platform.name}同名漫剧结果的弹窗BookID均与目标不一致`};
   }
   await sleep(250);
 }
 return {ok:false,reason:`${platform.name}没有找到剧名一致且类型为漫剧的候选剧目`};
}
async function checkAliasStatus(c,platform,alias,bookId){
 if(!await clickPlatformRoute(c,'promotion-list',platform.tab))return {state:'error',reason:`找不到${platform.name}申词记录菜单`};
 await sleep(1000);
 const row=await ev(c,`(()=>{let alias=${JSON.stringify(alias)},id=${JSON.stringify(String(bookId))},els=[...document.querySelectorAll('body *')].filter(e=>(e.innerText||e.textContent||'').trim()===alias);for(const e of els){let r=e.closest('tr'),t=r?.innerText||'';if(r&&t.includes(id)&&t.includes('漫剧'))return t;let x=e;for(let i=0;i<8&&x;i++,x=x.parentElement){let s=x.innerText||'';if(s.includes(id)&&s.includes('漫剧')&&s.length<3000)return s}}return ''})()`);
 if(!row)return {state:'absent'};
 if(/审核不通过|不通过|已拒绝|已驳回|已失效/.test(row))return {state:'rejected',text:row};
 if((/生效中|审核通过|已通过/.test(row))&&/可用/.test(row))return {state:'approved',text:row};
 return {state:'pending',text:row};
}
async function runTripleWorkflow(c){
 if(!requestedTaskFile)die('三端审核必须通过候选任务文件启动，不再接受命令行拼接别名。',20);
 if(!batchId)die('三端审核模式必须提供批次ID。',27);
  // 任务文件会保留历史候选并追加新一批，不能再假设文件中永远只有3条。
  if(aliases.length<1)die('三端审核模式至少需要1个候选别名。',21);
  if(!/^[\u4e00-\u9fff]{2}$/.test(aliasPrefix))die('任务文件中的别名前缀必须恰好是2个中文汉字。',22);
  const v=validateCandidates(aliases,aliasPrefix,aliasMode,manualMode);
  if(!v.ok)die(v.message,22);
  const _affixLabel=aliasMode==='suffix'?`以“${aliasPrefix}”结尾`:`以“${aliasPrefix}”开头`;
 const explicitStateFile=requestedTaskFile?path.resolve(requestedTaskFile):'';
 const dir=explicitStateFile?path.dirname(path.dirname(explicitStateFile)):path.join(OUTPUT,safe(title));
 const infoDir=path.join(dir,'剧目信息'),metaFile=path.join(infoDir,'剧目信息.json'),stateFile=explicitStateFile||path.join(infoDir,'三端别名任务.json'),lockFile=`${stateFile}.lock`;await fsp.mkdir(infoDir,{recursive:true});
 try{
  const st=await fsp.stat(lockFile);let oldLock={};try{oldLock=JSON.parse(await fsp.readFile(lockFile,'utf8'))}catch{}
  const lockIsStale=Date.now()-st.mtimeMs>30*60*1000||(oldLock.device===os.hostname()&&!processAlive(Number(oldLock.pid)));
  if(lockIsStale)await fsp.rm(lockFile,{force:true});
 }catch{}
 let lock;try{lock=await fsp.open(lockFile,'wx');await lock.writeFile(JSON.stringify({device:os.hostname(),pid:process.pid,started_at:new Date().toISOString()}))}catch{die('另一台设备或另一个进程正在执行这部剧的三端别名任务。',26)}
 const release=()=>{try{fs.rmSync(lockFile,{force:true})}catch{}};process.once('exit',release);process.once('SIGINT',()=>process.exit(130));
 let meta;try{meta=JSON.parse(await fsp.readFile(metaFile,'utf8'))}catch{die(`缺少剧目信息：${metaFile}，请先下载或查询该剧。`,23)}
 const bookId=String(meta.book_id||'');if(!bookId)die('剧目信息中缺少BookID。',24);
 const totalEpisodes=Number(meta.total_episodes||meta.totalEpisodes||meta.episode_count||meta.episodeCount||meta.chapter_num||meta.chapter_count||0)||0;
 const ledger=await loadAliasLedger();
 let state={version:2,title,book_id:bookId,batch_id:batchId,candidates:aliases,candidate_rows:manifestRows,current_index:0,status:'running',history:[],platforms:{},updated_at:new Date().toISOString()};
 try{const old=JSON.parse(await fsp.readFile(stateFile,'utf8'));if(old.title===title&&String(old.book_id||bookId)===bookId){const oldNames=Array.isArray(old.candidate_rows)?old.candidate_rows.map(row=>String(row?.alias||'').trim()).filter(Boolean):(old.candidates||[]);const merged=[...oldNames];for(const a of aliases)if(!merged.includes(a))merged.push(a);state={...state,...old,version:2,book_id:bookId,batch_id:old.batch_id||batchId,candidates:merged,status:old.status==='completed'&&tripleApprovalValid(old,old.approved_alias,bookId)?'completed':'running'};if(old.status==='completed'&&state.status!=='completed'){state.current_index=Math.max(0,merged.indexOf(String(old.approved_alias||'')));state.review_poll_alias='';state.review_poll_count=0}}}catch{}
 const candidateResult=alias=>{
   if(state.approved_alias===alias)return 'approved';
   const last=[...(state.history||[])].reverse().find(row=>row.alias===alias);
   if(last?.result==='rejected')return 'rejected';
   if(last?.result==='global_duplicate')return 'global_duplicate';
   if(last?.result==='failed')return 'failed';
   const index=state.candidates.indexOf(alias);
   if(index===state.current_index){if(state.status==='waiting_manual_verification')return 'verification_required';if(state.status==='waiting_review')return 'waiting_review';if(state.status==='waiting_retry')return 'waiting_retry';return 'running'}
   return index<state.current_index?'failed':'queued';
 };
 const save=async()=>{state.version=2;state.updated_at=new Date().toISOString();state.candidate_rows=state.candidates.map((alias,index)=>({order:index+1,alias,status:candidateResult(alias),platforms:state.platforms?.[alias]||{}}));const temporary=`${stateFile}.tmp`;await fsp.writeFile(temporary,JSON.stringify(state,null,2),'utf8');await fsp.rename(temporary,stateFile)};
 if(state.status==='completed'&&tripleApprovalValid(state,state.approved_alias,bookId)){
   const alias=state.approved_alias;
   console.log(`三端任务已完成，直接恢复结果：${alias}`);
   await lock.close();release();return;
 }
 await perf('workflow_start',{book_id:bookId,candidate_count:aliases.length});
  const LOCATOR_RECOVERY_VERSION=3;
  if(state.status!=='completed'&&Number(state.locator_recovery_version||0)<LOCATOR_RECOVERY_VERSION){
   let recovery=null;
   for(let i=0;i<state.candidates.length;i++){
      const alias=state.candidates[i],aliasFailures=(state.history||[]).filter(h=>h.alias===alias&&h.result==='failed'),coverage=Object.keys(state.platforms?.[alias]||{}).filter(name=>PLATFORMS.some(p=>p.name===name)).length,recoverable=aliasFailures.length>0&&(i<state.current_index||coverage<PLATFORMS.length);
     if(!recoverable)continue;
     const statuses={};for(const p of PLATFORMS){statuses[p.name]=await checkAliasStatus(c,p,alias,bookId);if(statuses[p.name].state==='absent'){const prior=[...(state.history||[])].reverse().map(h=>h.alias===alias?h.platforms?.[p.name]:null).find(x=>x?.submitted_at)||state.platforms?.[alias]?.[p.name];if(prior?.submitted_at)statuses[p.name]=prior}}
      const approved=PLATFORMS.filter(p=>statuses[p.name].state==='approved').length,rejected=PLATFORMS.some(p=>statuses[p.name].state==='rejected'),legacyPartial=coverage<PLATFORMS.length,locatorFailure=aliasFailures.some(h=>/没有找到.*(?:精确剧目|候选剧目)|弹窗BookID.*不一致|等待10秒提交按钮仍未启用|未能写入并确认别名输入值|等待20秒仍未打开/.test(JSON.stringify(h.platforms||{})));
      if(!rejected&&(approved>0||locatorFailure||legacyPartial)&&(!recovery||approved>recovery.approved))recovery={i,alias,statuses,approved,locatorFailure,legacyPartial};
   }
   state.locator_recovery_version=LOCATOR_RECOVERY_VERSION;
   if(recovery){state.current_index=recovery.i;state.current_alias=recovery.alias;state.platforms[recovery.alias]=recovery.statuses;state.status='running';state.review_poll_alias='';state.review_poll_count=0;await perf('historical_partial_recovered',{book_id:bookId,alias:recovery.alias,approved_platforms:recovery.approved,locator_failure:!!recovery.locatorFailure});await save();console.log(recovery.locatorFailure&&recovery.approved===0?`恢复旧定位器误判候选：${recovery.alias}，按新规则重新核验并提交。`:`恢复历史部分成功候选：${recovery.alias}（已通过${recovery.approved}端），仅补齐缺失端。`)}else await save();
 }
 while(state.current_index<state.candidates.length){
   const alias=state.candidates[state.current_index];state.current_alias=alias;state.platforms[alias]??={};
   if(await verificationChallenge(c)){
     state.status='waiting_manual_verification';state.platforms[alias].verification={state:'verification_required',detected_at:new Date().toISOString()};await recordAliasLedger(ledger,alias,'verification_required',bookId,{candidate_index:state.current_index});await perf('verification_required',{book_id:bookId,alias,stage:'candidate_start'});await save();await lock.close();release();die('检测到任务台滑块/安全验证。已保留当前候选和进度，请人工完成验证后重新运行同一任务。',29);
   }
   if(state.review_poll_alias!==alias){state.review_poll_alias=alias;state.review_poll_count=0}
   const occupied=ledger.get(alias),reservedStates=new Set(['attempting','submitted','approved','pending_visibility','waiting_review','waiting_retry']);
   if(occupied&&String(occupied.book_id)!==bookId&&reservedStates.has(String(occupied.status||''))){state.platforms[alias].global={state:'duplicate',reason:`全局账本已由《${occupied.title}》占用`,occupied};state.history.push({alias,result:'global_duplicate',platforms:state.platforms[alias],time:new Date().toISOString()});state.current_index++;state.review_poll_alias='';state.review_poll_count=0;await perf('candidate_skipped_global_duplicate',{book_id:bookId,alias,occupied_book_id:String(occupied.book_id),occupied_title:occupied.title});await save();continue}
   await recordAliasLedger(ledger,alias,'attempting',bookId,{candidate_index:state.current_index});await perf('candidate_start',{book_id:bookId,alias,candidate_index:state.current_index});await save();let failed=false,transientFailure=null;
   for(const p of PLATFORMS){
     if(await verificationChallenge(c)){
       state.status='waiting_manual_verification';state.platforms[alias][p.name]={state:'verification_required',detected_at:new Date().toISOString()};await recordAliasLedger(ledger,alias,'verification_required',bookId,{candidate_index:state.current_index,platform:p.name});await perf('verification_required',{book_id:bookId,alias,platform:p.name,stage:'before_status_check'});await save();await lock.close();release();die('检测到任务台滑块/安全验证。已保留当前候选和进度，请人工完成验证后重新运行同一任务。',29);
     }
     const previous=state.platforms[alias][p.name],checkStarted=Date.now();let status=await checkAliasStatus(c,p,alias,bookId);await perf('status_check',{book_id:bookId,alias,platform:p.name,duration_ms:Date.now()-checkStarted,result:status.state});
     if(status.state==='absent'&&previous?.submitted_at){const visibilityAge=Date.now()-Date.parse(previous.submitted_at);if(visibilityAge<2*60*1000)status={state:'pending_visibility',submitted_at:previous.submitted_at};else{state.platforms[alias][p.name]={state:'visibility_timeout',submitted_at:previous.submitted_at,reason:'提交后暂未出现申词记录；保留当前候选，稍后重新核验且不重复提交'};await perf('visibility_timeout',{book_id:bookId,alias,platform:p.name,visibility_age_ms:visibilityAge});transientFailure={platform:p.name,reason:'提交记录暂不可见'};break}}
     if(status.state==='rejected'){state.platforms[alias][p.name]=status;failed=true;break}
     if(status.state==='absent'){
       const openStarted=Date.now(),opened=await openBookForPlatform(c,p,bookId,totalEpisodes);await perf('open_book',{book_id:bookId,alias,platform:p.name,duration_ms:Date.now()-openStarted,ok:opened.ok,reason:opened.reason,title_match:opened.title_match,episode_match:opened.episode_match});if(!opened.ok){state.platforms[alias][p.name]={state:'submit_failed',reason:opened.reason};transientFailure={platform:p.name,reason:opened.reason};break}
       const submitStarted=Date.now(),submitted=await submitAlias(c,alias,bookId);await perf('submit_alias',{book_id:bookId,alias,platform:p.name,duration_ms:Date.now()-submitStarted,ok:submitted.ok,blocked:!!submitted.blocked,reason:submitted.ok?undefined:submitted.reason});
       if(submitted.blocked){
         state.platforms[alias][p.name]={state:'verification_required',reason:submitted.reason,detected_at:new Date().toISOString()};
         state.status='waiting_manual_verification';state.current_index=state.current_index;await recordAliasLedger(ledger,alias,'verification_required',bookId,{candidate_index:state.current_index,platform:p.name});await perf('verification_required',{book_id:bookId,alias,platform:p.name});await save();await lock.close();release();die('检测到任务台滑块/安全验证。已保留当前候选和进度，请人工完成验证后重新运行同一任务。',29);
       }
        if(!submitted.ok){const explicitDuplicate=!!submitted?.reason?.duplicate;state.platforms[alias][p.name]={state:explicitDuplicate?'duplicate':'submit_failed',reason:submitted.reason||submitted.state};failed=true;break}
        state.platforms[alias][p.name]={state:submitted.pending_visibility?'pending_visibility':(submitted.existing?'existing':'submitted'),submitted_at:new Date().toISOString(),platform_book_id:String(opened.platform_book_id||bookId),fallback_title_match:!!opened.fallback_title_match};await save();
     }else state.platforms[alias][p.name]=status;
   }
   if(transientFailure){state.transient_retries=(Number(state.transient_retries)||0)+1;state.first_transient_at=state.first_transient_at||new Date().toISOString();const budgetExhausted=(Number(state.transient_retries)||0)>=8||(Date.now()-Date.parse(state.first_transient_at||''))>=30*60*1000;if(budgetExhausted){state.status='waiting_manual_verification';state.last_transient_failure={alias,...transientFailure,time:new Date().toISOString(),budget_exhausted:true};await recordAliasLedger(ledger,alias,'verification_required',bookId,{candidate_index:state.current_index,platform:transientFailure.platform,reason:'retry_budget_exhausted'});await perf('verification_required',{book_id:bookId,alias,platform:transientFailure.platform,reason:'retry_budget_exhausted',retries:state.transient_retries});await save();await lock.close();release();die(`候选“${alias}”连续${state.transient_retries}次重试仍未通过，已暂停并转人工；请登录平台确认申词记录/登录态后重新运行同一任务。`,29)}state.status='waiting_retry';state.last_transient_failure={alias,...transientFailure,time:new Date().toISOString()};await recordAliasLedger(ledger,alias,'waiting_retry',bookId,{candidate_index:state.current_index,platform:transientFailure.platform,reason:transientFailure.reason});await perf('candidate_retry_wait',{book_id:bookId,alias,candidate_index:state.current_index,...transientFailure});await save();await lock.close();release();die(`当前候选“${alias}”遇到平台技术故障，已保留原候选和审核位置，稍后重试。`,30)}
   if(failed){state.history.push({alias,result:'failed',platforms:state.platforms[alias],time:new Date().toISOString()});await recordAliasLedger(ledger,alias,'failed',bookId,{candidate_index:state.current_index});await perf('candidate_failed',{book_id:bookId,alias,candidate_index:state.current_index});state.current_index++;state.review_poll_alias='';state.review_poll_count=0;await save();continue}
   // 本轮前半段已经逐端读取过一次状态；直接复用，避免同一轮再次访问三个申词记录页。
   const checked={};for(const p of PLATFORMS){checked[p.name]=state.platforms[alias][p.name]||{state:'absent'}}state.platforms[alias]=checked;await perf('review_state_reused',{book_id:bookId,alias,states:Object.fromEntries(PLATFORMS.map(p=>[p.name,checked[p.name].state]))});await save();
   if(PLATFORMS.every(p=>checked[p.name].state==='approved')){state.status='completed';state.approved_alias=alias;state.completed_at=new Date().toISOString();state.batch_id=batchId;await recordAliasLedger(ledger,alias,'approved',bookId,{candidate_index:state.current_index});await perf('workflow_completed',{book_id:bookId,alias,candidate_index:state.current_index});await save();console.log(`三端全部审核通过：${alias}`);await lock.close();release();return}
   if(PLATFORMS.some(p=>checked[p.name].state==='rejected')){state.history.push({alias,result:'rejected',platforms:checked,time:new Date().toISOString()});await recordAliasLedger(ledger,alias,'rejected',bookId,{candidate_index:state.current_index});await perf('candidate_rejected',{book_id:bookId,alias,candidate_index:state.current_index});state.current_index++;state.review_poll_alias='';state.review_poll_count=0;await save();continue}
   await recordAliasLedger(ledger,alias,'submitted',bookId,{candidate_index:state.current_index});
   const pollIndex=Math.min(Number(state.review_poll_count)||0,REVIEW_POLL_SCHEDULE.length-1),pollDelay=REVIEW_POLL_SCHEDULE[pollIndex];state.review_poll_count=(Number(state.review_poll_count)||0)+1;
   state.status='waiting_review';state.next_check_at=new Date(Date.now()+pollDelay).toISOString();await perf('review_wait',{book_id:bookId,alias,poll_number:state.review_poll_count,delay_ms:pollDelay});await save();console.log(`${alias} 正在等待三端审核；${Math.round(pollDelay/1000)}秒后检查：${state.next_check_at}`);
   if(!watchMode){await lock.close();release();return}await sleep(pollDelay);state.status='running';
 }
  state.status='needs_more_candidates';await save();await lock.close();release();die('现有候选均未能三端通过，需要生成新的候选别名后继续。',25);
}

await acquireRoleLock();
await startChrome();const c=await client();
try{
 await c.send('Page.bringToFront');
 if(inspectOnly&&argv.includes('--inspect-body')){console.log((await ev(c,`document.body.innerText`)).slice(0,12000));c.close();process.exit(0)}
 if(inspectOnly&&val('--inspect-url')){await c.send('Page.navigate',{url:val('--inspect-url')});await sleep(1800)}
 if(inspectOnly&&argv.includes('--inspect-routes')){console.log(JSON.stringify(await ev(c,`(()=>{let names=['红果短剧','番茄小说','红果漫剧','申词记录','内容库'];return names.map(name=>({name,matches:[...document.querySelectorAll('body *')].filter(e=>(e.innerText||e.textContent||'').trim()===name).slice(0,20).map(e=>{let a=e.closest('a');return {tag:e.tagName,cls:String(e.className||''),href:a?.href||e.getAttribute('href'),parent:e.parentElement?.outerHTML.slice(0,1000)}})}))})()`),null,2));c.close();process.exit(0)}
 if(inspectOnly&&argv.includes('--inspect-alias-html')){let opened=false;for(let i=0;i<20&&!opened;i++){opened=await ev(c,`(()=>{let b=[...document.querySelectorAll('button,[role=button]')].find(e=>(e.innerText||e.textContent||'').trim()==='别名推广');if(!b)return false;b.click();return true})()`);if(!opened)await sleep(300)}await sleep(500);console.log(await ev(c,`(()=>{let d=[...document.querySelectorAll('[role=dialog],.arco-modal')].find(e=>(e.innerText||'').includes('别名推广'));return d?.outerHTML||''})()`));c.close();process.exit(0)}
 if(inspectOnly&&argv.includes('--view-alias-tasks')){await ev(c,`(()=>{let b=[...document.querySelectorAll('button,[role=button]')].find(e=>(e.innerText||e.textContent||'').trim()==='查看任务');if(b)b.click();return!!b})()`);await sleep(1500);console.log(await ev(c,`document.body.innerText`));c.close();process.exit(0)}
 if(inspectOnly){if(argv.includes('--open-batch')||argv.includes('--open-alias')){const label=argv.includes('--open-alias')?'别名推广':'批量导出';await ev(c,`(()=>{let q=${JSON.stringify('PLACEHOLDER')},b=[...document.querySelectorAll('button,[role=button]')].find(e=>(e.innerText||e.textContent||'').trim()===q);if(b)b.click();return!!b})()`.replace('PLACEHOLDER',label));await sleep(1000)}const ui=await ev(c,`(()=>({url:location.href,buttons:[...document.querySelectorAll('button,[role=button]')].filter(e=>{let r=e.getBoundingClientRect();return r.width>2&&r.height>2}).map(e=>({tag:e.tagName,text:(e.innerText||e.textContent||'').trim(),cls:String(e.className||'')})).filter(x=>x.text).slice(0,100),inputs:[...document.querySelectorAll('input,textarea')].filter(e=>{let r=e.getBoundingClientRect();return r.width>2&&r.height>2}).map(e=>({tag:e.tagName,type:e.type,placeholder:e.placeholder,min:e.min,max:e.max,cls:String(e.className||'')})),checks:[...document.querySelectorAll('input[type=checkbox],[role=checkbox]')].map(e=>({checked:e.checked??e.getAttribute('aria-checked'),text:(e.closest('label')?.innerText||e.parentElement?.innerText||'').trim().slice(0,200),cls:String(e.className||'')})).slice(0,100),dialogs:[...document.querySelectorAll('[role=dialog],.arco-modal')].map(e=>(e.innerText||'').trim().slice(0,3000))}))()`);console.log(JSON.stringify(ui,null,2));c.close();process.exit(0)}
 // 已打开的旧标签页可能仍是无邀请参数的基础地址，强制改用正确机构入口。
 await c.send('Page.navigate',{url:ENTRY_URL});await sleep(1200);
 // 机构入口只负责建立/恢复登录；真正的剧名搜索位于内容库路由。
 await c.send('Page.navigate',{url:CONTENT_URL});await sleep(1500);
 if(!await waitSearch(c))die('等待登录或搜索页面超时。',7);
 if(loginOnly){console.log('\n登录环境已就绪。以后可直接运行下载工具。');process.exit(0)}
 if(tripleMode){await runTripleWorkflow(c);c.close();process.exit(0)}
 const a=collect(c);let searchResponseStart=a.length;console.log(`搜索：${directBookId||title}`);if(!await search(c,directBookId||undefined))die('未找到搜索框。',8);
 let bh=await wait(a,x=>x.url.includes('/book/search/'),30000,searchResponseStart);
 if(!bh){
   console.log('本次搜索响应未捕获，刷新内容库后重试一次。');
   await c.send('Page.navigate',{url:CONTENT_URL});await sleep(1500);if(!await waitSearch(c))die('刷新后仍未找到搜索框。',8);
   searchResponseStart=a.length;if(!await search(c,directBookId||undefined))die('刷新后仍无法发起搜索。',8);
   bh=await wait(a,x=>x.url.includes('/book/search/'),30000,searchResponseStart);
 }
 if(!bh)die('两次均未捕获本次搜索接口，请确认当前页面是内容库。',9);
 const bd=unwrap(bh.json),books=bd?.book_list||bd?.list||[];
 let exact,exactByTitle=[];
 if(directBookId){
   exact=books.filter(x=>String(x.book_id)===directBookId);
   if(exact.length!==1)die(exact.length?`发现 ${exact.length} 个相同BookID结果，已停止以避免下错。`:`按BookID ${directBookId} 未找到唯一结果。候选：${books.slice(0,15).map(x=>x.book_name||x.name).filter(Boolean).join('、')||'无'}`,10);
 }else{
    // 先精确匹配（含标点，只去空白），匹配不到再去标点模糊匹配；随后按内容类型标签+集数精确过滤，避免同名不同标签/不同集数选错剧
    const matched=pickUniqueBook(books,title,{contentType:requestedContentType,expectedEpisodes:expectedEpisodes||0,normalize:normalizeTitle});
    exactByTitle=matched.byTitle;
    exact=matched.filtered;
    if(exact.length!==1){
      const sameName=matched.byTitle.map(x=>`${x.book_name||x.name}（${bookEpisodes(x)||'未知'}集${(bookTypeLabels(x)[0]||'')?('·'+bookTypeLabels(x)[0]):''}）`).join('、');
      if(expectedEpisodes&&matched.byTitle.length)die(`原剧名匹配，但没有唯一的 ${expectedEpisodes} 集${CONTENT_TYPE_LABELS[requestedContentType]||''}结果。同名候选：${sameName}`,10);
      die(exact.length?`发现 ${exact.length} 个同名同类型结果，已停止以避免下错。同名候选：${sameName}`:`无唯一精确匹配（要求类型：${CONTENT_TYPE_LABELS[requestedContentType]||'不限'}）。候选：${books.slice(0,15).map(x=>x.book_name||x.name).filter(Boolean).join('、')||'无'}`,10);
    }
 }
 const book=exact[0];
 if(directBookId){title=String(book.book_name||book.name||'').trim();if(!expectedEpisodes)expectedEpisodes=Number(book.chapter_num||book.chapter_count||book.total_chapter_num||0)||0;console.log(`按BookID定位：${title}，${expectedEpisodes||'未知'} 集`)}
 if(expectedBookId&&String(book.book_id)!==expectedBookId)die(`精确同名同集数结果的BookID为 ${book.book_id||'空'}，与要求的 ${expectedBookId} 不一致，已停止。`,24);
 const platformTypeCode=Number(book.content_tab??book.tab_type??NaN);
 const platformTypeLabels=bookTypeLabels(book);
 const apiSaysType=bookMatchesType(book,requestedContentType);
 const cardEvidence=apiSaysType?{ok:false,skipped:true}:await visibleCardTypeEvidence(c,CONTENT_TYPE_LABELS[requestedContentType]||'漫剧');
 const isType=apiSaysType||cardEvidence.ok;
 if(!isType){
   const observed=[Number.isFinite(platformTypeCode)?`tab_type=${platformTypeCode}`:'',...platformTypeLabels].filter(Boolean).join(' / ')||`接口未返回类型标签；同卡片可见标签核验失败（精确标题节点${cardEvidence.exactTitleNodes||0}个）`;
   die(`精确匹配结果不是“${CONTENT_TYPE_LABELS[requestedContentType]||requestedContentType}”（${observed}），已停止，禁止下载或写入任务表。`,22);
 }
 ACTIVE_TAB=Number.isFinite(platformTypeCode)&&platformTypeCode>0?platformTypeCode:DOWNLOAD_TAB;
 console.log(`精确匹配：${book.book_name}，${book.chapter_num||book.chapter_count||book.total_chapter_num||'未知'} 集${expectedEpisodes?'（已按全集数量核对）':''}，BookID：${book.book_id}，类型：${CONTENT_TYPE_LABELS[requestedContentType]||'不限'}（${apiSaysType?'接口字段':'同一结果卡片可见标签'}）`);
 // 直接使用平台自己的详情页路由，避免卡片内部文字节点随页面版本变化。
 const detail=new URL('https://koc.fqopenplatform.com/page/member/content/book-detail');
 // 批量接口的 task_type 必须与当前内容页 tab_type 保持一致（网文/漫剧/短剧各不相同，取搜索结果返回的 content_tab）。
 detail.searchParams.set('tab_type',String(ACTIVE_TAB));
 detail.searchParams.set('invite_user_share_token',INVITE);
 detail.searchParams.set('top_tab_genre','-1');detail.searchParams.set('book_id',String(book.book_id));
 detail.searchParams.set('genre',String(book.genre??book.genre_id??205));
 console.log(`内容栏目：${CONTENT_TYPE_LABELS[requestedContentType]||'当前类型'}（批量接口 task_type=${ACTIVE_TAB}）；交付方式由平台响应决定，不改写请求。`);
 const chapterResponseStart=a.length;await c.send('Page.navigate',{url:detail.href});
 if(aliases.length){
   if(aliases.some(x=>!aliasPattern.test(x)))die(`所有关键词必须以“${aliasPrefix}”开头且恰好四个汉字。`,18);
   if(new Set(aliases).size!==aliases.length)die('关键词存在重复。',19);
   await sleep(1200);const recordDir=path.join(OUTPUT,safe(title),'剧目信息');await fsp.mkdir(recordDir,{recursive:true});const recordFile=path.join(recordDir,'关键词申请记录.json');let records=[];try{records=JSON.parse(await fsp.readFile(recordFile,'utf8')).records||[]}catch{}
   for(const alias of aliases){
     records=records.filter(x=>x.alias!==alias);await fsp.writeFile(recordFile,JSON.stringify({title,book_id:String(book.book_id),updated_at:new Date().toISOString(),records},null,2),'utf8');
     const before=a.length,r=await submitAlias(c,alias);await sleep(500);
     const responses=a.slice(before).map(x=>({url:new URL(x.url).pathname,status:x.status,code:x.json?.code,message:x.json?.message||x.json?.msg}));
     const ok=r.ok;
     records.push({alias,post_type:'解说混剪',ok,responses});console.log(`${ok?'申请已提交':'申请失败'}：${alias} / 解说混剪`);
     if(!ok){await fsp.writeFile(path.join(TOOL,'关键词申请诊断.json'),JSON.stringify({alias,result:r,responses},null,2),'utf8');die(`关键词“${alias}”提交后未确认成功，已停止后续申请。`,20)}
     await fsp.writeFile(recordFile,JSON.stringify({title,book_id:String(book.book_id),updated_at:new Date().toISOString(),records},null,2),'utf8');
   }
   console.log(`关键词申请完成：${aliases.join('、')}`);c.close();process.exit(0);
 }
 let ch=await wait(a,x=>x.url.includes('/chapter/list/'),30000,chapterResponseStart);
 if(!ch){
   console.log('本次章节响应未捕获，刷新详情页后重试一次。');
   const chapterRetryStart=a.length;await c.send('Page.reload',{ignoreCache:true});
   ch=await wait(a,x=>x.url.includes('/chapter/list/'),30000,chapterRetryStart);
 }
 if(!ch){const diag=await titleDiagnostics(c);await fsp.writeFile(path.join(TOOL,'页面结构诊断.json'),JSON.stringify(diag,null,2),'utf8');die('两次均未捕获本次详情页章节列表；已保存页面结构诊断。',12)}
 const cd=unwrap(ch.json),list=cd?.chapter_list||cd?.list||[];if(!list.length)die('章节列表为空。',13);
 const chapterResponseBookId=(()=>{try{return new URL(ch.url).searchParams.get('book_id')||String(cd?.book_id||'')}catch{return String(cd?.book_id||'')}})();
 if(chapterResponseBookId&&chapterResponseBookId!==String(book.book_id))die(`章节列表BookID与已选剧目不一致：预期 ${book.book_id}，实际 ${chapterResponseBookId}。已停止，禁止串用上一部剧数据。`,26);
 const declaredDownloadable=Number(cd?.download_chapter_index||0);
 let downloadable=list.filter(x=>x.pay_info===false||x.is_paid===false);
 if(!downloadable.length&&declaredDownloadable>0)downloadable=list.slice(0,declaredDownloadable);
 if(!downloadable.length)downloadable=list.filter(x=>x.video_download_url||x.video_url||x.download_url);
 if(!downloadable.length)die('平台没有开放任何可下载章节。',17);
 console.log(`平台章节共 ${list.length} 集，当前开放下载 ${downloadable.length} 集；使用平台批量下载接口一次提交。`);
 let generated=infoOnly?[]:downloadable.filter(x=>x.video_download_url||x.video_url||x.download_url);
 if(!infoOnly&&generated.length!==downloadable.length){
   const before=a.length,range=await requestBatchExport(c,1,downloadable.length);
   if(!range)die('无法在批量下载弹窗填写范围或提交任务。',15);
   console.log(`已提交平台批量下载：可下载列表第 ${range.start}–${range.end} 项。`);
   const batch=await wait(a,x=>x.url.includes('/batch_download/create/'),60000,before);
   if(!batch)die('未捕获批量下载接口响应。',16);
   const data=unwrap(batch.json),items=data?.download_info||data?.list||[];
   console.log(`平台批量下载响应：HTTP ${batch.status} / code=${String(batch.json?.code??'缺失')} / message=${batch.json?.message||batch.json?.msg||'无'}`);
   if(!items.length)die(`批量下载接口未返回文件地址：${batch.json?.message||batch.json?.msg||'无详细原因'}`,17);
   generated=items;
 }
 const generatedById=new Map(generated.map(x=>[String(x.item_id||''),x.url||x.video_url||x.download_url]));
 const dir=path.join(OUTPUT,safe(title));
 await fsp.mkdir(dir,{recursive:true});
 const coverUrl=book.thumb_url||book.cover_url||book.book_cover||book.poster_url;
 const coverFile=path.join(COVER_OUTPUT,`${safe(title)}.jpg`);
 if(coverUrl&&!fs.existsSync(coverFile)&&!infoOnly){console.log(`下载封面：${coverFile}`);try{await download(String(coverUrl).replace(/^http:/,'https:'),coverFile)}catch(e){console.error(`封面下载失败：${e.message}`)}}
 const info={title,book_id:String(book.book_id),content_type:requestedContentType,content_type_label:CONTENT_TYPE_LABELS[requestedContentType]||'',description:book.book_abstract||book.subabstract||'',captured_at:new Date().toISOString(),cover_available:!!coverUrl,cover_file:fs.existsSync(coverFile)?coverFile:null,chapters:[]};
 for(const x of list){const ordinal=info.chapters.length+1,name=String(x.chapter_name||`第${ordinal}集`),u=x.video_download_url||x.video_url||generatedById.get(String(x.item_id||'')),row={index:ordinal,download_order:ordinal,platform_index:x.index??x.chapter_index??null,item_id:x.item_id,chapter_name:name,url_available:!!u};info.chapters.push(row);if(infoOnly)continue;if(!u){console.log(`跳过下载列表第 ${ordinal} 集：平台未开放下载地址`);continue}let ext='.mp4';try{ext=path.extname(new URL(u).pathname)||'.mp4'}catch{}const file=path.join(dir,`${String(ordinal).padStart(3,'0')}_${safe(name)}${ext}`);if(fs.existsSync(file)&&(await fsp.stat(file)).size){console.log(`已存在：${path.basename(file)}`);continue}console.log(`下载：${path.basename(file)}（逻辑第${ordinal}集，平台标签“${name}”）`);try{await download(u,file)}catch(e){row.error=e.message;console.error(`失败：${e.message}`)}}
 const infoDir=path.join(dir,'剧目信息');await fsp.mkdir(infoDir,{recursive:true});const infoFile=path.join(infoDir,'剧目信息.json');await fsp.writeFile(infoFile,JSON.stringify(info,null,2),'utf8');
 console.log(`\n保存位置：${dir}`);const n=info.chapters.filter(x=>x.error).length;if(n)die(`${n} 集失败；再次运行可续传。`,14);
 console.log(`原剧已保存到本地任务中心指定目录：${dir}`);
}finally{c.close()}
