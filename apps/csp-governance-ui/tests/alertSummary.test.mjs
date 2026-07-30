// W3-3④ 驗收:告警摘要卡 + 警報頁 30 秒輪詢(air-gapped 首版通知)。
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
  alertSummaryHeadline,
  alertSummaryTone,
  isAlertSummaryUrgent,
  normalizeAlertSummary,
} from '../src/utils/alertSummary.js'
import { ALERT_POLL_INTERVAL_MS, createPoller } from '../src/utils/polling.js'

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

// ── ④ 告警摘要:high_count > 0 要有視覺區別 ────────────────────────────────

test('high_count > 0 → danger tone + 行動指引(與平時視覺不同)', () => {
  const calm = { open_count: 0, acknowledged_count: 0, resolved_count: 12, high_count: 0 }
  const hot = { open_count: 4, acknowledged_count: 1, resolved_count: 12, high_count: 3 }

  assert.equal(alertSummaryTone(calm), 'ok')
  assert.equal(alertSummaryTone(hot), 'danger')
  assert.notEqual(
    alertSummaryTone(hot),
    alertSummaryTone(calm),
    'high_count > 0 必須與平時有視覺區別',
  )
  assert.equal(isAlertSummaryUrgent(hot), true)
  assert.equal(isAlertSummaryUrgent(calm), false)
  assert.match(alertSummaryHeadline(hot), /高嚴重度/)
  assert.match(alertSummaryHeadline(hot), /3/)
  assert.equal(alertSummaryHeadline(calm), '目前無待處理告警')
})

test('只有待處理(無高嚴重度)→ warn,仍與平時不同', () => {
  const summary = { open_count: 2, acknowledged_count: 0, resolved_count: 0, high_count: 0 }
  assert.equal(alertSummaryTone(summary), 'warn')
  assert.match(alertSummaryHeadline(summary), /2 個告警待處理/)
})

test('壞形狀 / 缺欄位的摘要不得讓 NaN 上畫面', () => {
  for (const raw of [null, undefined, {}, { high_count: 'x' }, { open_count: -3 }]) {
    const s = normalizeAlertSummary(raw)
    for (const key of Object.keys(s)) {
      assert.ok(Number.isInteger(s[key]), `${key} 不是整數:${s[key]}`)
      assert.ok(s[key] >= 0)
    }
    assert.equal(alertSummaryTone(raw), 'ok')
  }
})

// ── ⑥ 輪詢 30s + 卸載清 timer ─────────────────────────────────────────────

function fakeTimers() {
  const calls = []
  const cleared = []
  let next = 1
  return {
    calls,
    cleared,
    setTimer: (fn, ms) => {
      const handle = next++
      calls.push({ handle, fn, ms })
      return handle
    },
    clearTimer: (handle) => cleared.push(handle),
  }
}

test('輪詢間隔就是 30 秒(規格明寫)', () => {
  assert.equal(ALERT_POLL_INTERVAL_MS, 30_000)

  const timers = fakeTimers()
  const poller = createPoller(() => {}, {
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  })
  poller.start()

  assert.equal(timers.calls.length, 1)
  assert.equal(timers.calls[0].ms, 30_000)
  assert.equal(poller.intervalMs, 30_000)
})

test('stop() 真的清掉 timer —— 這條就是「換頁後還在打 API」的護欄', () => {
  const timers = fakeTimers()
  const poller = createPoller(() => {}, {
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  })

  poller.start()
  assert.equal(poller.running, true)
  poller.stop()

  assert.deepEqual(timers.cleared, [timers.calls[0].handle])
  assert.equal(poller.running, false)
})

test('重複 start 不疊 timer;重複 stop 不炸', () => {
  const timers = fakeTimers()
  const poller = createPoller(() => {}, {
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  })

  poller.start()
  poller.start()
  poller.start()
  assert.equal(timers.calls.length, 1, '重複 start 疊了多支 timer')

  poller.stop()
  poller.stop()
  assert.equal(timers.cleared.length, 1)
})

test('輪詢真的會呼叫傳進來的 task', () => {
  const timers = fakeTimers()
  let ran = 0
  const poller = createPoller(() => { ran += 1 }, {
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  })
  poller.start()
  timers.calls[0].fn()
  timers.calls[0].fn()
  assert.equal(ran, 2)
})

test('createPoller 拒絕非函式(早爆勝過每 30 秒靜默失敗)', () => {
  assert.throws(() => createPoller(null), TypeError)
  assert.throws(() => createPoller('fetchData'), TypeError)
})

// ── 原始碼層護欄:呼叫端真的掛上去了 ──────────────────────────────────────

test('首頁掛了告警摘要卡', () => {
  const source = stripComments(readSource('views/DashboardView.vue'))

  assert.ok(source.includes('AlertSummaryCard'), '首頁沒掛告警摘要卡')
  assert.ok(source.includes('getAlertSummary'), '首頁沒去打 /api/alerts/summary')
  assert.ok(source.includes('authStore.isAdmin'), '卡片必須限 admin')
})

test('告警卡對 high_count 有專屬視覺分支', () => {
  const source = stripComments(readSource('components/dashboard/AlertSummaryCard.vue'))
  assert.ok(source.includes('isAlertSummaryUrgent'), '卡片應走共用 urgent 判定')
  assert.ok(source.includes('is-urgent') || source.includes('urgent'), 'high_count 需有視覺分支')
  assert.ok(source.includes('high_count'), '卡片必須顯示 high_count')
})

test('AlertsView 有 30s 輪詢,而且元件銷毀時 stop', () => {
  const source = stripComments(readSource('views/AlertsView.vue'))

  assert.ok(source.includes('createPoller'), 'AlertsView 應用共用 poller')
  assert.ok(source.includes('ALERT_POLL_INTERVAL_MS'), '間隔應取共用常數,不散落 magic number')
  assert.ok(source.includes('onUnmounted'), 'AlertsView 缺 onUnmounted —— 換頁後會繼續打 API')
  assert.ok(source.includes('poller.stop()'), 'onUnmounted 內必須 stop poller')
  assert.ok(
    !/setInterval\s*\(/.test(source),
    'AlertsView 不得直接 setInterval —— 那樣沒有東西會抓到忘記清 timer',
  )
})

test('新增檔案不得引入裸 data.detail 插值(沿用 W2-12 的 ratchet 紀律)', () => {
  for (const relative of [
    'views/AlertsView.vue',
    'components/dashboard/AlertSummaryCard.vue',
    'utils/alertSummary.js',
    'utils/polling.js',
  ]) {
    const source = stripComments(readSource(relative))
    const hits = source.match(/response\??\.data\??\.detail/g) || []
    assert.equal(hits.length, 0, `${relative} 有裸 data.detail 插值`)
  }
})
