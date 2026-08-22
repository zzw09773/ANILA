// DK-1 + F-9 原始碼層護欄：建庫空名稱與裸 detail 的兩道帳單。
//
// 治理中心 runner 是 `node --test`（無 vitest／jsdom），沿用既有兩層作法：
// 這道守衛很短，純函式抽取反而多一層搬移，直接做原始碼層護欄，
// 與 healthOverview.test.mjs 的「新增檔案不得引入裸 data.detail」ratchet 同一形狀。
// 它釘的是呼叫端：DK-1 的空白名稱守衛若被刪掉，其他測試沒有一條會紅。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

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

test('建庫送出前必驗名稱非空（DK-1：空白名稱不再把 pydantic JSON 打上畫面）', () => {
  const source = stripComments(readSource('views/KnowledgeCollectionsView.vue'))

  assert.ok(source.includes('!form.value.name'), 'submitCreate 缺空白名稱前置 check')
  assert.ok(
    source.includes('請輸入名稱'),
    '空白名稱的回應應是人話，不是後端原始 JSON',
  )
  assert.ok(source.includes('createCollection'), '建庫 API 呼叫應仍在')
})

test('建庫失敗訊息走 extractError，不放裸 detail（F-9 同族清掃）', () => {
  const source = stripComments(readSource('views/KnowledgeCollectionsView.vue'))

  assert.ok(source.includes('extractError'), '建庫失敗應走 extractError 收斂')
  assert.ok(source.includes("from '../api/errors'"), 'extractError 應自 api/errors 匯入')
})
