import test from 'node:test';
import assert from 'node:assert/strict';
import {setupNeeds,stepSummary,resultText} from '../src/quickBuilder/builderGuidance.js';
import {readTestSession,saveTestSession} from '../src/quickBuilder/testSession.js';
const memory=()=>{const map=new Map();return{getItem:k=>map.get(k),setItem:(k,v)=>map.set(k,v),map}};

test('設定提示包含缺件與兩個條件出口，false和數字0是有效比較值',()=>{
  const form={name:'',model_id:'',graph:{nodes:[{id:'c',label:'差勤分流',kind:'condition',config:{operator:'equals',value:false}},{id:'r',label:'查文件',kind:'rag',config:{collection_ids:[]}}],edges:[]}};
  const issues=setupNeeds(form);
  assert.ok(issues.some(i=>i.key==='name'));assert.ok(issues.some(i=>i.key==='model'));
  assert.ok(issues.some(i=>i.message.includes('成立')));assert.ok(issues.some(i=>i.message.includes('不成立')));assert.ok(issues.some(i=>i.message.includes('文件集')));
  assert.ok(!issues.some(i=>i.message.includes('填寫要判斷')));
  form.graph.nodes[0].config.value=0;assert.ok(!setupNeeds(form).some(i=>i.message.includes('填寫要判斷')));
});
test('失敗與不完整先於步驟用途，無文字結果不把JSON當基本答案',()=>{
  assert.match(stepSummary({error:{type:'PROVIDER_ERROR'}},'model'),/未能完成/);
  assert.match(stepSummary({status:'incomplete',error:{type:'MODEL_OUTPUT_LIMIT'}},'model'),/尚未完整/);
  assert.equal(resultText({text:'完整文字'}),'完整文字');assert.doesNotMatch(resultText({internal:'DO_NOT_DISPLAY'}),/DO_NOT_DISPLAY/);
});
test('文字文件空範本會提示補件，Studio可直接排版前一步文字',()=>{
  const form={name:'通知草稿',model_id:1,graph:{nodes:[{id:'d',kind:'document',label:'文件草稿',config:{mode:'text',title:'通知',template:''}}],edges:[]}};
  assert.ok(setupNeeds(form).some(i=>i.nodeId==='d' && i.message.includes('填寫文件文字範本')));
  form.graph.nodes[0].config.mode='studio';assert.ok(!setupNeeds(form).some(i=>i.message.includes('填寫文件文字範本')));
});
test('多步驟代理還沒勾核准工具時提示補件',()=>{
  const form={name:'查詢助手',model_id:1,graph:{nodes:[{id:'a',kind:'agent',label:'查詢',config:{tool_ids:[]}}],edges:[]}};
  assert.ok(setupNeeds(form).some(i=>i.message.includes('至少一個允許查詢')));
  form.graph.nodes[0].config.tool_ids=[8];assert.ok(!setupNeeds(form).some(i=>i.message.includes('至少一個允許查詢')));
});
test('結果指標只保存白名單，不保存正文或跨帳號沿用',()=>{
  const store=memory();assert.equal(saveTestSession(store,{actor:1,assistant:2,version:3,state:'finished',run:4,question:'PRIVATE_CANARY',answer:'PRIVATE_CANARY',token:'PRIVATE_CANARY'}),true);
  assert.doesNotMatch([...store.map.values()].join(),/PRIVATE_CANARY|question|answer|token/);
  assert.deepEqual(readTestSession(store,1,2),{actor:1,assistant:2,version:3,state:'finished',run:4});assert.equal(readTestSession(store,8,2),null);assert.equal(readTestSession(store,1,8),null);
});
test('等待或未知可保留run和UUID，狀態不能冒充完成',()=>{
  for(const state of ['waiting','unknown']){const store=memory();saveTestSession(store,{actor:1,assistant:2,version:3,state,run:4});assert.deepEqual(readTestSession(store,1,2),{actor:1,assistant:2,version:3,state,run:4});}
});
test('非法或無法存取的瀏覽器儲存保守失敗，不阻止基本流程',()=>{
  const store=memory();for(const value of [null,{},[],{actor:1,assistant:2,version:3,state:'complete',run:4},{actor:1,assistant:2,version:3,state:'finished',run:0}]){store.setItem('anila:quick-builder:test:1:2',JSON.stringify(value));assert.equal(readTestSession(store,1,2),null);}
  const denied={getItem(){throw Error('denied')},setItem(){throw Error('denied')}};assert.equal(readTestSession(denied,1,2),null);assert.equal(saveTestSession(denied,{actor:1,assistant:2,version:3,state:'unknown'}),false);
});

test('識別碼只接受UUID並canonical，額外訊息不儲存',()=>{const store=memory();const request_id='12345678-ABCD-4234-8234-123456789ABC';assert.equal(saveTestSession(store,{actor:1,assistant:2,version:3,state:'waiting',run:4,request_id,question:'PRIVATE_CANARY'}),true);assert.equal(readTestSession(store,1,2).request_id,request_id.toLowerCase());assert.doesNotMatch([...store.map.values()].join(),/PRIVATE_CANARY/);assert.equal(saveTestSession(store,{actor:1,assistant:2,version:3,state:'waiting',request_id:'not-a-uuid'}),false)});
