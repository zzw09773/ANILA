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

test('建庫表單有圖說開關與模型下拉，並寫出為什麼這顆設定要留', () => {
  const source = stripComments(readSource('views/KnowledgeCollectionsView.vue'))
  assert.ok(source.includes('captionMode'), '要有圖說意圖三態')
  assert.ok(source.includes('listModels'), '模型下拉沿用 GET /api/models')
  assert.ok(source.includes('沒有視覺能力過濾'), '不得假裝能過濾視覺模型')
  assert.ok(source.includes('抽換偵測'), '欄位旁邊要寫為什麼上線後不能等下次改版')
  assert.ok(source.includes('caption_enabled'), '送出要帶 caption_enabled')
})

test('文件列渲染 latest_job_progress_message，0 張圖與 N 張 0 成功才能看得見', () => {
  const source = stripComments(readSource('views/CollectionDetailView.vue'))
  assert.ok(
    source.includes('latest_job_progress_message'),
    '文件列要畫出匯入結果的 progress_message（含「N 張圖、0 張成功」）',
  )
})

test('wizard 建庫也送 caption_enabled，不得只改快速建立那條', () => {
  const source = stripComments(readSource('views/ChunkingPreviewView.vue'))
  assert.ok(source.includes('caption_enabled'), 'wizard 送出缺 caption_enabled')
  assert.ok(source.includes('listModels'), 'wizard 模型下拉也沿用 GET /api/models')
})

test('建庫失敗訊息走 extractError，不放裸 detail（F-9 同族清掃）', () => {
  const source = stripComments(readSource('views/KnowledgeCollectionsView.vue'))

  assert.ok(source.includes('extractError'), '建庫失敗應走 extractError 收斂')
  assert.ok(source.includes("from '../api/errors'"), 'extractError 應自 api/errors 匯入')
})
