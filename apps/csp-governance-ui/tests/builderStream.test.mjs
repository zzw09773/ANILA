import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/dom.mjs'
import { createApp, nextTick } from 'vue'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { resolve, dirname } from 'node:path'
import { setTestStream, denyCancel } from './helpers/quickBuilderMock.mjs'
import { setTestResult } from './helpers/quickBuilderMock.mjs'
import { calls, reset, setFailure, setPending, setManageTemplates, changeActor, mutationRequests, holdDetail, releaseDetail, setRetrieval, setReadiness, failReadiness, holdReadiness, releaseReadiness, setTestSteps, setGraph, holdTest, releaseTest, holdRun, releaseRun, denyRun, setListedRuns, setRunResult, readRequests } from './helpers/quickBuilderMock.mjs'
import { connect, removeNode, maskTrace, modules } from '../src/quickBuilder/graph.js'
import { RAG_DEFAULTS, RAG_MODES } from '../src/quickBuilder/ragSettings.js'
import { DOCUMENT_DEFAULTS, DOCUMENT_PRESETS, availableDocumentPresets } from '../src/quickBuilder/documentSettings.js'
const ROOT=resolve(dirname(fileURLToPath(import.meta.url)),'..')
let View, mounted
const settle=async()=>{for(let i=0;i<15;i++){await Promise.resolve();await nextTick()}}
const button=text=>document.querySelector(`[data-module-kind="${modules.find(m=>m.name===text)?.kind || '__missing'}"]`) || [...document.querySelectorAll('button')].find(n=>n.textContent.trim()===text || n.firstChild?.textContent.trim()===text)
const field=text=>[...document.querySelectorAll('label')].find(n=>n.firstChild?.textContent.includes(text))?.querySelector('input,select,textarea')
async function change(node,value){assert.ok(node,'欄位存在');node.value=String(value);node.dispatchEvent(new Event(node.tagName==='SELECT'?'change':'input',{bubbles:true}));await settle()}
async function click(text){assert.ok(button(text),text);button(text).click();await settle()}
async function open(){await change(field('助手'), '11'); assert.ok(calls.find(c=>c[0]==='get'), JSON.stringify([...document.querySelectorAll('select')].map(s=>({value:s.value,html:s.innerHTML}))))}
test.before(async()=>{const require=createRequire(import.meta.url);const result=await build({configFile:false,envDir:false,root:ROOT,plugins:[{name:'builder-test-api',enforce:'pre',resolveId(id){if(id==='../api/quickBuilder')return{id:'builder-test-api',external:true}}},vue()],logLevel:'silent',build:{write:false,minify:false,lib:{entry:resolve(ROOT,'src/views/QuickBuilderView.vue'),formats:['es']},rollupOptions:{external:['vue','vue-router','builder-test-api'],output:{inlineDynamicImports:true,paths:{vue:pathToFileURL(require.resolve('vue')).href,'vue-router':pathToFileURL(require.resolve('vue-router')).href,'builder-test-api':new URL('./helpers/quickBuilderMock.mjs',import.meta.url).href}}}}});const chunk=(Array.isArray(result)?result[0]:result).output.find(o=>o.type==='chunk'&&o.isEntry);View=(await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default})
test.beforeEach(async()=>{window.sessionStorage.clear();reset();window.confirm=()=>true;const el=document.createElement('div');document.body.append(el);mounted=createApp(View);mounted.mount(el);await settle()})
test.afterEach(()=>{mounted.unmount();document.body.replaceChildren()})
const liveGraph={nodes:[{id:'in',kind:'input',label:'輸入',position:{x:0,y:0},config:{}},{id:'model',kind:'model',label:'產生回答',position:{x:0,y:240},config:{}},{id:'out',kind:'output',label:'輸出',position:{x:0,y:480},config:{}}],edges:[{source:'in',target:'model'},{source:'model',target:'out'}]};
let source,send,end,streamCancelled,request,sequence;
async function begin({uncancellable=false}={}){
 setGraph(liveGraph);await open();await click('測試對話');await change(field('測試問題'),'STREAM_QUESTION_CANARY');
 streamCancelled=false;sequence=0;setTestStream(body=>{
  request=body;
  if(uncancellable){
   let deliver;
   source={enqueue:value=>deliver({done:false,value}),close:()=>deliver({done:true})};
   return {getReader:()=>({read:()=>new Promise(resolve=>deliver=resolve),cancel:async()=>{streamCancelled=true;},releaseLock(){}})};
  }
  return new ReadableStream({start(controller){source=controller;},cancel(){streamCancelled=true;}});
 });
 await click('傳送測試');
 send=async(name,payload,patch={})=>{const seq=++sequence;source.enqueue(new TextEncoder().encode(`event: quick.${name}\nid: 7:${seq}\ndata: ${JSON.stringify({schema_version:1,run_id:7,version_id:22,revision:2,request_id:request.request_id,seq,payload,...patch})}\n\n`));await settle();};
 end=async()=>{source.close();await settle();};
 await send('run_started',{status:'running'});
 setRunResult(7,{id:7,version_id:22,revision:2,request_id:request.request_id,status:'running',output:'',steps:[]});
}
const result=status=>({id:7,version_id:22,revision:2,request_id:request.request_id,status,output:'權威完成答案',steps:[{node_id:'model',kind:'model',status:'completed',elapsed_ms:31,input:{api_key:'INPUT_CANARY'},output:'完成'}]});
test('真SSE形狀逐步更新、遮罩delta、server耗時與finished權威結果，只派一次',async()=>{
 await begin();await send('step_started',{step:{node_id:'model',kind:'model',status:'running',elapsed_ms:0}});
 assert.match(document.querySelector('.qb-run-step').textContent,/執行中.*完成後才會提供/s);assert.equal(document.querySelector('.qb-run-step details'),null);
 await send('output_delta',{node_id:'model',text:'先收到部分答案 api_key=DELTA_CANARY'});assert.match(document.querySelector('.qb-answer').textContent,/先收到部分答案/);assert.doesNotMatch(document.body.textContent,/DELTA_CANARY/);
 await send('step_finished',{step:result('completed').steps[0]});assert.match(document.querySelector('.qb-run-step').textContent,/31 毫秒/);assert.doesNotMatch(document.body.textContent,/INPUT_CANARY/);
 await send('run_finished',{result:result('completed')});assert.equal(document.querySelector('.qb-test-tracking').dataset.runStatus,'completed');assert.match(document.querySelector('.qb-answer').textContent,/權威完成答案/);assert.equal(calls.filter(c=>c[0]==='test').length,1);
 const pointer=window.sessionStorage.getItem('anila:quick-builder:test:101:11');assert.doesNotMatch(pointer,/CANARY|答案|31/);assert.equal(mutationRequests.find(r=>r.kind==='test').options.headers['X-ANILA-Actor-Id'],'101');
});
test('停止等待只detach；繼續查詢同run與UUID，沒有cancel POST',async()=>{
 await begin();await send('step_started',{step:{node_id:'model',kind:'model',status:'running',elapsed_ms:0}});await send('output_delta',{node_id:'model',text:'暫存部分'});await click('停止等待');
 assert.equal(streamCancelled,true);assert.equal(calls.some(c=>c[0]==='cancelTestRun'),false);assert.equal(document.querySelector('.qb-test-tracking').dataset.runId,'7');assert.match(document.querySelector('.qb-answer').textContent,/暫存部分/);
 setRunResult(7,result('completed'));await click('查詢這次試跑');assert.match(document.querySelector('.qb-answer').textContent,/權威完成答案/);assert.equal(calls.filter(c=>c[0]==='test').length,1);assert.equal(readRequests.at(-1).headers['X-ANILA-Actor-Id'],'101');
});
test('獨立取消需確認並等待authoritative cancelled，不當停止等待',async()=>{
 await begin();window.confirm=()=>false;await click('取消伺服器執行');assert.equal(calls.filter(c=>c[0]==='cancelTestRun').length,0);window.confirm=()=>true;await click('取消伺服器執行');
 assert.equal(document.querySelector('.qb-test-tracking').dataset.runStatus,'cancelled');assert.match(document.body.textContent,/執行已停止.*外部服務可能/s);assert.deepEqual(calls.find(c=>c[0]==='cancelTestRun').slice(1),[7,{version_id:22,request_id:request.request_id}]);
});
test('EOF/seq gap僅detach與GET同run，不顯示server失敗或重派',async()=>{
 for(const mode of ['eof','gap']){
  if(mode==='gap'){mounted.unmount();document.body.replaceChildren();reset();window.sessionStorage.clear();const el=document.createElement('div');document.body.append(el);mounted=createApp(View);mounted.mount(el);await settle();}
  await begin();if(mode==='eof')await end();else await send('step_started',{step:{node_id:'model',kind:'model',status:'running'}},{seq:9});
  await settle();assert.equal(document.querySelector('.qb-test-tracking').dataset.runStatus,'running');assert.equal(calls.filter(c=>c[0]==='test').length,1);assert.ok(calls.some(c=>c[0]==='getRun'&&c[1]===7));assert.doesNotMatch(document.body.textContent,/執行狀態：失敗/);
 }
});
test('duplicate snapshot不重複步驟；compact finished從同run GET取得權威內容',async()=>{
 await begin();setRunResult(7,result('completed'));await send('run_finished',{status:'completed',result_available:true});await settle();assert.match(document.querySelector('.qb-answer').textContent,/權威完成答案/);assert.equal(calls.filter(c=>c[0]==='test').length,1);assert.equal(document.querySelectorAll('.qb-run-step').length,1);
});
test('身份epoch通知立即清進度並detach，另一帳號晚frame不顯示且不保存正文',async()=>{
 await begin();await send('step_started',{step:{node_id:'model',kind:'model',status:'running',elapsed_ms:0}});await send('output_delta',{node_id:'model',text:'前帳號答案'});
 window.dispatchEvent(new CustomEvent('anila:identity',{detail:{userId:null}}));await settle();assert.equal(streamCancelled,true);assert.equal(document.querySelector('.qb-answer'),null);assert.equal(document.querySelector('.qb-test-tracking'),null);assert.match(document.body.textContent,/IDENTITY_CHANGED/);assert.doesNotMatch(JSON.stringify(window.sessionStorage),/前帳號答案|STREAM_QUESTION_CANARY/);
 assert.equal(window.sessionStorage.getItem('anila:quick-builder:test:202:11'),null);
});
test('server interrupted是未確認終局，不假取消且可重載GET同UUID',async()=>{
 await begin();const raw={...result('interrupted'),output:'',error:{type:'RESULT_UNCONFIRMED',message:'SECRET_CANARY'}};setRunResult(7,raw);await send('run_finished',{result:raw});assert.equal(document.querySelector('.qb-test-tracking').dataset.runStatus,'interrupted');assert.match(document.body.textContent,/執行已中斷；結果尚未確認/);assert.doesNotMatch(document.body.textContent,/SECRET_CANARY/);assert.equal(button('傳送測試').disabled,false);
});
test('SSE完成後重載只GET同run/UUID，不存正文也不再派POST',async()=>{
 await begin();setRunResult(7,result('completed'));await send('run_finished',{result:result('completed')});const uuid=request.request_id;
 mounted.unmount();document.body.replaceChildren();const el=document.createElement('div');document.body.append(el);mounted=createApp(View);mounted.mount(el);await settle();await open();await click('測試對話');
 assert.equal(document.querySelector('.qb-test-tracking').dataset.runId,'7');assert.equal(document.querySelector('.qb-test-tracking').dataset.requestId,uuid);assert.match(document.querySelector('.qb-answer').textContent,/權威完成答案/);assert.equal(calls.filter(c=>c[0]==='test').length,1);assert.doesNotMatch(JSON.stringify(window.sessionStorage),/CANARY|權威完成答案/);
});
test('duplicate request snapshot只讀實際running checkpoint，不再追加或假完成',async()=>{
 setGraph(liveGraph);await open();await click('測試對話');await change(field('測試問題'),'重複請求');
 setTestStream(body=>{request=body;const raw={...result('running'),steps:[{node_id:'model',kind:'model',status:'running',elapsed_ms:0}],output:''};setRunResult(7,raw);const envelope={schema_version:1,run_id:7,version_id:22,revision:2,request_id:body.request_id,seq:1,payload:{result:raw}};return new ReadableStream({start(controller){controller.enqueue(new TextEncoder().encode(`event: quick.run_snapshot\nid: 7:1\ndata: ${JSON.stringify(envelope)}\n\n`));controller.close();}});});
 await click('傳送測試');assert.equal(document.querySelector('.qb-test-tracking').dataset.runStatus,'running');assert.equal(document.querySelectorAll('.qb-run-step').length,1);assert.equal(calls.filter(c=>c[0]==='test').length,1);assert.match(document.body.textContent,/回答尚未完成/);assert.equal(button('傳送測試').disabled,true);
});
for(const status of [401,403,404]){
 test(`GET ${status} 立即撤銷observer，緩衝SSE晚delta不能恢復題目與結果`,async()=>{
  await begin({uncancellable:true});
  await send('step_started',{step:{node_id:'model',kind:'model',status:'running',elapsed_ms:0}});
  await send('output_delta',{node_id:'model',text:'撤權前內容'});
  denyRun(status);
  await click('查詢這次試跑');
  assert.equal(streamCancelled,true);
  assert.equal(document.querySelector('.qb-answer'),null);
  assert.equal(field('測試問題').value,'');
  assert.match(document.body.textContent,/TEST_RESULT_UNAVAILABLE/);
  assert.equal(document.querySelector('.qb-test-tracking').dataset.runId,'7');
  await send('output_delta',{node_id:'model',text:'REVOKED_BUFFER_CANARY'});
  assert.equal(document.querySelector('.qb-answer'),null);
  assert.doesNotMatch(document.body.textContent,/撤權前內容|REVOKED_BUFFER_CANARY|STREAM_QUESTION_CANARY/);
  assert.equal(field('助手名稱').closest('fieldset').disabled,false);
  assert.equal(calls.some(call=>call[0]==='cancelTestRun'),false);
  assert.equal(calls.filter(call=>call[0]==='test').length,1);
  assert.doesNotMatch(JSON.stringify(window.sessionStorage),/CANARY|撤權前內容/);
 });
}
for(const status of [401,403,404]){
 test(`取消 ${status} 撤銷observer且同epoch晚GET與SSE皆不可復活`,async()=>{
  await begin({uncancellable:true});
  await send('step_started',{step:{node_id:'model',kind:'model',status:'running',elapsed_ms:0}});
  await send('output_delta',{node_id:'model',text:'取消拒權前內容'});
  setRunResult(7,{...result('completed'),output:'LATE_GET_CANARY'});
  holdRun();
  await click('查詢這次試跑');
  denyCancel(status);
  await click('取消伺服器執行');
  assert.equal(streamCancelled,true);
  assert.equal(document.querySelector('.qb-answer'),null);
  assert.equal(field('測試問題').value,'');
  releaseRun();await settle();
  await send('output_delta',{node_id:'model',text:'LATE_CANCEL_BUFFER_CANARY'});
  assert.equal(document.querySelector('.qb-answer'),null);
  assert.doesNotMatch(document.body.textContent,/LATE_GET_CANARY|LATE_CANCEL_BUFFER_CANARY|執行已停止/);
  assert.match(document.body.textContent,/TEST_RESULT_UNAVAILABLE/);
  assert.equal(document.querySelector('.qb-test-tracking').dataset.runStatus,'unknown');
  assert.equal(field('助手名稱').closest('fieldset').disabled,false);
 });
}
test('本人清單拒權也清除先前已取得題目與結果，保留指標供明確再查',async()=>{
 await begin();setRunResult(7,result('completed'));await send('run_finished',{result:result('completed')});
 assert.ok(document.querySelector('.qb-answer'));denyRun(403);await click('查詢我的試跑');
 assert.equal(document.querySelector('.qb-answer'),null);assert.equal(field('測試問題').value,'');assert.match(document.body.textContent,/TEST_RESULT_UNAVAILABLE/);assert.equal(document.querySelector('.qb-test-tracking').dataset.runId,'7');assert.equal(field('助手名稱').closest('fieldset').disabled,false);
});
