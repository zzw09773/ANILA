// W3-3⑦ 驗收:服務健康總覽卡。
//
// 為什麼是這個形狀的測試
// ----------------------
// governance 沒有 vitest / jsdom / @vue/test-utils(runner 是
// `node --test tests/*.test.mjs`),所以這裡沿用 `apiErrors.test.mjs` 已經確立
// 的兩層作法:
//
//   1. **行為層**:邏輯抽成零依賴純函式,直接 import 斷言。
//   2. **原始碼層護欄**:真正會壞的是**呼叫端** —— helper 全綠而 DashboardView
//      忘了掛卡、或 AlertsView 忘了 onUnmounted,行為測試一個都不會紅。所以
//      直接讀 .vue 原始碼把呼叫端釘住。
//
// 第 2 層看起來笨,但它擋的正是本包最可能的失效模式:忘記清 timer 在畫面上
// 完全看不出來,而換頁十次就疊十支 setInterval 在打 /api/alerts。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  formatCheckedAt,
  healthTone,
  probeReasonLabel,
  summarizeHealthOverview,
  worstStatus,
} from '../src/utils/healthOverview.js'

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

/** 後端 `GET /api/admin/health/overview` 的真實形狀。 */
function overviewPayload(overrides = {}) {
  return {
    overall: 'healthy',
    checked_at: '2026-07-27T04:00:00Z',
    services: [
      { name: 'csp', label: '控制平面', kind: 'self', status: 'healthy', reason: 'ok', latency_ms: 0, checked_at: '2026-07-27T04:00:00Z' },
      { name: 'csp-db', label: '資料庫', kind: 'database', status: 'healthy', reason: 'ok', latency_ms: 2, checked_at: '2026-07-27T04:00:00Z' },
      { name: 'redis', label: '佇列與快取', kind: 'cache', status: 'healthy', reason: 'ok', latency_ms: 1, checked_at: '2026-07-27T04:00:00Z' },
      { name: 'nginx', label: '反向代理', kind: 'http', status: 'healthy', reason: 'ok', latency_ms: 3, checked_at: '2026-07-27T04:00:00Z' },
      { name: 'router', label: '對話路由', kind: 'http', status: 'healthy', reason: 'ok', latency_ms: 5, checked_at: '2026-07-27T04:00:00Z' },
      { name: 'ingestion-worker', label: '文件匯入工作者', kind: 'http', status: 'healthy', reason: 'ok', latency_ms: 4, checked_at: '2026-07-27T04:00:00Z' },
      { name: 'anila-studio', label: '簡報產生服務', kind: 'http', status: 'healthy', reason: 'ok', latency_ms: 6, checked_at: '2026-07-27T04:00:00Z' },
      { name: 'pptx-renderer', label: '簡報渲染服務', kind: 'http', status: 'healthy', reason: 'ok', latency_ms: 7, checked_at: '2026-07-27T04:00:00Z' },
    ],
    models: { total: 3, unknown: 0, healthy: 3, degraded: 0, unhealthy: 0, disabled: 0 },
    agents: { total: 1, unknown: 0, healthy: 1, degraded: 0, unhealthy: 0, disabled: 0 },
    ...overrides,
  }
}

// ── ⑦ 服務健康總覽:邏輯 ───────────────────────────────────────────────────

test('總覽把七個基礎服務 + csp 全部帶進卡片,並帶最後檢查時間', () => {
  const view = summarizeHealthOverview(overviewPayload())

  const names = view.services.map((s) => s.name)
  for (const expected of [
    'csp',
    'csp-db',
    'redis',
    'nginx',
    'router',
    'ingestion-worker',
    'anila-studio',
    'pptx-renderer',
  ]) {
    assert.ok(names.includes(expected), `總覽缺少 ${expected}`)
  }
  assert.equal(view.overall, 'healthy')
  assert.equal(view.tone, 'ok')
  assert.equal(view.hasProblem, false)
  assert.notEqual(view.checkedAtLabel, '—')
})

test('綠/黃/紅:一個服務異常 → 紅,一個降級 → 黃', () => {
  const red = summarizeHealthOverview(
    overviewPayload({
      overall: 'unhealthy',
      services: [
        { name: 'router', label: '對話路由', kind: 'http', status: 'unhealthy', reason: 'unreachable', latency_ms: 4001 },
        { name: 'csp', label: '控制平面', kind: 'self', status: 'healthy', reason: 'ok', latency_ms: 0 },
      ],
    }),
  )
  assert.equal(red.tone, 'danger')
  assert.equal(red.hasProblem, true)

  const yellow = summarizeHealthOverview(
    overviewPayload({
      overall: 'degraded',
      services: [
        { name: 'redis', label: '佇列與快取', kind: 'cache', status: 'degraded', reason: 'timeout', latency_ms: 4000 },
      ],
    }),
  )
  assert.equal(yellow.tone, 'warn')
  assert.equal(yellow.hasProblem, true)
})

test('載入失敗 / 尚未載入絕不顯示全綠(這是最糟的失效模式)', () => {
  for (const payload of [null, undefined, {}, { services: null }]) {
    const view = summarizeHealthOverview(payload)
    assert.notEqual(view.tone, 'ok', '沒資料時不得畫成綠燈')
    assert.equal(view.overall, 'unknown')
    assert.equal(view.checkedAtLabel, '—')
    assert.deepEqual(view.services, [])
  }
})

test('unknown 在總覽是黃燈(與模型清單頁的中性灰刻意不同)', () => {
  assert.equal(healthTone('unknown'), 'warn')
  assert.equal(healthTone('healthy'), 'ok')
  assert.equal(healthTone('degraded'), 'warn')
  assert.equal(healthTone('unhealthy'), 'danger')
  assert.equal(healthTone('disabled'), 'idle')
  // 後端字彙漂了也不准變綠。
  assert.equal(healthTone('totally-new-state'), 'warn')
  assert.equal(healthTone(undefined), 'warn')
})

test('worstStatus 取最差;空清單是 unknown 不是 healthy', () => {
  assert.equal(worstStatus([]), 'unknown')
  assert.equal(worstStatus(['healthy', 'healthy']), 'healthy')
  assert.equal(worstStatus(['healthy', 'degraded']), 'degraded')
  assert.equal(worstStatus(['degraded', 'unhealthy']), 'unhealthy')
  assert.equal(worstStatus(['healthy', 'unknown']), 'unknown')
  assert.equal(worstStatus(['healthy', 'disabled']), 'healthy')
})

test('bounded reason 有繁中對照;未知 reason 原樣顯示不臆測', () => {
  assert.equal(probeReasonLabel('ok'), '可連線')
  assert.equal(probeReasonLabel('timeout'), '探測逾時')
  assert.equal(probeReasonLabel('unreachable'), '無法連線')
  assert.equal(probeReasonLabel('not_deployed'), '此部署未啟用')
  assert.equal(probeReasonLabel('brand_new_reason'), 'brand_new_reason')
  assert.equal(probeReasonLabel(null), '—')
})

test('壞時間戳不得渲染成 Invalid Date', () => {
  assert.equal(formatCheckedAt('not-a-date'), '—')
  assert.equal(formatCheckedAt(''), '—')
  assert.equal(formatCheckedAt(null), '—')
  assert.match(formatCheckedAt('2026-07-27T04:00:00Z'), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
})

test('DB 掛掉時 models/agents 是 null,不補 0(0 是謊)', () => {
  const view = summarizeHealthOverview(overviewPayload({ models: null, agents: null }))
  assert.equal(view.models, null)
  assert.equal(view.agents, null)
})

// ── 原始碼層護欄:呼叫端真的掛上去了 ──────────────────────────────────────

test('首頁掛了服務健康卡', () => {
  const source = stripComments(readSource('views/DashboardView.vue'))

  assert.ok(source.includes('ServiceHealthCard'), '首頁沒掛服務健康卡')
  assert.ok(source.includes('getHealthOverview'), '首頁沒去打健康總覽 API')
  // 這張卡在 admin-only 區塊(非 admin 打這支 API 會 403)。
  assert.ok(source.includes('authStore.isAdmin'), '卡片必須限 admin')
})

test('健康總覽卡用共用 helper,不自己重寫五態判斷', () => {
  const source = stripComments(readSource('components/dashboard/ServiceHealthCard.vue'))
  assert.ok(source.includes('summarizeHealthOverview'), '卡片應走共用 helper')
  assert.ok(source.includes('最後檢查'), '卡片必須顯示最後檢查時間')
})

test('健康總覽 API 呼叫端不得傳任何參數(探測目標不可由前端指定)', () => {
  const source = stripComments(readSource('api/health.js'))
  assert.ok(source.includes('/api/admin/health/overview'))
  assert.ok(
    !source.includes('params'),
    'getHealthOverview 不得帶 params —— 後端對任何查詢參數 fail-closed 回 400',
  )
})

test('新增檔案不得引入裸 data.detail 插值(沿用 W2-12 的 ratchet 紀律)', () => {
  for (const relative of [
    'views/DashboardView.vue',
    'components/dashboard/ServiceHealthCard.vue',
    'utils/healthOverview.js',
    'api/health.js',
  ]) {
    const source = stripComments(readSource(relative))
    const hits = source.match(/response\??\.data\??\.detail/g) || []
    assert.equal(hits.length, 0, `${relative} 有裸 data.detail 插值`)
  }
})
