// UX-IDEAS ③ 驗收:「等待核准」畫面得說得出去問誰。
//
// 為什麼是這個形狀的測試
// ----------------------
// governance 沒有 vitest / jsdom / @vue/test-utils(runner 是
// `node --test tests/*.test.mjs`),所以沿用本目錄已確立的兩層作法:
//
//   1. **行為層**:挑選邏輯抽成零依賴純函式 approvalContact.js,直接斷言
//      「有公告顯示公告 / 沒公告顯示保底窗口說明」。
//   2. **原始碼層護欄**:真正會壞的是**呼叫端** —— 純函式全綠,而 LoginView
//      忘了掛上那一行、或忘了去拉公告,畫面上還是死路一條,行為測試一個都
//      不會紅。所以直接讀 .vue 原始碼把呼叫端釘住。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  APPROVAL_CONTACT_FALLBACK,
  approvalContactNotice,
} from '../src/utils/approvalContact.js'

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

/** `GET /api/banners/active` 的真實形狀。 */
function banner(overrides = {}) {
  return {
    id: 1,
    level: 'info',
    content: '系統開通問題請洽資訊服務窗口分機 0000。',
    is_active: true,
    sort_order: 0,
    created_at: '2026-07-31T00:00:00Z',
    ...overrides,
  }
}

// ---- 行為層 ---------------------------------------------------------------

test('有啟用中的公告時,顯示公告內容', () => {
  const notice = approvalContactNotice([banner()])
  assert.equal(notice.source, 'banner')
  assert.equal(notice.text, '系統開通問題請洽資訊服務窗口分機 0000。')
  assert.equal(notice.level, 'info')
})

test('多則公告時取後端排好的第一則', () => {
  const notice = approvalContactNotice([
    banner({ id: 1, content: '第一則', sort_order: 0 }),
    banner({ id: 2, content: '第二則', sort_order: 1 }),
  ])
  assert.equal(notice.text, '第一則')
})

test('公告等級跟著出來,讓版面可以提亮', () => {
  assert.equal(approvalContactNotice([banner({ level: 'warning' })]).level, 'warning')
})

test('沒有公告時顯示保底窗口說明', () => {
  for (const empty of [[], null, undefined]) {
    const notice = approvalContactNotice(empty)
    assert.equal(notice.source, 'fallback')
    assert.equal(notice.text, APPROVAL_CONTACT_FALLBACK)
  }
})

test('空白／停用／壞掉的公告一律當作沒有公告', () => {
  const rows = [
    banner({ content: '   ' }),
    banner({ is_active: false, content: '已停用的公告' }),
    null,
    { id: 9 },
  ]
  assert.equal(approvalContactNotice(rows).text, APPROVAL_CONTACT_FALLBACK)
})

test('保底文案指得出方向,而且不含真人姓名／信箱／內網位址', () => {
  assert.match(APPROVAL_CONTACT_FALLBACK, /窗口/)
  assert.match(APPROVAL_CONTACT_FALLBACK, /平台管理員/)
  // 這個 repo 是公開的:保底文案不得夾帶 email 或 IP。
  assert.doesNotMatch(APPROVAL_CONTACT_FALLBACK, /@/)
  assert.doesNotMatch(APPROVAL_CONTACT_FALLBACK, /\d+\.\d+\.\d+\.\d+/)
})

// ---- 原始碼層護欄:LoginView 真的把它掛上去了 -----------------------------

test('LoginView 讀既有的公告 API,並把訊息掛在等待核准畫面上', () => {
  const source = stripComments(readSource('./views/LoginView.vue'))

  // 用既有端點,不另建設定機制。
  assert.match(source, /listActiveBanners/)
  assert.match(source, /approvalContactNotice/)
  assert.match(source, /fetchActiveBanners\(\)/)

  // 等待核准區塊真的渲染那一行。
  assert.match(source, /\{\{ approvalNotice\.text \}\}/)

  // 「等管理員核准」不再是句點 —— 同一段裡必須跟著窗口說明。
  const approvalBlock = source.slice(
    source.indexOf('一旦管理員核准'),
    source.indexOf('一旦管理員核准') + 400,
  )
  assert.match(approvalBlock, /approvalNotice\.text/)
})
