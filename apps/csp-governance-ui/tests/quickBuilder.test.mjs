import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/dom.mjs'
import { createApp, nextTick } from 'vue'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { resolve, dirname } from 'node:path'
import { calls, reset, setFailure, setPending, setManageTemplates, changeActor, mutationRequests, holdDetail, releaseDetail, setRetrieval, setReadiness, failReadiness, holdReadiness, releaseReadiness, setTestSteps, setGraph } from './helpers/quickBuilderMock.mjs'
import { connect, removeNode, maskTrace, modules } from '../src/quickBuilder/graph.js'
import { RAG_DEFAULTS, RAG_MODES } from '../src/quickBuilder/ragSettings.js'
import { DOCUMENT_DEFAULTS, DOCUMENT_PRESETS, availableDocumentPresets } from '../src/quickBuilder/documentSettings.js'
const ROOT=resolve(dirname(fileURLToPath(import.meta.url)),'..')
let View, mounted
const settle=async()=>{for(let i=0;i<15;i++){await Promise.resolve();await nextTick()}}
const button=text=>[...document.querySelectorAll('button')].find(n=>n.textContent.trim()===text || n.firstChild?.textContent.trim()===text)
const field=text=>[...document.querySelectorAll('label')].find(n=>n.firstChild?.textContent.includes(text))?.querySelector('input,select,textarea')
async function change(node,value){assert.ok(node,'欄位存在');node.value=String(value);node.dispatchEvent(new Event(node.tagName==='SELECT'?'change':'input',{bubbles:true}));await settle()}
async function click(text){assert.ok(button(text),text);button(text).click();await settle()}
async function open(){await change(field('助手'), '11'); assert.ok(calls.find(c=>c[0]==='get'), JSON.stringify([...document.querySelectorAll('select')].map(s=>({value:s.value,html:s.innerHTML}))))}
test.before(async()=>{const require=createRequire(import.meta.url);const result=await build({configFile:false,envDir:false,root:ROOT,plugins:[{name:'builder-test-api',enforce:'pre',resolveId(id){if(id==='../api/quickBuilder')return{id:'builder-test-api',external:true}}},vue()],logLevel:'silent',build:{write:false,minify:false,lib:{entry:resolve(ROOT,'src/views/QuickBuilderView.vue'),formats:['es']},rollupOptions:{external:['vue','vue-router','builder-test-api'],output:{inlineDynamicImports:true,paths:{vue:pathToFileURL(require.resolve('vue')).href,'vue-router':pathToFileURL(require.resolve('vue-router')).href,'builder-test-api':new URL('./helpers/quickBuilderMock.mjs',import.meta.url).href}}}}});const chunk=(Array.isArray(result)?result[0]:result).output.find(o=>o.type==='chunk'&&o.isEntry);View=(await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default})
test.beforeEach(async()=>{reset();window.confirm=()=>true;const el=document.createElement('div');document.body.append(el);mounted=createApp(View);mounted.mount(el);await settle()})
test.afterEach(()=>{mounted.unmount();document.body.replaceChildren()})
test('畫布具成立與不成立分支，鍵盤移動後儲存帶正確版本',async()=>{await open();assert.equal(document.querySelectorAll('.qb-node').length,4);assert.match(document.querySelector('.qb-connections').textContent,/成立.*不成立/s);const title=document.querySelector('.qb-node-title');title.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));await settle();await click('儲存新版本');const call=calls.find(c=>c[0]==='save');assert.equal(call[2].expected_revision,2);assert.equal(call[2].graph.nodes[0].position.x,40);assert.equal(call[2].graph.edges[2].branch,'false');assert.match(document.body.textContent,/已儲存新版本/)})
test('表單連線拒絕重複出口，新增模組可編輯白話設定',async()=>{await open();await change(field('起點'),'condition');await change(field('終點'),'yes');await click('新增連線');assert.match(document.querySelector('[role=alert]').textContent,/EDGE_EXISTS/);await click('多步驟代理');assert.ok(field('最多步數'));assert.equal(field('最多步數').value,'5');await change(field('最多步數'),3);await click('儲存新版本');assert.equal(calls.find(c=>c[0]==='save')[2].graph.nodes.at(-1).config.max_steps,3)})
test('草稿變更未儲存不允許測試，儲存後逐步紀錄遮罩敏感內容',async()=>{await open();await change(field('助手名稱'),'新規章助手');await click('測試對話');await change(field('測試問題'),'請假需要什麼？');assert.equal(button('傳送測試').disabled,true);await click('儲存新版本');await click('傳送測試');assert.equal(calls.find(c=>c[0]==='test')[2].version_id,23);assert.match(document.body.textContent,/18 毫秒/);assert.match(document.body.textContent,/規章.pdf/);assert.match(document.body.textContent,/已遮罩/);assert.doesNotMatch(document.body.textContent,/never-display|private-token|private-key/);assert.equal(document.querySelector('a[download]').getAttribute('href'),'/api/quick-builder/runs/1/artifacts/0');assert.equal(document.querySelectorAll('a[href^="javascript:"]').length,0)})
test('舊版只讀且可回復新草稿，版本比較走指定版本',async()=>{await open();await click('版本與審核');await change(field('檢視版本'),21);assert.equal(field('助手名稱').closest('fieldset').disabled,true);await change(field('較早版本'),21);await change(field('較新版本'),22);await click('比較版本');assert.deepEqual(calls.find(c=>c[0]==='compare').slice(2).map(Number),[21,22]);await click('回復為新草稿');assert.equal(calls.find(c=>c[0]==='restore')[2].version_id,21);assert.match(document.body.textContent,/已另建回復草稿/)})
test('主管審核綁定指定版本，退回要理由，核准後可註冊',async()=>{setPending();await open();await click('版本與審核');assert.equal(button('附理由退回').disabled,true);await change(field('審核說明'),'條文來源確認完成');await click('核准這個版本');assert.deepEqual(calls.find(c=>c[0]==='review')[2],{version_id:22,approve:true,reason:'條文來源確認完成'});assert.equal(button('註冊為 Agent').disabled,false);await click('註冊為 Agent');assert.match(document.body.textContent,/仍需平台管理員核准/)})
test('版本衝突保留草稿，顯示白話原因、錯誤類型與恢復方式',async()=>{await open();await change(field('助手名稱'),'保留我的草稿');setFailure();await click('儲存新版本');assert.equal(field('助手名稱').value,'保留我的草稿');assert.match(document.querySelector('[role=alert]').textContent,/REVISION_CONFLICT.*重新整理/s)})
test('連線與資料遮罩不執行程式，拒絕循環與非型別分支',()=>{const g={nodes:[{id:'a',kind:'input'},{id:'b',kind:'condition'},{id:'c',kind:'format'}],edges:[]};connect(g,'a','b');assert.throws(()=>connect(g,'b','c',true),/EDGE_BRANCH/);connect(g,'b','c','true');assert.throws(()=>connect(g,'c','b'),/GRAPH_CYCLE/);removeNode(g,'b');assert.deepEqual(g.edges,[]);assert.equal(modules.length,11);assert.deepEqual(maskTrace({nested:{token:'bad'},text:'A123456789 0912345678'}),{nested:{token:'［已遮罩］'},text:'［身分證字號已遮罩］ ［手機已遮罩］'})})

test('管理員另存範本與文件公開分開確認，無權限時不顯示管理區',async()=>{assert.equal(field('範本名稱'),undefined);setManageTemplates();await click('重新整理');await change(field('範本名稱'),'單位查詢範本');await change(field('範本用途'),'查單位公開規章');await click('另存為新範本');assert.equal(calls.find(c=>c[0]==='createTemplate')[1].name,'單位查詢範本');await change(field('文件開放範圍'),'public');window.confirm=()=>false;await click('儲存文件開放範圍');assert.equal(calls.filter(c=>c[0]==='collectionPublication').length,0);window.confirm=()=>true;await click('儲存文件開放範圍');assert.deepEqual(calls.find(c=>c[0]==='collectionPublication').slice(1),[4,{scope:'public',target_department_ids:[]}])})
test('捨棄變更取消時助手選單仍維持原助手，草稿保留',async()=>{await open();await change(field('助手名稱'),'未存草稿');window.confirm=()=>false;await change(field('助手'),'');assert.equal(field('助手').value,'11');assert.equal(field('助手名稱').value,'未存草稿')})

test('另一分頁換帳號：寫入仍帶原actor且409後保稿鎖定，不能建立到另一人名下',async()=>{await change(field('助手名稱'),'帳號A的敏感草稿');await change(field('用途說明'),'A的單位資料');await change(field('平台模型'),'1');changeActor(202);await click('儲存新版本');assert.equal(calls.some(c=>c[0]==='create'),false);assert.equal(mutationRequests[0].options.headers['X-ANILA-Actor-Id'],'101');assert.equal(field('助手名稱').value,'帳號A的敏感草稿');assert.equal(field('用途說明').value,'A的單位資料');assert.match(document.body.textContent,/IDENTITY_CHANGED.*保留並鎖定/s);assert.equal(field('助手名稱').closest('fieldset').disabled,true);await click('儲存新版本');assert.equal(mutationRequests.length,1)})
test('重新整理發現帳號不同不重綁actor，也不覆蓋原草稿',async()=>{await change(field('助手名稱'),'保留A的草稿');changeActor(202);await click('重新整理');assert.equal(field('助手名稱').value,'保留A的草稿');assert.match(document.body.textContent,/IDENTITY_CHANGED/);assert.equal(button('儲存新版本').disabled,true);assert.equal(mutationRequests.length,0)})
test('/me身分通知改變會立即鎖定草稿，還沒按儲存也不會外洩',async()=>{await change(field('助手名稱'),'A草稿');window.dispatchEvent(new CustomEvent('anila:identity',{detail:{userId:202}}));await settle();assert.match(document.body.textContent,/IDENTITY_CHANGED/);assert.equal(button('儲存新版本').disabled,true);assert.equal(field('助手名稱').value,'A草稿');assert.equal(mutationRequests.length,0)})
test('重新整理等候回應期間鎖定所有編輯與建立操作',async()=>{await open();holdDetail();await click('重新整理');assert.equal(field('助手名稱').closest('fieldset').disabled,true);assert.equal(button('建立助手').disabled,true);assert.equal(button('儲存新版本').disabled,true);releaseDetail();await settle();assert.equal(field('助手名稱').value,'規章問答');assert.equal(field('助手名稱').closest('fieldset').disabled,false)})
test('未儲存時停用按鈕明確停用並說明恢復方式',async()=>{await open();await change(field('助手名稱'),'未存變更');await click('版本與審核');assert.equal(button('停用助手').disabled,true);assert.match(document.body.textContent,/請先儲存或捨棄變更.*停用助手/s);assert.equal(calls.some(c=>c[0]==='disable'),false)})
test('條件等於數字3與布林false保留真正型別，重新開啟也正確',async()=>{await open();[...document.querySelectorAll('.qb-node-title')].find(b=>b.textContent.trim()==='判斷').click();await settle();await change(field('比較方式'),'equals');await change(field('比較值型別'),'number');await change(field('比較內容'),'3');await click('儲存新版本');assert.equal(calls.find(c=>c[0]==='save')[2].graph.nodes.find(n=>n.id==='condition').config.value,3);[...document.querySelectorAll('.qb-node-title')].find(b=>b.textContent.trim()==='判斷').click();await settle();assert.equal(field('比較值型別').value,'number');await change(field('比較值型別'),'boolean');await change(field('比較內容'),'false');await click('儲存新版本');assert.equal(calls.filter(c=>c[0]==='save').at(-1)[2].graph.nodes.find(n=>n.id==='condition').config.value,false)})
test('唯讀工具必填boolean預設false會在未點選時回填並序列化',async()=>{await open();await click('工具查詢');await change(field('查詢工具'),'8');const flag=field('flag');assert.ok(flag);assert.equal(flag.checked,false);document.querySelector('button[aria-label="移除判斷到不成立輸出的連線"]').click();await settle();const mcpId=[...field('終點').options].find(o=>o.textContent.trim()==='工具查詢').value;await change(field('起點'),'condition');await change(field('條件出口'),'false');await change(field('終點'),mcpId);await click('新增連線');await change(field('起點'),mcpId);await change(field('終點'),'no');await click('新增連線');await click('儲存新版本');assert.deepEqual(calls.find(c=>c[0]==='save')[2].graph.nodes.find(n=>n.kind==='mcp').config.arguments,{flag:false})})

test('MCP文字與JSON字串中的秘密assignment均遮罩，不能只依物件key',()=>{const canary='REVIEW_CANARY';const result=maskTrace({'api.key':canary,content:[{type:'text',text:'api_key='+canary},{type:'text',text:'apikey: '+canary},{type:'text',text:'credential='+canary},{type:'text',text:'authorization: Basic '+canary},{type:'text',text:'cookie: session='+canary+'; tracking='+canary},{type:'text',text:'密碼：'+canary+'，憑證='+canary},{type:'text',text:JSON.stringify({nested:{text:'api-key='+canary},cookie:canary,api_key:canary})}],plain:'Bearer '+canary+' A123456789'});assert.doesNotMatch(JSON.stringify(result),new RegExp(canary));assert.match(JSON.stringify(result),/已遮罩/);assert.match(result.plain,/身分證字號已遮罩/);assert.equal(result.content[6].type,'text');assert.equal(JSON.parse(result.content[6].text).api_key,'［已遮罩］')})

async function addConnectedDocument() {
  await open(); await click('文件草稿');
  document.querySelector('button[aria-label="移除判斷到不成立輸出的連線"]').click(); await settle();
  const id=[...field('終點').options].find(o=>o.textContent.trim()==='文件草稿').value;
  await change(field('起點'),'condition'); await change(field('條件出口'),'false'); await change(field('終點'),id); await click('新增連線');
  await change(field('起點'),id); await change(field('終點'),'no'); await click('新增連線');
}
async function selectDocument() { [...document.querySelectorAll('.qb-node-title')].find(b=>b.textContent.trim()==='文件草稿').click(); await settle() }
test('文件節點預設保留RTF純文字契約、標題與固定範本預設',async()=>{await addConnectedDocument();assert.equal(field('文件產出方式').value,'text');assert.equal(field('文件標題').value,'助手草稿');assert.equal(field('Studio 文件範本'),undefined);assert.match(document.body.textContent,/輸出格式：RTF/);await click('儲存新版本');const config=calls.filter(c=>c[0]==='save').at(-1)[2].graph.nodes.find(n=>n.kind==='document').config;assert.deepEqual(config,DOCUMENT_DEFAULTS)})
test('Studio文件節點僅四種preset與HTML/PDF/DOCX，可留空範本文字並保留型別契約',async()=>{await addConnectedDocument();await change(field('文件產出方式'),'studio');assert.equal(field('文件輸出格式').value,'docx');assert.deepEqual([...field('Studio 文件範本').options].map(o=>o.value),DOCUMENT_PRESETS.map(p=>p.id));assert.deepEqual([...field('文件輸出格式').options].map(o=>o.value),['html','pdf','docx']);assert.equal(field('文件標題').maxLength,200);assert.equal(field('文件標題').required,true);await change(field('文件標題'),'教學驗證文件');await change(field('文件文字範本'),'');for(const [format,preset] of [['html','deep_tech_review'],['pdf','teaching_handout'],['docx','external_comms']]){await change(field('Studio 文件範本'),preset);await change(field('文件輸出格式'),format);await click('儲存新版本');const config=calls.filter(c=>c[0]==='save').at(-1)[2].graph.nodes.find(n=>n.kind==='document').config;assert.deepEqual(config,{template:'',template_id:null,mode:'studio',title:'教學驗證文件',preset,format});await selectDocument()}})
test('文件模式切回純文字必定改回RTF，不會把Studio格式送入text模式',async()=>{await addConnectedDocument();await change(field('文件產出方式'),'studio');await change(field('文件輸出格式'),'pdf');await change(field('文件產出方式'),'text');assert.equal(field('文件輸出格式'),undefined);await click('儲存新版本');const config=calls.filter(c=>c[0]==='save').at(-1)[2].graph.nodes.find(n=>n.kind==='document').config;assert.equal(config.mode,'text');assert.equal(config.format,'rtf')})
test('catalog不能將任意外掛或模板路徑加進Studio範本選單',()=>{const presets=availableDocumentPresets({document_presets:[{id:'custom-jinja',name:'外部自訂範本'},{id:'key_summary',name:'院內重點摘要'}]});assert.equal(presets.length,4);assert.equal(presets.find(p=>p.id==='key_summary').name,'院內重點摘要');assert.equal(presets.some(p=>p.id==='custom-jinja'),false)})

async function addConnectedRag() {
  await open(); await click('文件檢索');
  document.querySelector('button[aria-label="移除判斷到不成立輸出的連線"]').click(); await settle();
  const id=[...field('終點').options].find(o=>o.textContent.trim()==='文件檢索').value;
  await change(field('起點'),'condition'); await change(field('條件出口'),'false'); await change(field('終點'),id); await click('新增連線');
  await change(field('起點'),id); await change(field('終點'),'no'); await click('新增連線');
  const checkbox=[...document.querySelectorAll('label')].find(l=>l.textContent.includes('採購規章'))?.querySelector('input[type=checkbox]'); checkbox.click(); await settle();
}
test('RAG預設語意與關鍵字搜尋，保留文件集與片段上限契約',async()=>{await addConnectedRag();assert.equal(field('文件取得方式').value,'hybrid');assert.equal(field('最多取回幾段').value,'5');assert.equal(field('最多取回幾段').disabled,false);assert.deepEqual([...field('文件取得方式').options].map(o=>o.value),RAG_MODES.map(m=>m.id));await click('儲存新版本');const config=calls.filter(c=>c[0]==='save').at(-1)[2].graph.nodes.find(n=>n.kind==='rag').config;assert.deepEqual(config,{...RAG_DEFAULTS,collection_ids:[4]})})
test('小文件全文顯示伺服器上限與自動混合回退，top_k不截全文且模式可回復',async()=>{await addConnectedRag();await change(field('最多取回幾段'),'8');await change(field('文件取得方式'),'fulltext');assert.equal(field('最多取回幾段').disabled,true);assert.match(document.body.textContent,/最多 20 份文件、合計 10 萬字/);assert.match(document.body.textContent,/全文模式本身不以這個數字截斷文件/);await click('儲存新版本');let config=calls.filter(c=>c[0]==='save').at(-1)[2].graph.nodes.find(n=>n.kind==='rag').config;assert.deepEqual(config,{collection_ids:[4],top_k:8,mode:'fulltext'});[...document.querySelectorAll('.qb-node-title')].find(b=>b.textContent.trim()==='文件檢索').click();await settle();assert.equal(field('文件取得方式').value,'fulltext');await change(field('文件取得方式'),'hybrid');assert.equal(field('最多取回幾段').disabled,false);await click('儲存新版本');config=calls.filter(c=>c[0]==='save').at(-1)[2].graph.nodes.find(n=>n.kind==='rag').config;assert.equal(config.mode,'hybrid');assert.equal(config.top_k,8)})

test('逐步紀錄以白話顯示實際全文、超限改搜尋及不完整來源提醒並遮罩',async()=>{await open();setRetrieval([{metadata:{requested_mode:'fulltext',retrieval_mode:'fulltext'}},{metadata:{requested_mode:'fulltext',retrieval_mode:'hybrid',fulltext_fallback:true,fallback_reason:'fulltext_limit',quality_notice:'此簡報僅擷取部分內容；api_key=REVIEW_CANARY'}}]);await click('測試對話');await change(field('測試問題'),'檢查來源');await click('傳送測試');assert.match(document.body.textContent,/本次採用小文件全文/);assert.match(document.body.textContent,/已自動改用語意與關鍵字搜尋/);assert.match(document.body.textContent,/來源提醒：此簡報僅擷取部分內容/);assert.doesNotMatch(document.body.textContent,/REVIEW_CANARY/)})

test('合計模型內容超限而整個RAG省略時，以step context說明空檢索回退且不洩漏內部值',async()=>{await open();setRetrieval([],{requested_mode:'fulltext',retrieval_mode:'hybrid',fulltext_fallback:true,fallback_reason:'fulltext_model_context_limit',provider_debug:'encrypted:REVIEW_CANARY',credential:'REVIEW_CANARY',quality_notice:'僅供部分文件核對；api_key=REVIEW_CANARY'});await click('測試對話');await change(field('測試問題'),'檢查多步驟全文長度');await click('傳送測試');assert.match(document.body.textContent,/已自動改用語意與關鍵字搜尋/);assert.match(document.body.textContent,/合計內容超出模型可用長度/);assert.match(document.body.textContent,/這個步驟未提供文件片段/);assert.match(document.body.textContent,/來源提醒：僅供部分文件核對/);assert.ok([...document.querySelectorAll('.qb-section pre')].some(n=>n.textContent.trim()==='[]'));assert.doesNotMatch(document.body.textContent,/REVIEW_CANARY|provider_debug|fulltext_model_context_limit|encrypted:/)})

const readinessFixture=()=>({schema_version:1,scope:'current_process',service:'csp',status:'attention',checked_at:'2026-10-15T10:00:00Z',message:'/private/REVIEW_CANARY',checks:[...['pdf','image','docx','odt','presentation','source_code'].map(id=>({id,label:'REVIEW_CANARY',status:'ready',type:'ready',message:'/tmp/REVIEW_CANARY'})),{id:'libreoffice',status:'blocked',type:'office_converter_unavailable',missing:['libreoffice','/private/REVIEW_CANARY'],action:'REVIEW_CANARY'},{id:'tesseract',status:'blocked',type:'offline_ocr_languages_missing',missing:['chi_tra','eng'],message:'stderr REVIEW_CANARY'}],formats:[{id:'pdf_text',status:'ready',label:'REVIEW_CANARY',extensions:['/tmp/REVIEW_CANARY']},{id:'pdf_scanned',status:'blocked'},{id:'pptx',status:'ready'},{id:'legacy_presentation',status:'blocked'}],not_checked:['ingestion_worker','remote_docling','production_image'],env:'REVIEW_CANARY'})
async function openReadinessPanel(){setReadiness(readinessFixture());await click('重新整理');const panel=document.querySelector('.qb-readiness');assert.ok(panel);panel.open=true;await settle()}
test('一般建立者沒有離線文件診斷入口，也不會自動探測管理員端點',async()=>{assert.equal(document.querySelector('.qb-readiness'),null);assert.equal(calls.some(c=>c[0]==='readiness'),false)})
test('管理員手動檢查顯示缺binary與兩種OCR語言，隱藏路徑環境及原始錯誤',async()=>{await openReadinessPanel();assert.equal(calls.some(c=>c[0]==='readiness'),false);await click('檢查離線工具');const panel=document.querySelector('.qb-readiness');assert.match(panel.textContent,/Office 轉檔程式（LibreOffice）/);assert.match(panel.textContent,/繁體中文語言資料（chi_tra）/);assert.match(panel.textContent,/英文語言資料（eng）/);assert.match(panel.textContent,/offline_ocr_languages_missing/);assert.match(panel.textContent,/office_converter_unavailable/);assert.match(panel.textContent,/文字型 PDF/);assert.match(panel.textContent,/掃描型 PDF（需 OCR）/);assert.match(panel.textContent,/舊版 PowerPoint（需轉檔）/);assert.match(panel.textContent,/必要工具已就緒/);assert.match(panel.textContent,/尚缺必要工具/);assert.match(panel.textContent,/未初始化套件或實際解析文件/);assert.match(panel.textContent,/不代表背景工作程序、遠端 Docling/);assert.doesNotMatch(panel.textContent,/REVIEW_CANARY|stderr|private/);assert.equal(calls.filter(c=>c[0]==='readiness').length,1)})
test('慢速診斷不鎖草稿與儲存，換帳清除舊診斷且忽略晚到回覆',async()=>{await openReadinessPanel();await click('檢查離線工具');assert.ok(document.querySelector('.qb-readiness table'));holdReadiness();await click('檢查離線工具');assert.equal(button('正在檢查離線工具…').disabled,true);await change(field('助手名稱'),'診斷期間仍可編輯的草稿');assert.equal(field('助手名稱').closest('fieldset').disabled,false);assert.equal(button('儲存新版本').disabled,false);window.dispatchEvent(new CustomEvent('anila:identity',{detail:{userId:202}}));await settle();assert.equal(document.querySelector('.qb-readiness'),null);assert.equal(field('助手名稱').value,'診斷期間仍可編輯的草稿');releaseReadiness();await settle();assert.equal(document.querySelector('.qb-readiness'),null);assert.match(document.body.textContent,/IDENTITY_CHANGED/)})
test('診斷被拒絕保留助手草稿，白話錯誤附HTTP類型且不揭露服務原始內容',async()=>{await openReadinessPanel();await change(field('助手名稱'),'保留診斷失敗草稿');failReadiness(403);await click('檢查離線工具');const panel=document.querySelector('.qb-readiness');assert.match(panel.textContent,/目前沒有檢查權限.*HTTP_403/);assert.equal(panel.querySelector('table'),null);assert.equal(field('助手名稱').value,'保留診斷失敗草稿');assert.doesNotMatch(panel.textContent,/REVIEW_CANARY|stderr|private/)})

test('readiness不明schema使用固定錯誤，不顯示原服務資料',async()=>{await openReadinessPanel();setReadiness({schema_version:2,scope:'/private/REVIEW_CANARY',checks:[],message:'stderr REVIEW_CANARY'});await click('檢查離線工具');const panel=document.querySelector('.qb-readiness');assert.match(panel.textContent,/READINESS_INVALID_RESPONSE/);assert.doesNotMatch(panel.textContent,/REVIEW_CANARY|stderr|private/)})
test('模型調整用獨立白話事件，保留前面RAG全文紀錄且不顯示hash或內部reason',async()=>{await open();setTestSteps([{node_id:'in',elapsed_ms:5,input:'問題',output:'先前全文結果',retrieval:[{text:'原始全文步驟仍保持',metadata:{requested_mode:'fulltext',retrieval_mode:'fulltext'}}]},{node_id:'condition',elapsed_ms:8,input:'模型實際片段',output:'模型回答',context_replacements:[{type:'retrieval_context_replaced',source_node_id:'in',reason:'fulltext_model_context_limit',requested_mode:'fulltext',retrieval_mode:'hybrid',fulltext_fallback:true,context_limited:true,before:{chunk_count:4,bytes:70000,sha256:'HASH_REVIEW_CANARY'},after:{chunk_count:2,bytes:5000,sha256:'HASH_REVIEW_CANARY'}},{type:'retrieval_context_replaced',source_node_id:'REVIEW_CANARY',reason:'fulltext_model_context_limit',requested_mode:'hybrid',retrieval_mode:'hybrid',fulltext_fallback:false,context_limited:true,before:{chunk_count:2,bytes:5000,sha256:'HASH_REVIEW_CANARY'},after:{chunk_count:0,bytes:0,sha256:'HASH_REVIEW_CANARY'}}]}]);await click('測試對話');await change(field('測試問題'),'多個文件步驟');await click('傳送測試');const traces=[...document.querySelectorAll('.qb-section details')];assert.match(traces[0].textContent,/本次採用小文件全文/);assert.match(traces[0].textContent,/原始全文步驟仍保持/);assert.doesNotMatch(traces[0].textContent,/已改用語意與關鍵字搜尋|模型內容調整/);assert.match(traces[1].textContent,/模型內容調整/);assert.match(traces[1].textContent,/輸入：.*全文已改用語意與關鍵字搜尋/);assert.match(traces[1].textContent,/片段由 4 段調整為 2 段/);assert.match(traces[1].textContent,/未標示的文件步驟：.*此來源的片段已省略（原 2 段）/);assert.doesNotMatch(document.body.textContent,/REVIEW_CANARY|sha256|fulltext_model_context_limit/)})

test('缺LO保留office_converter_missing類型，格式不能越過blocked必要工具而誤報ready',async()=>{await openReadinessPanel();const fixture=readinessFixture();fixture.checks.find(c=>c.id==='libreoffice').type='office_converter_missing';fixture.formats.find(f=>f.id==='legacy_presentation').status='ready';setReadiness(fixture);await click('檢查離線工具');const panel=document.querySelector('.qb-readiness');assert.match(panel.textContent,/office_converter_missing/);assert.doesNotMatch(panel.textContent,/READINESS_UNKNOWN_STATUS/);const row=[...panel.querySelectorAll('.qb-readiness-formats tbody tr')].find(r=>r.textContent.includes('舊版 PowerPoint'));assert.equal(row.querySelector('td:last-child').textContent,'尚缺必要工具')})
test('readiness缺項、重複或未知工具id不得以8筆重複ready誤報整體就緒',async()=>{await openReadinessPanel();const original=readinessFixture();for(const checks of [original.checks.slice(1),Array.from({length:8},()=>({id:'pdf',status:'ready',type:'ready'})),original.checks.map((c,i)=>i===0?{...c,id:'/private/REVIEW_CANARY'}:c)]){setReadiness({...original,status:'ready',checks});await click('檢查離線工具');const panel=document.querySelector('.qb-readiness');assert.match(panel.textContent,/READINESS_INVALID_RESPONSE/);assert.equal(panel.querySelector('table'),null);assert.doesNotMatch(panel.textContent,/目前 CSP 程序的工具已就緒|REVIEW_CANARY/)}})

test('縮放及設定收合只改檢視，不產生草稿變更或修改選取步驟', async () => {
  await open()
  assert.equal(button('儲存新版本').disabled, true)
  await click('縮小')
  assert.equal(document.querySelector('output[aria-label="畫布縮放比例"]').textContent, '90%')
  await click('收合步驟設定')
  assert.equal(document.querySelector('.qb-config').style.display, 'none')
  document.querySelector('[aria-label="選取判斷；方向鍵移動"]').click()
  await settle()
  assert.equal(document.querySelector('.qb-config').style.display, 'none')
  assert.match(document.querySelector('.qb-canvas-status').textContent, /已選取：判斷.*已收合/)
  await click('顯示步驟設定')
  assert.equal(field('比較內容').value, '請假')
  await click('100%')
  assert.equal(document.querySelector('output').textContent, '100%')
  assert.equal(button('儲存新版本').disabled, true)
  assert.equal(mutationRequests.length, 0)
})

test('適合畫面與恢復100%保留版本資料及鍵盤可操作控制', async () => {
  await open()
  const viewport = document.querySelector('.qb-canvas-scroll')
  Object.defineProperties(viewport, {clientWidth: {configurable: true, value: 520}, clientHeight: {configurable: true, value: 600}})
  const before = [...document.querySelectorAll('.qb-node')].map(node => node.getAttribute('style'))
  await click('適合畫面')
  assert.equal(button('適合畫面').getAttribute('aria-pressed'), 'true')
  assert.ok(Number.parseInt(document.querySelector('output').textContent) >= 40)
  assert.deepEqual([...document.querySelectorAll('.qb-node')].map(node => node.getAttribute('style')), before)
  await click('100%')
  assert.equal(button('適合畫面').getAttribute('aria-pressed'), 'false')
  assert.equal(document.querySelector('output').textContent, '100%')
  assert.equal(button('儲存新版本').disabled, true)
  assert.equal(viewport.tabIndex, 0)
})

test('50%下拖移節點按畫布座標保存，鍵盤移動仍為20個畫布單位', async () => {
  await open()
  for (let i = 0; i < 5; i++) await click('縮小')
  const title = document.querySelector('[aria-label="選取輸入；方向鍵移動"]')
  title.dispatchEvent(new PointerEvent('pointerdown', {button: 0, clientX: 100, clientY: 100, bubbles: true}))
  title.dispatchEvent(new PointerEvent('pointermove', {clientX: 150, clientY: 125, bubbles: true}))
  title.dispatchEvent(new PointerEvent('pointerup', {bubbles: true}))
  title.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}))
  await settle()
  await click('儲存新版本')
  const node = calls.find(call => call[0] === 'save')[2].graph.nodes.find(node => node.id === 'in')
  assert.deepEqual(node.position, {x: 140, y: 80})
})

test('縮放且捲動後拖入模組正確扣除容器原點與邊框', async () => {
  await open()
  await click('整理排列')
  for (let i = 0; i < 5; i++) await click('縮小')
  const viewport = document.querySelector('.qb-canvas-scroll')
  viewport.getBoundingClientRect = () => ({left: 40, top: 60})
  Object.defineProperties(viewport, {clientLeft: {configurable: true, value: 2}, clientTop: {configurable: true, value: 2}})
  viewport.scrollLeft = 80
  viewport.scrollTop = 120
  const event = new Event('drop', {bubbles: true, cancelable: true})
  Object.defineProperties(event, {clientX: {value: 200}, clientY: {value: 160}, dataTransfer: {value: {getData: () => JSON.stringify({kind: 'format'})}}})
  viewport.dispatchEvent(event)
  await settle()
  await click('儲存新版本')
  const node = calls.find(call => call[0] === 'save')[2].graph.nodes.at(-1)
  assert.equal(node.kind, 'format')
  assert.deepEqual(node.position, {x: 376, y: 416})
})

test('整理排列明確改變位置，保留分支與設定且標籤有獨立底色元素', async () => {
  await open()
  await click('整理排列')
  assert.equal(button('儲存新版本').disabled, false)
  assert.match(document.body.textContent, /流程與設定不變/)
  assert.equal(document.querySelectorAll('.qb-edge-label rect').length, 2)
  await click('儲存新版本')
  const graph = calls.find(call => call[0] === 'save')[2].graph
  assert.deepEqual(graph.edges.map(edge => edge.branch), [undefined, 'true', 'false'])
  assert.deepEqual(graph.nodes.find(node => node.id === 'condition').config, {field: 'text', operator: 'contains', value: '請假'})
  assert.ok(graph.nodes.find(node => node.id === 'condition').position.y > graph.nodes.find(node => node.id === 'in').position.y)
})


test('長流程適合畫面可低於40%，完整框入頭尾且不改儲存位置', async () => {
  const nodes = Array.from({length: 10}, (_, index) => ({id: `n${index}`, kind: index === 0 ? 'input' : index === 9 ? 'output' : 'prompt', label: `步驟${index}`, position: {x: 24, y: 24 + index * 208}, config: {}}))
  setGraph({nodes, edges: nodes.slice(1).map((node, index) => ({source: nodes[index].id, target: node.id}))})
  await open()
  const viewport = document.querySelector('.qb-canvas-scroll')
  Object.defineProperties(viewport, {clientWidth: {configurable: true, value: 520}, clientHeight: {configurable: true, value: 600}})
  const before = [...document.querySelectorAll('.qb-node')].map(node => node.getAttribute('style'))
  await click('適合畫面')
  const scale = Number(document.querySelector('.qb-canvas').style.transform.match(/scale\(([^)]+)\)/)[1])
  assert.ok(scale < 0.4)
  assert.ok(2072 * scale <= 552)
  assert.equal(viewport.scrollTop, 0)
  assert.deepEqual([...document.querySelectorAll('.qb-node')].map(node => node.getAttribute('style')), before)
  assert.equal(button('儲存新版本').disabled, true)
})

test('遠離原點的既存流程適合畫面聚焦內容，保留100%與原位置', async () => {
  setGraph({nodes: [{id: 'start', kind: 'input', label: '遠處輸入', position: {x: 10000, y: 10000}, config: {}}, {id: 'end', kind: 'output', label: '遠處輸出', position: {x: 10000, y: 10208}, config: {}}], edges: [{source: 'start', target: 'end'}]})
  await open()
  const viewport = document.querySelector('.qb-canvas-scroll')
  Object.defineProperties(viewport, {clientWidth: {configurable: true, value: 520}, clientHeight: {configurable: true, value: 600}})
  await click('適合畫面')
  assert.equal(document.querySelector('output').textContent, '100%')
  assert.ok(viewport.scrollLeft > 9700 && viewport.scrollLeft < 10000)
  assert.ok(viewport.scrollTop > 9700 && viewport.scrollTop < 10000)
  assert.match(document.querySelector('.qb-node').getAttribute('style'), /10000px/)
  assert.equal(button('儲存新版本').disabled, true)
})

test('新流程連按模組庫三次各自放在空位，不覆蓋既有步驟', async () => {
  await click('建立助手')
  const before = [...document.querySelectorAll('.qb-node')].map(node => node.getAttribute('style'))
  for (let index = 0; index < 3; index++) await click('多步驟代理')
  assert.deepEqual([...document.querySelectorAll('.qb-node')].slice(0, 4).map(node => node.getAttribute('style')), before)
  await change(field('助手名稱'), '新增模組驗證')
  await change(field('平台模型'), '1')
  await click('儲存新版本')
  const nodes = calls.find(call => call[0] === 'create')[1].graph.nodes
  assert.equal(nodes.length, 7)
  for (let index = 0; index < nodes.length; index++) for (const other of nodes.slice(index + 1)) {
    const point = nodes[index].position, target = other.position
    assert.ok(point.x + 200 <= target.x || target.x + 200 <= point.x || point.y + 152 <= target.y || target.y + 152 <= point.y)
  }
})
