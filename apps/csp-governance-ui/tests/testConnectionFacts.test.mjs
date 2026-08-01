// P2.1 — Agent 詳情「測試連線」三事實顯示。
//
// governance 沒有 vitest / jsdom（runner 是 `node --test tests/*.test.mjs`），
// 所以：
//   1. 三態對照抽成純函式測（helper 圍籬）
//   2. 用 @vue/server-renderer 把 DeveloperAgentsView 裡真實的 probe-facts
//      <dl> 抽出來 SSR，釘住「渲染結果」的三態誠實（模板圍籬）
//   3. 原始碼護欄釘住呼叫端（testAgentConnection / helper / 核准文案）

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createSSRApp, computed, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'

import {
  factLabel,
  factTone,
  formatTestConnectionFacts,
  resolveTestConnectionDetail,
} from '../src/utils/testConnectionFacts.js'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

/** 從真實 .vue 抽出 probe-facts <dl>（含綁定），供 SSR 驗渲染行為。 */
function extractProbeFactsTemplate(vueSource) {
  const start = vueSource.indexOf('<dl v-if="testConnectionView"')
  if (start < 0) throw new Error('probe-facts <dl> block not found in DeveloperAgentsView.vue')
  const end = vueSource.indexOf('</dl>', start)
  if (end < 0) throw new Error('probe-facts </dl> not found')
  return vueSource.slice(start, end + '</dl>'.length)
}

/** 以詳情頁同一條 computed 路徑 SSR 真實模板。 */
async function renderProbeFactsPanel(data) {
  const tpl = extractProbeFactsTemplate(readSource('views/DeveloperAgentsView.vue'))
  const app = createSSRApp({
    template: tpl,
    setup() {
      const testConnectionResult = ref(data)
      const testConnectionView = computed(() =>
        testConnectionResult.value ? formatTestConnectionFacts(testConnectionResult.value) : null,
      )
      return { testConnectionView }
    },
  })
  return renderToString(app)
}

function countClass(html, cls) {
  return (html.match(new RegExp(cls, 'g')) || []).length
}

test('credentials_accepted === null → 無法判定，且 tone 不是 ok', () => {
  assert.equal(factLabel(null), '無法判定')
  assert.equal(factTone(null), 'unknown')
  assert.notEqual(factTone(null), 'ok')

  const view = formatTestConnectionFacts({
    host_reachable: true,
    credentials_accepted: null,
    path_verified: null,
    status_code: 401,
    detail: '主機有回應（HTTP 401）。未驗證路徑。未驗證憑證。',
  })
  const creds = view.facts.find((f) => f.key === 'credentials_accepted')
  assert.ok(creds)
  assert.equal(creds.display, '無法判定')
  assert.equal(creds.tone, 'unknown')
  assert.notEqual(creds.tone, 'ok')
  assert.equal(view.detail, '主機有回應（HTTP 401）。未驗證路徑。未驗證憑證。')
  assert.equal(view.statusCode, 401)
})

test('path_verified === null → 無法判定（非失敗、非成功）', () => {
  assert.equal(factLabel(null), '無法判定')
  assert.equal(factTone(null), 'unknown')
  assert.notEqual(factTone(null), 'fail')
  assert.notEqual(factTone(null), 'ok')

  const view = formatTestConnectionFacts({
    host_reachable: true,
    credentials_accepted: null,
    path_verified: null,
    status_code: 401,
    detail: '未驗證路徑',
  })
  const path = view.facts.find((f) => f.key === 'path_verified')
  assert.equal(path.display, '無法判定')
  assert.equal(path.tone, 'unknown')
})

test('true → 是/ok；false → 否/fail', () => {
  assert.equal(factLabel(true), '是')
  assert.equal(factTone(true), 'ok')
  assert.equal(factLabel(false), '否')
  assert.equal(factTone(false), 'fail')

  const view = formatTestConnectionFacts({
    host_reachable: true,
    credentials_accepted: true,
    path_verified: false,
    status_code: 404,
    detail: '路徑未通過驗證',
  })
  assert.deepEqual(
    view.facts.map((f) => [f.label, f.display, f.tone]),
    [
      ['主機可連線', '是', 'ok'],
      ['憑證被接受', '是', 'ok'],
      ['路徑正確', '否', 'fail'],
    ],
  )
})

test('三事實標籤固定為主機可連線／憑證被接受／路徑正確', () => {
  const view = formatTestConnectionFacts({ host_reachable: false })
  assert.deepEqual(
    view.facts.map((f) => f.label),
    ['主機可連線', '憑證被接受', '路徑正確'],
  )
})

test('非物件 body／空 detail → 說明列有後備文案（不可空白）', () => {
  const htmlBody = formatTestConnectionFacts('<!doctype html><html>...</html>')
  assert.equal(htmlBody.facts.every((f) => f.display === '無法判定' && f.tone === 'unknown'), true)
  assert.notEqual(htmlBody.detail.trim(), '')
  assert.match(htmlBody.detail, /非 JSON|HTML/)

  const emptyDetail = formatTestConnectionFacts({
    host_reachable: true,
    credentials_accepted: null,
    path_verified: null,
    detail: '',
  })
  assert.notEqual(emptyDetail.detail.trim(), '')
  assert.equal(resolveTestConnectionDetail({}), '後端未提供說明文字')
})

test('SSR 真實模板：null／缺鍵不得渲染成成功態（is-ok／通過）', async () => {
  const allNull = await renderProbeFactsPanel({
    host_reachable: true,
    credentials_accepted: null,
    path_verified: null,
    status_code: 401,
    detail: '主機有回應（HTTP 401）。未驗證路徑。未驗證憑證。',
  })
  // 只有 host_reachable===true 可綠；憑證／路徑必須是 unknown＋無法判定。
  assert.equal(countClass(allNull, 'is-ok'), 1, 'null 事實不得帶 is-ok')
  assert.equal(countClass(allNull, 'is-unknown'), 2)
  assert.match(allNull, /憑證被接受<\/dt><dd class="probe-facts__val is-unknown">無法判定<\/dd>/)
  assert.match(allNull, /路徑正確<\/dt><dd class="probe-facts__val is-unknown">無法判定<\/dd>/)
  assert.doesNotMatch(allNull, /is-ok">通過/)
  assert.doesNotMatch(allNull, /is-ok">無法判定/)

  const missingKeys = await renderProbeFactsPanel({
    host_reachable: true,
    status_code: 405,
    detail: 'x',
  })
  assert.equal(countClass(missingKeys, 'is-ok'), 1)
  assert.equal(countClass(missingKeys, 'is-unknown'), 2)
  assert.match(missingKeys, /無法判定/)

  const realOk = await renderProbeFactsPanel({
    host_reachable: true,
    credentials_accepted: true,
    path_verified: true,
    status_code: 200,
    detail: 'ok',
  })
  assert.equal(countClass(realOk, 'is-ok'), 3)
  assert.equal(countClass(realOk, 'is-unknown'), 0)

  const nonObject = await renderProbeFactsPanel('<!doctype html><html>...</html>')
  assert.equal(countClass(nonObject, 'is-ok'), 0)
  assert.equal(countClass(nonObject, 'is-unknown'), 3)
  assert.match(nonObject, /probe-facts__detail">[^<]+/)
  assert.doesNotMatch(nonObject, /probe-facts__detail"><\/dd>/)
})

test('DeveloperAgentsView 接上 testAgentConnection 與 formatTestConnectionFacts', () => {
  const src = stripComments(readSource('views/DeveloperAgentsView.vue'))
  assert.match(src, /testAgentConnection/)
  assert.match(src, /formatTestConnectionFacts/)
  assert.match(src, /測試連線/)
  // 核准不得依賴探測結果——註解或文案仍要留下這點。
  assert.match(src, /核准不依賴/)
  // 模板必須把 class／文案綁到 helper 輸出，不可寫死成功態。
  assert.match(src, /probe-facts__val is-\$\{fact\.tone\}/)
  assert.match(src, /\{\{\s*fact\.display\s*\}\}/)
})

test('agents.js 有 test-connection wrapper', () => {
  const src = readSource('api/agents.js')
  assert.match(src, /testAgentConnection/)
  assert.match(src, /\/test-connection/)
})
