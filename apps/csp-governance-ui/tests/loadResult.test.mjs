// 「讀不到清單」不得被呈現成「清單是空的」。
//
// 為什麼是這個形狀的測試
// ----------------------
// governance 沒有 vitest / jsdom / @vue/test-utils（runner 是
// `node --test tests/*.test.mjs`），所以沿用 apiKeysAllowList.test.mjs
// 已確立的兩層作法：
//
//   1. **行為層**：讀取 → 畫面狀態的邏輯抽成零依賴純函式（utils/loadResult.js），
//      直接 import 斷言；用一個必定 reject 的 fetcher 走 catch 路徑。
//   2. **原始碼層護欄**：真正會壞的是呼叫端 —— helper 全綠而 UsageView /
//      UsersView 又寫回 `catch {}`，行為測試一條都不會紅。所以直接讀
//      .vue 原始碼把呼叫端釘住。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  LOAD_EMPTY,
  LOAD_FAILED,
  LOAD_READY,
  loadList,
  loadNotice,
  loadStatus,
  loadSucceeded,
} from '../src/utils/loadResult.js'

const COPY = {
  empty: '尚未註冊模型',
  failed: '讀不到模型清單 · 這不代表尚未註冊模型 · 請重新整理後再試',
}

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

// ── 行為層 ───────────────────────────────────────────────────────────────────

test('讀取失敗 → failed，不是 empty', async () => {
  const result = await loadList(async () => {
    throw new Error('Network Error')
  })
  assert.equal(result.loadFailed, true)
  assert.deepEqual(result.items, [])
  assert.equal(loadStatus(result), LOAD_FAILED)
  assert.notEqual(loadStatus(result), LOAD_EMPTY)
})

test('讀取失敗的文案必須說「讀不到」，且與空清單文案不同', async () => {
  const failed = await loadList(async () => {
    throw new Error('Network Error')
  })
  const empty = await loadList(async () => ({ data: [] }))

  const failedNotice = loadNotice(loadStatus(failed), COPY)
  const emptyNotice = loadNotice(loadStatus(empty), COPY)

  assert.equal(failedNotice, COPY.failed)
  assert.equal(emptyNotice, COPY.empty)
  assert.notEqual(failedNotice, emptyNotice)
  assert.match(failedNotice, /讀不到/)
  assert.doesNotMatch(failedNotice, /尚未註冊模型$/)
})

test('讀不到時不可拿這份清單覆寫後端；空清單仍是一次成功讀取', async () => {
  const failed = await loadList(async () => {
    throw new Error('boom')
  })
  const empty = await loadList(async () => ({ data: [] }))
  const ready = await loadList(async () => ({ data: [{ id: 1 }] }))

  assert.equal(loadSucceeded(loadStatus(failed)), false)
  assert.equal(loadSucceeded(loadStatus(empty)), true)
  assert.equal(loadSucceeded(loadStatus(ready)), true)
  assert.equal(loadStatus(ready), LOAD_READY)
  assert.equal(loadStatus(empty), LOAD_EMPTY)
})

test('成功讀取時 loadFailed 為 false，非陣列 payload 收斂成空陣列', async () => {
  const ok = await loadList(async () => ({ data: [{ id: 7 }, { id: 8 }] }))
  assert.equal(ok.loadFailed, false)
  assert.equal(ok.items.length, 2)

  const weird = await loadList(async () => ({ data: null }))
  assert.equal(weird.loadFailed, false)
  assert.deepEqual(weird.items, [])
  assert.equal(loadStatus(weird), LOAD_EMPTY)
})

// ── UsageView 呼叫端 ────────────────────────────────────────────────────────

test('UsageView Phase G 兩份 rollup 都要經過 loadList，不得 catch 後清空', () => {
  const src = stripComments(readSource('views/UsageView.vue'))
  assert.match(src, /loadList\(\s*\(\)\s*=>\s*client\.get\('\/api\/usage\/top-agents'/)
  assert.match(src, /loadList\(\s*\(\)\s*=>\s*client\.get\('\/api\/usage\/by-base-model'/)
  assert.doesNotMatch(
    src,
    /topAgentsResult\.value\s*=\s*\[\s*\]/,
    '失敗路徑不得把 Phase G Agent rollup 寫成空陣列',
  )
})

test('UsageView 空清單文案不變，失敗文案不得沿用空清單的字', () => {
  const src = stripComments(readSource('views/UsageView.vue'))
  assert.match(src, /尚無歸屬呼叫端的 Agent 用量（Phase G 前的資料顯示為未歸屬）/)
  assert.match(src, /尚無 Agent → 基礎模型的歸屬資料/)
  assert.match(src, /讀不到 Agent 用量/)
  assert.match(src, /讀不到基礎模型歸屬資料/)
  assert.doesNotMatch(
    src,
    /<TermEmpty[^>]*message="尚無歸屬呼叫端的 Agent 用量/,
    '空清單文案不得硬寫在模板裡，否則讀取失敗也會顯示同一句',
  )
  assert.match(src, /topAgentsStatus === LOAD_FAILED/)
  assert.match(src, /byBaseModelStatus === LOAD_FAILED/)
})

// ── UsersView 呼叫端 ────────────────────────────────────────────────────────

test('UsersView 三份目錄都要經過 loadList，不得再用空的 catch', () => {
  const src = stripComments(readSource('views/UsersView.vue'))
  assert.match(src, /loadList\(\s*listDepartments\s*\)/)
  assert.match(src, /loadList\(\s*listModels\s*\)/)
  assert.match(src, /loadList\(async \(\) =>/)
  assert.match(src, /client\.get\('\/api\/agents'\)/)
  assert.doesNotMatch(
    src,
    /catch\s*(\([^)]*\))?\s*\{\s*\}/,
    'UsersView 出現空的 catch —— 目錄讀取失敗會再次偽裝成空清單',
  )
})

test('UsersView 部門下拉失敗時不得假裝「— 無 —」', () => {
  const src = stripComments(readSource('views/UsersView.vue'))
  assert.match(src, /departmentsStatus === LOAD_FAILED/)
  assert.match(src, /\{\{\s*departmentsNotice\s*\}\}/)
  assert.match(src, /v-else v-model="form\.department_id"/)
  assert.match(src, /canSubmitUser/)
  assert.match(src, /departmentsStatus\.value === LOAD_FAILED/)
})

test('UsersView 模型／Agent modal 空清單文案不得硬寫死在模板裡', () => {
  const src = stripComments(readSource('views/UsersView.vue'))
  assert.doesNotMatch(src, /<p[^>]*>\s*尚未註冊模型/)
  assert.doesNotMatch(src, /<p[^>]*>\s*無已核准的 Agent/)
  assert.match(src, /\{\{\s*allModelsNotice\s*\}\}/)
  assert.match(src, /\{\{\s*allAgentsNotice\s*\}\}/)
  assert.match(src, /empty: '尚未註冊模型'/)
  assert.match(src, /empty: '無已核准的 Agent'/)
})

test('UsersView 目錄讀不到時儲存按鈕以 loadSucceeded 判定', () => {
  const src = stripComments(readSource('views/UsersView.vue'))
  assert.match(src, /loadSucceeded\(\s*allModelsStatus\s*\)/)
  assert.match(src, /loadSucceeded\(\s*allAgentsStatus\s*\)/)
  assert.match(src, /loadSucceeded\(allModelsStatus\.value\)/)
  assert.match(src, /loadSucceeded\(allAgentsStatus\.value\)/)
})
