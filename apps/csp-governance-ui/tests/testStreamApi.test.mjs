import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'vite';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {respond,requests} from './helpers/testStreamClientMock.mjs';
const ROOT=resolve(dirname(fileURLToPath(import.meta.url)),'..');
let api;
const stream=text=>new ReadableStream({start(controller){controller.enqueue(new TextEncoder().encode(text));controller.close();}});
test.before(async()=>{
 const bundle=await build({configFile:false,envDir:false,root:ROOT,logLevel:'silent',plugins:[{name:'stream-api-client',enforce:'pre',resolveId(id){if(id==='./client')return{id:'test-stream-client',external:true};}}],build:{write:false,minify:false,lib:{entry:resolve(ROOT,'src/api/quickBuilder.js'),formats:['es']},rollupOptions:{external:['test-stream-client'],output:{paths:{'test-stream-client':new URL('./helpers/testStreamClientMock.mjs',import.meta.url).href}}}}});
 api=await import(`data:text/javascript;base64,${Buffer.from((Array.isArray(bundle)?bundle[0]:bundle).output.find(item=>item.type==='chunk').code).toString('base64')}`);
});
test('SSE POST保留UUID/actor/abort，使用既有client fetch adapter不獨立繞認證',async()=>{
 const source=stream('event: quick.run_started\n\n'),signal=new AbortController().signal;
 respond({headers:{'content-type':'text/event-stream; charset=utf-8'},data:source});
 const response=await api.streamAssistantTest(11,{version_id:4,message:'合成問題',request_id:'same-uuid'},{headers:{'X-ANILA-Actor-Id':'101'},signal});
 assert.equal(response.data,source);assert.deepEqual(requests[0].body,{version_id:4,message:'合成問題',request_id:'same-uuid',stream:true});assert.equal(requests[0].options.headers['X-ANILA-Actor-Id'],'101');assert.equal(requests[0].options.headers.Accept,'text/event-stream');assert.equal(requests[0].options.signal,signal);assert.equal(requests[0].options.adapter,'fetch');
});
test('JSON fallback接受標準JSON，拒絕HTML並有界解析409身份錯誤',async()=>{
 respond({headers:{'content-type':'application/json'},data:stream('{"id":7,"status":"completed"}')});assert.equal((await api.streamAssistantTest(11,{})).data.id,7);
 respond({headers:{'content-type':'text/html'},data:stream('<html>CANARY</html>')});await assert.rejects(api.streamAssistantTest(11,{}),/TEST_STREAM_INVALID/);
 const error=Object.assign(new Error('409'),{response:{status:409,data:stream('{"detail":{"type":"IDENTITY_CHANGED","message":"固定"}}')}});respond(error);await assert.rejects(api.streamAssistantTest(11,{}),value=>value.response.data.detail.type==='IDENTITY_CHANGED');
});
test('cancel POST exact識別和actor原樣保存，沒有stream或重新test',async()=>{
 respond({data:{status:'cancelled'}});await api.cancelTestRun(7,{version_id:4,request_id:'same-uuid'},{headers:{'X-ANILA-Actor-Id':'101'}});assert.equal(requests[0].url,'/api/quick-builder/runs/7/cancel');assert.deepEqual(requests[0].body,{version_id:4,request_id:'same-uuid'});assert.equal(requests[0].options.headers['X-ANILA-Actor-Id'],'101');
});
