import test from 'node:test';
import assert from 'node:assert/strict';
import './helpers/dom.mjs';
import { createApp, h, nextTick, reactive, ref } from 'vue';
import { build } from 'vite';
import vue from '@vitejs/plugin-vue';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve, dirname } from 'node:path';
import { modules, clone } from '../src/quickBuilder/graph.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
let NodeSettings, ToolParameter, app;
const catalog = {collections: [{id: 4, name: '差勤規章'}], mcp_tools: [{id: 8, name: '案件進度查詢', description: '查目前案件辦理進度', input_schema: {type: 'object', required: ['flag', 'code'], properties: {flag: {type: 'boolean', title: '包含結案案件', default: false}, code: {type: 'string', title: '案件編號'}, optional: {type: 'number', title: '最多幾筆'}}}}]};
async function settle() { for (let i = 0; i < 8; i++) {await Promise.resolve(); await nextTick();} }
async function compile(name) {
  const require = createRequire(import.meta.url);
  const result = await build({configFile: false, envDir: false, root: ROOT, plugins: [vue()], logLevel: 'silent', build: {write: false, minify: false, lib: {entry: resolve(ROOT, `src/quickBuilder/${name}.vue`), formats: ['es']}, rollupOptions: {external: ['vue'], output: {inlineDynamicImports: true, paths: {vue: pathToFileURL(require.resolve('vue')).href}}}}});
  const chunk = (Array.isArray(result) ? result[0] : result).output.find(item => item.type === 'chunk' && item.isEntry);
  return (await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default;
}
function field(text, selector = 'input,select,textarea') {
  return [...document.querySelectorAll('label')].find(label => label.firstChild?.textContent.includes(text))?.querySelector(selector);
}
async function change(el, value) {
  assert.ok(el, '設定欄位存在');
  el.value = String(value);
  el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', {bubbles: true}));
  await settle();
}
async function mountNode(kind, config = {}) {
  const original = modules.find(module => module.kind === kind)?.defaults || {};
  const node = reactive({id: `test_${kind}`, kind, label: '行政測試步驟', config: {...clone(original), ...config}});
  const root = document.createElement('div'); document.body.append(root);
  app = createApp(NodeSettings, {node, catalog, fields: ['天數']}); app.mount(root); await settle();
  return node;
}
async function mountParameter(schema, initial, required = true) {
  const value = ref(initial);
  const root = document.createElement('div'); document.body.append(root);
  app = createApp({setup: () => () => h(ToolParameter, {schema, label: '查詢條件', required, modelValue: value.value, 'onUpdate:modelValue': next => {value.value = next;}})});
  app.mount(root); await settle(); return value;
}
test.before(async () => {NodeSettings = await compile('NodeSettings'); ToolParameter = await compile('ToolParameter');});
test.afterEach(() => {app?.unmount(); app = null; document.body.replaceChildren();});

for (const {kind, defaults} of modules) {
  test(`${kind}顯示白話指南，展開進階不改原設定與預設值`, async () => {
    const node = await mountNode(kind);
    const guide = document.querySelector('[aria-label="步驟說明"]');
    assert.ok(guide); assert.match(guide.textContent, /需要什麼資料.*會交出什麼結果.*例子/s);
    assert.equal(JSON.stringify(node.config), JSON.stringify(defaults));
    for (const detail of document.querySelectorAll('details')) detail.open = true;
    await settle(); assert.equal(JSON.stringify(node.config), JSON.stringify(defaults));
    assert.equal(document.querySelector('textarea[placeholder*="JSON"]'), null);
  });
}
test('agent與RAG基本來源保持可見，數量與重試設定預設收合但仍可編輯', async () => {
  const node = await mountNode('agent');
  assert.equal(document.querySelector('fieldset').closest('details'), null);
  assert.match(document.querySelector('fieldset legend').textContent, /測試前必選/);
  const steps = field('最多步數'); assert.equal(steps.closest('details').open, false);
  steps.closest('details').open = true; await change(steps, 3); await change(field('最多重試次數'), 0);
  assert.deepEqual({...node.config}, {tool_ids: [], max_steps: 3, max_retries: 0});
});
test('RAG保留選取來源與搜尋模式，全文模式與進階片段數可來回調整', async () => {
  const node = await mountNode('rag');
  assert.equal(field('最多取回幾段').closest('details').open, false);
  assert.equal(document.querySelector('fieldset').closest('details'), null);
  document.querySelector('fieldset input').click(); await settle();
  await change(field('文件取得方式'), 'fulltext');
  assert.deepEqual([...node.config.collection_ids], [4]); assert.equal(field('最多取回幾段').disabled, true);
  await change(field('文件取得方式'), 'hybrid'); await change(field('最多取回幾段'), 8);
  assert.equal(node.config.top_k, 8);
});
test('條件比較型別收合但保留數字與false型別，基本比較內容仍可見', async () => {
  const node = await mountNode('condition');
  assert.equal(field('比較內容').closest('details'), null);
  assert.equal(field('比較值型別').closest('details').open, false);
  await change(field('比較值型別'), 'number'); await change(field('比較內容'), 3);
  assert.equal(node.config.value, 3);
  await change(field('比較值型別'), 'boolean'); await change(field('比較內容'), 'false');
  assert.equal(node.config.value, false);
});
test('工具基本區只列必填，選填可展開且required false持續回填', async () => {
  const node = await mountNode('mcp'); await change(field('查詢工具'), 8);
  const flag = field('包含結案案件'); assert.ok(flag); assert.equal(flag.closest('details'), null);
  assert.equal(flag.checked, false); assert.equal(node.config.arguments.flag, false);
  assert.match(flag.closest('label').textContent, /必填.*目前：否/s);
  const optional = field('最多幾筆'); assert.equal(optional.closest('details').open, false);
  optional.closest('details').open = true; await change(optional, 12);
  assert.equal(node.config.arguments.optional, 12);
});
test('Studio保留既有四範本與格式、替代文字預設收合，模式切換不丟文字', async () => {
  const node = await mountNode('document');
  assert.equal(field('文件文字範本').closest('details').open, true);
  await change(field('文件產出方式'), 'studio');
  assert.equal(field('文件文字範本').closest('details').open, false);
  assert.equal(field('文件輸出格式').value, 'docx');
  assert.equal(field('Studio 文件範本').options.length, 4);
  assert.match(field('文件標題').closest('label').textContent, /必填/);
  await change(field('文件文字範本'), '核銷說明：{{text}}');
  await change(field('文件產出方式'), 'text');
  assert.equal(node.config.template, '核銷說明：{{text}}'); assert.equal(node.config.format, 'rtf');
});
test('工具enum維持數字與boolean原型別，不把false當空值', async () => {
  const value = await mountParameter({type: 'integer', enum: [0, 3, false]}, undefined);
  const select = document.querySelector('select'); assert.equal(select.getAttribute('aria-required'), 'true');
  await change(select, '1'); assert.equal(value.value, 3);
  await change(select, '2'); assert.equal(value.value, false);
  await change(select, '0'); assert.equal(value.value, 0);
});
test('工具物件與陣列以欄位編輯、必填可讀，無需JSON輸入', async () => {
  const value = await mountParameter({type: 'object', required: ['flag', 'items'], properties: {flag: {type: 'boolean', default: false}, items: {type: 'array', minItems: 1, maxItems: 2, items: {type: 'integer'}}, note: {type: 'string'}}}, {});
  assert.equal(value.value.flag, false);
  const legends = [...document.querySelectorAll('legend')].map(item => item.textContent);
  assert.match(document.querySelector('input[type=checkbox]').closest('label').textContent, /flag（必填）/);
  assert.ok(legends.includes('items（必填）'));
  assert.match(document.body.textContent, /至少填寫 1 項/);
  [...document.querySelectorAll('button')].find(button => button.textContent.trim() === '新增一項').click(); await settle();
  await change(document.querySelector('input[type=number]'), 7);
  assert.deepEqual([...value.value.items], [7]);
  [...document.querySelectorAll('button')].find(button => button.textContent.includes('移除第')).click(); await settle();
  assert.deepEqual([...value.value.items], []);
  assert.equal(document.querySelector('textarea'), null);
  assert.equal(field('note').closest('details').open, false);
});
test('工具boolean必填的否不要求勾選成是，數字可清空不暗填0', async () => {
  const value = await mountParameter({type: 'object', required: ['flag', 'count'], properties: {flag: {type: 'boolean'}, count: {type: 'number', minimum: 0, maximum: 10}}}, {});
  const flag = document.querySelector('input[type=checkbox]');
  assert.equal(value.value.flag, false); assert.equal(flag.required, false); assert.equal(flag.getAttribute('aria-required'), 'true');
  await change(document.querySelector('input[type=number]'), 0); assert.equal(value.value.count, 0);
  await change(document.querySelector('input[type=number]'), ''); assert.equal(Object.hasOwn(value.value, 'count'), false);
});

test('工具文字可使用前一步或手動值，切換來源保留固定標記且不是程式碼輸入', async () => {
  const value = await mountParameter({type: 'string', maxLength: 50}, '案件A');
  const source = document.querySelector('select');
  await change(source, 'text');
  assert.equal(value.value, '{{text}}');
  assert.equal(document.querySelector('input').disabled, true);
  await change(source, 'literal');
  assert.equal(value.value, '');
  assert.equal(document.querySelector('input').disabled, false);
  await change(document.querySelector('input'), '案件B');
  assert.equal(value.value, '案件B');
  assert.equal(document.querySelector('input').maxLength, 50);
});

test('必填表示欄位必須提供，不額外把schema允許的空字串變成無效輸入', async () => {
  const value = await mountParameter({type: 'string'}, '');
  const input = document.querySelector('input');
  assert.equal(input.required, false);
  assert.equal(input.getAttribute('aria-required'), 'true');
  assert.equal(value.value, '');
  assert.match(input.closest('label').textContent, /必填/);
});
