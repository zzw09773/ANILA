import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/dom.mjs'
import { createApp,nextTick } from 'vue'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRequire } from 'node:module'
import { fileURLToPath,pathToFileURL } from 'node:url'
import { resolve,dirname } from 'node:path'
import { calls,reset,requestOptions } from './helpers/quickBuilderMcpMock.mjs'
const ROOT=resolve(dirname(fileURLToPath(import.meta.url)),'..')
let View,app
const settle=async()=>{for(let i=0;i<15;i++){await Promise.resolve();await nextTick()}}
const button=text=>[...document.querySelectorAll('button')].find(n=>n.textContent.trim()===text)
test.before(async()=>{const require=createRequire(import.meta.url);const result=await build({configFile:false,envDir:false,root:ROOT,plugins:[{name:'mcp-test-api',enforce:'pre',resolveId(id){if(id==='../api/client')return{id:'mcp-test-api',external:true}}},vue()],logLevel:'silent',build:{write:false,minify:false,lib:{entry:resolve(ROOT,'src/views/QuickBuilderMcpView.vue'),formats:['es']},rollupOptions:{external:['vue','mcp-test-api'],output:{inlineDynamicImports:true,paths:{vue:pathToFileURL(require.resolve('vue')).href,'mcp-test-api':new URL('./helpers/quickBuilderMcpMock.mjs',import.meta.url).href}}}}});const chunk=(Array.isArray(result)?result[0]:result).output.find(c=>c.type==='chunk'&&c.isEntry);View=(await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default})
test.beforeEach(async()=>{reset();window.confirm=()=>true;const el=document.createElement('div');document.body.append(el);app=createApp(View);app.mount(el);await settle()})
test.afterEach(()=>{app.unmount();document.body.replaceChildren()})
test('唯讀宣告不會自動核准，管理員明確勾選與確認後才送allowlist',async()=>{const approve=button('儲存唯讀核准清單');assert.equal(approve.disabled,true);const tool=document.querySelector('[aria-label="允許查假唯讀查詢"]');assert.equal(tool.checked,false);tool.click();await settle();assert.equal(approve.disabled,true);const confirmation=[...document.querySelectorAll('label')].find(n=>n.textContent.includes('我已確認所選工具僅供查詢')).querySelector('input');confirmation.click();await settle();assert.equal(approve.disabled,false);approve.click();await settle();assert.deepEqual(calls[0],{path:'/api/quick-builder/mcp/5/approve',body:{approved_tool_ids:[8]}});assert.equal(requestOptions[0].headers['X-ANILA-Actor-Id'],'101')})
test('重新擷取需確認，取消不呼叫；同意後清空核准確認',async()=>{window.confirm=()=>false;button('重新擷取工具').click();await settle();assert.equal(calls.length,0);window.confirm=()=>true;button('重新擷取工具').click();await settle();assert.equal(calls[0].path,'/api/quick-builder/mcp/5/refresh');assert.equal(button('儲存唯讀核准清單').disabled,true);assert.match(document.body.textContent,/請重新確認唯讀核准清單/)})

test('管理工具草稿收到/me換帳號通知後保留內容並鎖定',async()=>{const name=[...document.querySelectorAll('label')].find(l=>l.firstChild?.textContent.includes('服務名稱')).querySelector('input');name.value='A的MCP草稿';name.dispatchEvent(new Event('input',{bubbles:true}));window.dispatchEvent(new CustomEvent('anila:identity',{detail:{userId:202}}));await settle();assert.equal(name.value,'A的MCP草稿');assert.equal(name.closest('fieldset').closest('.qb-content').disabled,true);assert.match(document.body.textContent,/IDENTITY_CHANGED/);assert.equal(calls.length,0)})
