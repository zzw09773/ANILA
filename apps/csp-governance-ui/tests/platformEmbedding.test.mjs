// 平台主 embedding 指定：管理員到底有沒有被告知。
//
// 這一包的驗收 FAIL 就出在這裡：後端老老實實回了 `index_mismatch_warning`，
// ModelsView 只讀 `truncation_warning` 與 `measured_native_dim`，於是把整個
// 語料庫索引作廢的人看到的是綠色成功提示。送了沒人收的欄位，就是
// FAKE-CONTROLS 上那種「按了、沒報錯、什麼也沒發生」。
//
// 沿用 healthOverview.test.mjs 已確立的兩層作法：
//   1. 行為層：決策抽成零依賴純函式，直接 import 斷言。
//   2. 原始碼層護欄：真正會壞的是呼叫端 —— 純函式全綠而 ModelsView 忘了接，
//      行為測試一個都不會紅。這正是本包第一版的失效模式，所以直接讀 .vue。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  MISMATCH_TOAST_MS,
  currentPlatformEmbedding,
  designationConfirm,
  designationToast,
} from '../src/utils/platformEmbedding.js'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

/** 去掉 `//` 與 `<!-- -->` 註解 —— 註解裡提到的字不該讓 guard 假綠。 */
function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

/** 後端 `POST /api/models/{id}/set-platform-embedding` 的真實形狀。 */
function designationPayload(overrides = {}) {
  return {
    id: 7,
    name: 'nvidia/nv-embed-v2',
    truncation_warning: null,
    measured_native_dim: 4096,
    previous_embedding_model: null,
    index_mismatch_warning: null,
    stranded_collections: [],
    ...overrides,
  }
}

// ── 行為層：designationToast ────────────────────────────────────────────────

test('索引不一致時不得回綠色成功提示', () => {
  const notice = designationToast(designationPayload({
    index_mismatch_warning: '平台主 embedding 已設為「new/embedder」，但下列知識庫的索引是以其他模型建立的：法規庫。',
    stranded_collections: ['法規庫'],
  }))
  assert.ok(notice, '有不一致卻什麼都不顯示')
  assert.notEqual(notice.tone, 'ok', '把整個知識庫作廢的操作回了成功語氣')
  assert.equal(notice.tone, 'warn')
  assert.match(notice.message, /法規庫/)
})

test('索引不一致的提示要壓過維度截斷的提示', () => {
  const notice = designationToast(designationPayload({
    index_mismatch_warning: '索引模型不一致',
    truncation_warning: '維度截斷',
  }))
  assert.equal(notice.message, '索引模型不一致')
})

test('索引不一致的提示停留時間要比預設長', () => {
  const notice = designationToast(designationPayload({ index_mismatch_warning: 'x' }))
  assert.ok(notice.duration >= MISMATCH_TOAST_MS)
  assert.ok(notice.duration > 3600, 'useDialog 預設 3600ms，對這種嚴重度太短')
})

test('沒有不一致時維持原本的截斷／成功提示', () => {
  assert.equal(
    designationToast(designationPayload({ truncation_warning: '截斷 96 維' })).tone,
    'warn',
  )
  const ok = designationToast(designationPayload())
  assert.equal(ok.tone, 'ok')
  assert.match(ok.message, /4096/)
})

test('後端沒回任何可講的東西時不硬擠提示', () => {
  assert.equal(designationToast({}), null)
  assert.equal(designationToast(null), null)
})

// ── 行為層：designationConfirm ──────────────────────────────────────────────

const MODELS = [
  { id: 1, name: 'old/embedder', display_name: '舊向量模型', is_platform_embedding: true },
  { id: 2, name: 'new/embedder', display_name: '新向量模型', is_platform_embedding: false },
]

test('換一個模型要先攔下來，且訊息要指名前後兩端', () => {
  const gate = designationConfirm(MODELS, 2)
  assert.equal(gate.needed, true)
  assert.equal(gate.danger, true)
  assert.match(gate.message, /舊向量模型/)
  assert.match(gate.message, /新向量模型/)
  assert.match(gate.message, /重新索引|檢索不到/)
})

test('重新確認同一個模型不該有摩擦', () => {
  assert.equal(designationConfirm(MODELS, 1).needed, false)
})

test('本來就沒有主 embedding 時不攔（沒有舊索引會被作廢）', () => {
  const none = MODELS.map((m) => ({ ...m, is_platform_embedding: false }))
  assert.equal(designationConfirm(none, 2).needed, false)
})

test('清單還沒載入時不攔，也不炸', () => {
  assert.equal(designationConfirm(undefined, 2).needed, false)
  assert.equal(designationConfirm([], 2).needed, false)
  assert.equal(currentPlatformEmbedding(undefined), null)
})

// ── 原始碼層護欄：呼叫端真的接了 ─────────────────────────────────────────────

test('ModelsView 必須把 duration 交給 toast —— 否則退回 3600ms 預設', () => {
  // 純函式回了 duration 而呼叫端沒有轉交,是本包第一版失效模式的縮小版:
  // 送了沒人收。刪掉 ModelsView 那個 duration 就會紅在這裡。
  const source = stripComments(readSource('views/ModelsView.vue'))
  const handler = source.slice(
    source.indexOf('async function handleSetPlatformEmbed'),
    source.indexOf('async function handleUnsetPlatformEmbed'),
  )
  assert.match(
    handler,
    /toast\([^)]*duration:\s*notice\.duration/,
    'toast 沒有帶上 notice.duration,索引不一致的警告會在 3.6 秒後消失',
  )
})

test('ModelsView 必須把指定結果交給 designationToast 處理', () => {
  const source = stripComments(readSource('views/ModelsView.vue'))
  assert.match(source, /import \{[^}]*designationToast[^}]*\} from '\.\.\/utils\/platformEmbedding'/)
  assert.match(source, /designationToast\(data\)/)
})

test('ModelsView 不得再自己判斷 truncation_warning 而漏掉不一致', () => {
  const source = stripComments(readSource('views/ModelsView.vue'))
  const handler = source.slice(
    source.indexOf('async function handleSetPlatformEmbed'),
    source.indexOf('async function handleUnsetPlatformEmbed'),
  )
  assert.ok(handler.length > 0, '找不到 handleSetPlatformEmbed')
  assert.equal(
    (handler.match(/truncation_warning/g) || []).length,
    0,
    '呼叫端又自己讀 truncation_warning 了，嚴重度排序會再度繞過不一致提示',
  )
  assert.equal(
    (handler.match(/measured_native_dim/g) || []).length,
    0,
    '呼叫端又自己組成功提示了',
  )
})

test('ModelsView 指定主 embedding 前必須先 confirm', () => {
  const source = stripComments(readSource('views/ModelsView.vue'))
  const handler = source.slice(
    source.indexOf('async function handleSetPlatformEmbed'),
    source.indexOf('async function handleUnsetPlatformEmbed'),
  )
  assert.match(handler, /designationConfirm\(/)
  assert.match(handler, /await confirm\(/)
})
