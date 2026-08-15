// formatDate 共用工具的單元測試。
//
// 背景：治理中心過去有 12 份各自複製的 `formatDate`，每份時區／24h／空值
// 語意都不同。這支測試釘住統一後的契約。風格對齊 `apiTimestampTaipei.test.mjs`：
// 用 `assert.match` 鎖時間片段，不硬比完整字串（輸出含 U+202F 窄空白）。

import test from 'node:test'
import assert from 'node:assert/strict'

import {
  formatDate,
  DATE_EMPTY_PLACEHOLDER,
} from '../src/utils/formatDate.js'

test('兩筆不同時間，各自顯示自己的台北時分', () => {
  const a = formatDate('2026-08-15T06:30:00.000000Z') // UTC 06:30 = 台北 14:30
  const b = formatDate('2026-08-15T07:45:00.000000Z') // UTC 07:45 = 台北 15:45
  assert.match(a, /14:30:00/)
  assert.match(b, /15:45:00/)
})

test('24h 邊界：18:30 不含「下午」', () => {
  const rendered = formatDate('2026-08-15T10:30:00.000000Z') // 台北 18:30
  assert.match(rendered, /18:30:00/)
  assert.doesNotMatch(rendered, /下午/)
})

test('時區斷言：UTC ISO 時戳 → 台北時間（+8）', () => {
  const rendered = formatDate('2026-07-30T08:00:00.000000+00:00')
  assert.match(rendered, /16:00:00/)
})

test('空值 → 佔位符「—」', () => {
  assert.equal(formatDate(null), DATE_EMPTY_PLACEHOLDER)
  assert.equal(formatDate(undefined), DATE_EMPTY_PLACEHOLDER)
  assert.equal(formatDate(''), DATE_EMPTY_PLACEHOLDER)
})

test('非空但解析失敗 → 原文照回（不吞資料）', () => {
  assert.equal(formatDate('not-a-date'), 'not-a-date')
})
