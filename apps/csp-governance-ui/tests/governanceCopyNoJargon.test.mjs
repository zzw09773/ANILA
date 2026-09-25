// 擁有者 2026-09-02：治理中心是給長官看的（「軍人長官不能接受」），逐頁走查發現
// 幾頁的標題／副標還是工程師語彙（SSRF guard、s2s、latch、backfill、public 公告 banner、
// 資料表名當標題、讚／爛）。這條守衛把已經改掉的那幾個字釘住，免得下次又長回來。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const VIEWS = resolve(HERE, '../src/views')
const read = (f) => readFileSync(resolve(VIEWS, f), 'utf8')

const FORBIDDEN = {
  'BannersView.vue': ['公告 banner', 'content（公告內容）'],
  'ServiceAccessView.vue': ['platform_links 的', 'multi-service-integration'],
  'PlatformLinksView.vue': ['>服務登記<'],
  'TrustedHostsView.vue': ['SSRF guard', 'allow-list', 'single-label', 'audit log'],
  'ServiceClientsView.vue': ['s2s', 'admin-tool'],
  'FeedbackView.vue': ['爛'],
}

for (const [file, words] of Object.entries(FORBIDDEN)) {
  test(`${file}: no engineering jargon in what an officer reads`, () => {
    const src = read(file)
    for (const w of words) assert.ok(!src.includes(w), `${file} still contains "${w}"`)
  })
}
