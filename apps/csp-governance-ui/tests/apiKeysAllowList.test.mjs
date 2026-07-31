// 「讀不到允許清單」不得被呈現成「允許清單是空的」。
//
// 為什麼是這個形狀的測試
// ----------------------
// governance 沒有 vitest / jsdom / @vue/test-utils(runner 是
// `node --test tests/*.test.mjs`),所以沿用 healthOverview.test.mjs 已確立
// 的兩層作法:
//
//   1. **行為層**:讀取 → 畫面狀態的邏輯抽成零依賴純函式(utils/allowList.js),
//      直接 import 斷言;用一個必定 reject 的 fetcher 走 catch 路徑。
//   2. **原始碼層護欄**:真正會壞的是呼叫端 —— helper 全綠而 ApiKeysView 又
//      寫回 `catch {}`,行為測試一條都不會紅。所以直接讀 .vue 原始碼把
//      呼叫端釘住。
//
// 這是本週最嚴重缺陷(UsersView 允許清單靜默清空)的同形狀複本:讀取失敗時
// 落到「允許清單中沒有模型 · 請聯絡管理員」,和「這個人真的沒有模型」在畫面
// 上完全無法分辨。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  ALLOW_LIST_EMPTY,
  ALLOW_LIST_READY,
  ALLOW_LIST_UNREAD,
  allowListNotice,
  allowListStatus,
  allowListUsable,
  loadAllowList,
} from '../src/utils/allowList.js'

const COPY = {
  empty: '尚未指派模型 · 請聯絡管理員',
  unread: '讀不到你的允許清單 · 這不代表你沒有模型 · 請重新整理後再試',
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

test('讀取失敗 → unread,不是 empty', async () => {
  const result = await loadAllowList(async () => {
    throw new Error('Network Error')
  })
  assert.equal(result.loadFailed, true)
  assert.deepEqual(result.items, [])
  assert.equal(allowListStatus(result), ALLOW_LIST_UNREAD)
  assert.notEqual(allowListStatus(result), ALLOW_LIST_EMPTY)
})

test('讀取失敗的文案必須說「讀不到」,且與空清單文案不同', async () => {
  const failed = await loadAllowList(async () => {
    throw new Error('Network Error')
  })
  const empty = await loadAllowList(async () => ({ data: [] }))

  const failedNotice = allowListNotice(allowListStatus(failed), COPY)
  const emptyNotice = allowListNotice(allowListStatus(empty), COPY)

  assert.equal(failedNotice, COPY.unread)
  assert.equal(emptyNotice, COPY.empty)
  assert.notEqual(failedNotice, emptyNotice)
  assert.match(failedNotice, /讀不到/)
  // 「請聯絡管理員」是空清單的處置;讀取失敗要叫使用者重試,不是去排隊。
  assert.doesNotMatch(failedNotice, /尚未指派/)
})

test('讀不到時不可送出以允許清單為內容的表單', async () => {
  const failed = await loadAllowList(async () => {
    throw new Error('boom')
  })
  const empty = await loadAllowList(async () => ({ data: [] }))
  const ready = await loadAllowList(async () => ({ data: [{ id: 1 }] }))

  assert.equal(allowListUsable(allowListStatus(failed)), false)
  assert.equal(allowListUsable(allowListStatus(empty)), false)
  assert.equal(allowListUsable(allowListStatus(ready)), true)
  assert.equal(allowListStatus(ready), ALLOW_LIST_READY)
})

test('成功讀取時 loadFailed 為 false,非陣列 payload 收斂成空陣列', async () => {
  const ok = await loadAllowList(async () => ({ data: [{ id: 7 }, { id: 8 }] }))
  assert.equal(ok.loadFailed, false)
  assert.equal(ok.items.length, 2)

  const weird = await loadAllowList(async () => ({ data: null }))
  assert.equal(weird.loadFailed, false)
  assert.deepEqual(weird.items, [])
  assert.equal(allowListStatus(weird), ALLOW_LIST_EMPTY)
})

// ── 原始碼層護欄(呼叫端) ───────────────────────────────────────────────────

test('ApiKeysView 不得再用空的 catch 吞掉允許清單讀取失敗', () => {
  const src = stripComments(readSource('views/ApiKeysView.vue'))
  assert.doesNotMatch(
    src,
    /catch\s*(\([^)]*\))?\s*\{\s*\}/,
    'ApiKeysView 出現空的 catch —— 讀取失敗會再次偽裝成空的允許清單',
  )
})

test('ApiKeysView 兩份清單都要經過 loadAllowList', () => {
  const src = stripComments(readSource('views/ApiKeysView.vue'))
  assert.match(src, /loadAllowList\(\s*listModels\s*\)/)
  assert.match(src, /loadAllowList\(\s*getMyAllowedModels\s*\)/)
})

test('ApiKeysView 的空清單文案不得被硬寫死在模板裡', () => {
  const src = stripComments(readSource('views/ApiKeysView.vue'))
  // 模板只准插值 notice;把字串直接寫在 <p> 裡就等於讀取失敗也顯示同一句。
  assert.doesNotMatch(src, /<p[^>]*>\s*尚未指派模型/)
  assert.doesNotMatch(src, /<p[^>]*>\s*尚未註冊任何模型/)
  assert.match(src, /\{\{\s*myAllowedNotice\s*\}\}/)
  assert.match(src, /\{\{\s*allModelsNotice\s*\}\}/)
})

test('ApiKeysView 的建立按鈕以 allowListUsable 判定,不是看長度', () => {
  const src = stripComments(readSource('views/ApiKeysView.vue'))
  assert.match(src, /allowListUsable\(\s*myAllowedStatus\.value\s*\)/)
  assert.doesNotMatch(
    src,
    /myAllowedModels\.value\.length\s*===\s*0/,
    '用長度判定會把「讀不到」當成「沒有」',
  )
})
