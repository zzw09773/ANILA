import test from 'node:test';
import assert from 'node:assert/strict';
import {readTestEventStream, readTestJsonStream} from '../src/quickBuilder/testStream.js';
import {createTestStreamState} from '../src/quickBuilder/testStreamState.js';
const context = {version:4, revision:2, request_id:'11111111-1111-4111-8111-111111111111'};
const graph = {nodes:[{id:'i',kind:'input'},{id:'m',kind:'model'},{id:'o',kind:'output'}],edges:[{source:'i',target:'m'},{source:'m',target:'o'}]};
const envelope = (seq,payload={}) => ({schema_version:1,run_id:12,version_id:4,revision:2,request_id:context.request_id,seq,payload});
const frame = (seq,event,payload) => ({event:`quick.${event}`,id:`12:${seq}`,data:envelope(seq,payload)});
const wire = value => `event: ${value.event}\nid: ${value.id}\ndata: ${JSON.stringify(value.data)}\n\n`;
const bytes = value => new TextEncoder().encode(value);
const stream = values => new ReadableStream({start(controller){for(const value of values)controller.enqueue(value);controller.close();}});
const finished = status => ({id:12,version_id:4,request_id:context.request_id,status,output:'回答',steps:[{node_id:'m',kind:'model',status:'completed',elapsed_ms:13,input:{api_key:'CANARY'},output:'完成'}]});
test('真 UTF8/CRLF 分片、heartbeat 與終局後 late frame 不再採用', async()=>{
  const text=': keep-alive\r\n\r\n'+wire(frame(1,'run_started',{status:'running'})).replaceAll('\n','\r\n')+wire(frame(2,'run_finished',{result:finished('completed')}))+wire(frame(3,'output_delta',{text:'LATE_CANARY'}));
  const state=createTestStreamState(context,graph);const source=bytes(text);let result;
  const done=await readTestEventStream(stream([...source].map(v=>new Uint8Array([v]))),{onEvent:event=>{result=state.apply(event);return result.terminal;}});
  assert.equal(done.events,2);assert.equal(result.finalResult.status,'completed');assert.doesNotMatch(JSON.stringify(result),/CANARY/);assert.equal(result.finalResult.steps[0].elapsed_ms,13);
});
test('step 真 started/finished、terminal delta 累積遮罩、完整 result 權威覆蓋',()=>{
 const state=createTestStreamState(context,graph);state.apply(frame(1,'run_started',{status:'running'}));
 let value=state.apply(frame(2,'step_started',{step:{node_id:'m',kind:'model',status:'running',elapsed_ms:0}}));assert.equal(value.result.steps[0].input,undefined);assert.equal(value.result.steps[0].status,'running');
 state.apply(frame(3,'output_delta',{node_id:'m',text:'api_'}));value=state.apply(frame(4,'output_delta',{node_id:'m',text:'key=SECRET_CANARY'}));assert.doesNotMatch(value.result.output,/SECRET_CANARY/);
 value=state.apply(frame(5,'step_finished',{step:finished('completed').steps[0]}));assert.equal(value.result.steps[0].elapsed_ms,13);assert.equal(value.result.steps.length,1);
 value=state.apply(frame(6,'run_finished',{result:finished('completed')}));assert.equal(value.result.output,'回答');assert.equal(value.finalResult.status,'completed');
 assert.equal(state.apply(frame(7,'output_delta',{text:'LATE'})).ignored,true);
});
test('seq gap/重複、版本UUID與未知節點均拒絕，不能偽造 failed',()=>{
 for(const mutate of [f=>f.data.seq=2,f=>f.data.version_id=8,f=>f.data.request_id='other',f=>f.id='12:9']){const state=createTestStreamState(context,graph),f=frame(1,'run_started',{status:'running'});mutate(f);assert.throws(()=>state.apply(f),/TEST_STREAM_/);assert.equal(state.result.status,'running');}
 const state=createTestStreamState(context,graph);state.apply(frame(1,'run_started',{status:'running'}));assert.throws(()=>state.apply(frame(1,'run_started',{status:'running'})),/SEQUENCE/);
 assert.throws(()=>state.apply(frame(2,'step_started',{step:{node_id:'secret',kind:'model',status:'running'}})),/CONTEXT/);
});
test('snapshot 去重不重派；compact result 必須 GET；subscription error 不宣告run失敗',()=>{
 let state=createTestStreamState(context,graph);let value=state.apply(frame(1,'run_snapshot',{result:{...finished('running'),steps:[]}}));assert.equal(value.terminal,true);assert.equal(value.result.status,'running');assert.equal(value.finalResult.status,'running');
 state=createTestStreamState(context,graph);value=state.apply(frame(1,'run_snapshot',{status:'completed',result_available:true}));assert.equal(value.needsResult,true);assert.equal(value.finalResult,null);
 state=createTestStreamState(context,graph);state.apply(frame(1,'run_started',{status:'running'}));value=state.apply(frame(2,'typed_error',{scope:'subscription',error:{type:'UNKNOWN_CANARY',message:'RAW_SECRET_CANARY'}}));assert.equal(value.terminal,true);assert.equal(value.result.status,'running');assert.doesNotMatch(JSON.stringify(value),/RAW_SECRET_CANARY/);
});
test('run error 只提示，finished 才終局；compact step 只標權威結果待查',()=>{
 const state=createTestStreamState(context,graph);state.apply(frame(1,'run_started',{status:'running'}));state.apply(frame(2,'step_started',{step:{node_id:'m',kind:'model',status:'running',elapsed_ms:0}}));
 let value=state.apply(frame(3,'step_finished',{step:{node_id:'m',kind:'model',status:'failed',elapsed_ms:14},result_available:true}));assert.equal(value.needsResult,true);assert.equal(value.result.steps[0].result_available,true);
 value=state.apply(frame(4,'typed_error',{scope:'run',error:{type:'MODEL_SERVICE_ERROR',message:'CANARY'}}));assert.equal(value.terminal,false);assert.equal(value.result.status,'running');assert.doesNotMatch(JSON.stringify(value),/CANARY/);
 value=state.apply(frame(5,'run_finished',{result:finished('failed')}));assert.equal(value.finalResult.status,'failed');
});
test('delta 只允許已開始且未完成的末端 model/format，不接受中間步驟',()=>{
 const other={...graph,edges:[{source:'m',target:'i'}]};const state=createTestStreamState(context,other);state.apply(frame(1,'run_started',{status:'running'}));state.apply(frame(2,'step_started',{step:{node_id:'m',kind:'model',status:'running'}}));assert.throws(()=>state.apply(frame(3,'output_delta',{node_id:'m',text:'秘密'})),/TEST_STREAM/);
});
test('EOF/未知事件/DONE/非法UTF8/有界frame/total/events 明確detach',async()=>{
 for(const value of ['event: unrelated\ndata: [DONE]\n\n','data: [DONE]\n\n',wire(frame(1,'run_started',{status:'running'}))])await assert.rejects(readTestEventStream(stream([bytes(value)]),{onEvent:()=>false}),/TEST_STREAM/);
 await assert.rejects(readTestEventStream(stream([new Uint8Array([255])]),{onEvent:()=>false}),/INVALID/);
 for(const limits of [{frameBytes:10},{totalBytes:10},{events:1}])await assert.rejects(readTestEventStream(stream([bytes(wire(frame(1,'run_started',{status:'running'}))+wire(frame(2,'run_started',{status:'running'})))]),{limits,onEvent:()=>false}),/LIMIT/);
});
test('await callback 背壓與停止等待只 cancel reader',async()=>{
 let reads=0,cancelled=0,release;const controller=new AbortController();
 const source={getReader:()=>({read:async()=>{reads++;return {done:false,value:bytes(wire(frame(reads,'run_started',{status:'running'})))};},cancel:async()=>{cancelled++;},releaseLock(){}})};
 const pending=readTestEventStream(source,{signal:controller.signal,onEvent:()=>new Promise(resolve=>release=resolve)});
 for(let i=0;i<10;i++)await Promise.resolve();assert.equal(reads,1);controller.abort();release(false);await assert.rejects(pending,/停止等待/);assert.ok(cancelled>=1);assert.equal(reads,1);
});
test('JSON fallback 有界讀取且合法內容保留',async()=>{
 assert.deepEqual(await readTestJsonStream(stream([bytes('{"status":"completed"}')])),{status:'completed'});
 await assert.rejects(readTestJsonStream(stream([bytes('CANARY')]),{maxBytes:3}),/LIMIT/);
});
test('GET checkpoint可先於第一個SSE到達，finished快照不得重複節點或錯revision',()=>{
 const state=createTestStreamState(context,graph);state.mergeSnapshot({...finished('running'),steps:[]});assert.equal(state.apply(frame(1,'run_started',{status:'running'})).run,12);
 assert.throws(()=>state.mergeSnapshot({...finished('completed'),revision:99}),/CONTEXT/);
 assert.throws(()=>state.mergeSnapshot({...finished('completed'),steps:[finished('completed').steps[0],finished('completed').steps[0]]}),/CONTEXT/);
});
test('subscription身份變更固定安全型別，可使UI立即鎖定且不抄自由message',()=>{
 const state=createTestStreamState(context,graph);const value=state.apply(frame(1,'typed_error',{scope:'subscription',error:{type:'IDENTITY_CHANGED',message:'SECRET_CANARY'}}));assert.equal(value.observerError.type,'IDENTITY_CHANGED');assert.doesNotMatch(JSON.stringify(value),/SECRET_CANARY/);
});
