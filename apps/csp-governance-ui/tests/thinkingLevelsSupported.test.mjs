// 治理中心模型頁：思考等級探測結果與「允許使用者自選」開關。
//
// 這個 app 沒有 vitest／Vue Test Utils（package.json 是 `node --test`），
// 行為層測純函式；畫面層用原始碼護欄釘住 ModelsView 真的有接。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  THINKING_LEVEL_MAX_CHIPS,
  formatThinkingLevel,
  thinkingEffortOptionLabel,
  thinkingLevelsView,
  thinkingUserSelectableFromModel,
  withThinkingWriteFields,
} from '../src/utils/thinkingLevels.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(resolve(HERE, rel), 'utf8')

// 只去掉行註解與 HTML 註解。不剝區塊註解：ModelsView 文案含 /v1/* ，誤剝會刪掉後半檔。
function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

const VIEW_RAW = read('../src/views/ModelsView.vue')
const VIEW = stripComments(VIEW_RAW)
const DISPLAY = stripComments(read('../src/components/ThinkingLevelsDisplay.vue'))
const API = read('../src/api/models.js')
const STORE = read('../src/stores/models.js')

// ── 未探測／chips ──────────────────────────────────────────────────────────

test('null 支援集顯示為未探測', () => {
  assert.deepEqual(thinkingLevelsView(null), {
    status: 'unprobed',
    visible: [],
    overflow: 0,
  })
  assert.deepEqual(thinkingLevelsView(undefined), {
    status: 'unprobed',
    visible: [],
    overflow: 0,
  })
})

test('none 在 chip 上顯示為「關」', () => {
  assert.equal(formatThinkingLevel('none'), '關')
  assert.equal(formatThinkingLevel('off'), '關')
  assert.equal(formatThinkingLevel('xhigh'), 'xhigh')
})

test('超過 4 個等級折成 +N，前 4 個依後端順序保留', () => {
  const levels = ['none', 'low', 'medium', 'high', 'xhigh', 'max']
  const view = thinkingLevelsView(levels)
  assert.equal(view.status, 'probed')
  assert.deepEqual(view.visible, ['none', 'low', 'medium', 'high'])
  assert.equal(view.overflow, 2)
  assert.equal(THINKING_LEVEL_MAX_CHIPS, 4)
})

test('剛好 4 個不折疊', () => {
  const view = thinkingLevelsView(['none', 'low', 'medium', 'xhigh'])
  assert.deepEqual(view.visible, ['none', 'low', 'medium', 'xhigh'])
  assert.equal(view.overflow, 0)
})

test('空陣列與 null 一樣顯示為未探測', () => {
  assert.deepEqual(thinkingLevelsView([]), {
    status: 'unprobed',
    visible: [],
    overflow: 0,
  })
})

// ── 下拉標「（端點不接受）」────────────────────────────────────────────────

test('已探測時，不在支援集的選項標「（端點不接受）」但仍回可選字串', () => {
  const supported = ['none', 'low', 'medium', 'xhigh']
  assert.equal(thinkingEffortOptionLabel('medium', supported), 'medium')
  assert.equal(thinkingEffortOptionLabel('none', supported), 'NONE')
  assert.equal(thinkingEffortOptionLabel('high', supported), 'high（端點不接受）')
  assert.equal(thinkingEffortOptionLabel('max', supported), 'max（端點不接受）')
})

test('未探測時下拉不加「端點不接受」', () => {
  assert.equal(thinkingEffortOptionLabel('high', null), 'high')
  assert.equal(thinkingEffortOptionLabel('max', undefined), 'max')
  assert.equal(thinkingEffortOptionLabel('high', []), 'high')
})

// ── 開關送出 ──────────────────────────────────────────────────────────────

test('開關送出 thinking_user_selectable，且不回送探測結果', () => {
  const payload = withThinkingWriteFields(
    {
      thinking_effort: 'max',
      thinking_levels_supported: ['none', 'low', 'medium', 'xhigh'],
      thinking_user_selectable: true,
    },
    { thinking_user_selectable: false },
  )
  assert.equal(payload.thinking_user_selectable, false)
  assert.equal(payload.thinking_effort, 'max')
  assert.equal('thinking_levels_supported' in payload, false)
})

test('漏填開關時預設開啟，不改 thinking_effort', () => {
  const payload = withThinkingWriteFields(
    { thinking_effort: 'low' },
    {},
  )
  assert.equal(payload.thinking_user_selectable, true)
  assert.equal(payload.thinking_effort, 'low')
})

test('舊列沒有 thinking_user_selectable 時表單當開啟', () => {
  assert.equal(thinkingUserSelectableFromModel({}), true)
  assert.equal(thinkingUserSelectableFromModel({ thinking_user_selectable: false }), false)
  assert.equal(thinkingUserSelectableFromModel({ thinking_user_selectable: true }), true)
})

// ── 畫面／API 護欄 ─────────────────────────────────────────────────────────

test('列表與編輯表單都用 ThinkingLevelsDisplay，未探測文案在元件裡', () => {
  assert.match(DISPLAY, /未探測/)
  assert.match(DISPLAY, /variant="muted"/)
  assert.match(VIEW, /<th[^>]*>支援等級<\/th>/)
  assert.match(VIEW, /<ThinkingLevelsDisplay :levels="model\.thinking_levels_supported"/)
  assert.match(VIEW, /<ThinkingLevelsDisplay :levels="form\.thinking_levels_supported"/)
})

test('編輯表單有重新探測按鈕與自選開關', () => {
  assert.match(VIEW, /重新探測/)
  assert.match(VIEW, /handleProbeThinking/)
  assert.match(VIEW, /允許使用者自選思考程度/)
  assert.match(VIEW, /v-model="form\.thinking_user_selectable"/)
  assert.match(
    VIEW,
    /關閉後，ANILA 對話中的思考選單對此模型鎖定，改用上方的預設思考程度。/,
  )
})

test('buildModelPayload 經 withThinkingWriteFields 送出開關', () => {
  const handler = VIEW.slice(
    VIEW.indexOf('function buildModelPayload'),
    VIEW.indexOf('function noticeThinkingProbe'),
  )
  assert.match(handler, /withThinkingWriteFields/)
  assert.doesNotMatch(
    handler,
    /delete payload\.thinking_user_selectable/,
    '不可把開關從存檔 payload 拿掉',
  )
})

test('thinking_effort 下拉在已探測時標不支援選項', () => {
  assert.match(VIEW, /thinkingEffortOptionLabel\(opt\.value, form\.thinking_levels_supported\)/)
})

test('API 與 store 有 probeThinking', () => {
  assert.match(API, /export const probeThinking = \(id\) =>/)
  assert.match(API, /\/api\/models\/\$\{id\}\/probe-thinking/)
  assert.match(STORE, /probeThinking/)
  assert.match(STORE, /probeThinkingApi/)
})

test('重新探測失敗走 extractError，不直接讀 response.data.detail', () => {
  const start = VIEW_RAW.indexOf('async function handleProbeThinking')
  assert.notEqual(start, -1, '找不到 handleProbeThinking')
  const handler = VIEW_RAW.slice(start, VIEW_RAW.indexOf('async function handleSetPrimary'))
  assert.match(handler, /extractError\(/)
  assert.doesNotMatch(handler, /response\?\.data\?\.detail/)
})
