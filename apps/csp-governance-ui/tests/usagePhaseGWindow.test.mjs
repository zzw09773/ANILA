// R2-2：Phase G 兩個面板（Agent／基礎模型）的標題窗必須等於資料窗。
//
// 那兩支端點（top-agents／by-base-model）吃 days 不是 range 參數，
// 4h/12h/24h 都會被夾成 24 小時——標題不得繼續寫「4h」。誠實標明最小窗 24h。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

test('Agent／基礎模型兩面板標題用 phaseGRangeLabel，不是 rangeLabel', () => {
  const source = readSource('views/UsageView.vue')
  // 兩處標題必須各自走 phaseGRangeLabel（窗被夾成 24h 的那種）。
  assert.ok(
    source.includes('`熱門 · Agent · ${phaseGRangeLabel}`'),
    'Agent 面板標題應走 phaseGRangeLabel',
  )
  assert.ok(
    source.includes('`依 · 基礎模型 · ${phaseGRangeLabel}`'),
    '基礎模型面板標題應走 phaseGRangeLabel',
  )
})

test('代理管理員看不到用量 CSV 匯出，管理員仍看得到', () => {
  const source = readSource('views/UsageView.vue')
  const buttonAt = source.indexOf('label="匯出 CSV"')
  assert.ok(buttonAt > 0, '用量頁要留著匯出 CSV')
  const button = source.slice(Math.max(0, buttonAt - 220), buttonAt)
  assert.match(button, /v-if="!authStore\.isDeputy"/)
})

test('代理管理員看部門與 Agent 彙總，看不到每人用量', () => {
  const source = readSource('views/UsageView.vue')
  const usersAt = source.indexOf('熱門 · 使用者')
  const usersBox = source.slice(Math.max(0, usersAt - 160), usersAt)
  assert.match(usersBox, /v-if="authStore\.isAdmin"/)
  assert.doesNotMatch(usersBox, /isDeputy/)
  const deptAt = source.indexOf('熱門 · 部門')
  const deptBox = source.slice(Math.max(0, deptAt - 180), deptAt)
  assert.match(deptBox, /isDeputy/)
  const agentAt = source.indexOf('熱門 · Agent')
  const agentBox = source.slice(Math.max(0, agentAt - 180), agentAt)
  assert.match(agentBox, /isDeputy/)
})

test('phaseGRangeLabel 把 sub-day 視窗誠實標成 24h', () => {
  const source = readSource('views/UsageView.vue')
  // 4h/12h/24h → 24h；7d/30d → 原樣。
  assert.ok(source.includes("'4h': '24h'"), '4h 應標成 24h')
  assert.ok(source.includes("'12h': '24h'"), '12h 應標成 24h')
  assert.ok(source.includes("'7d': '7d'"), '7d 應原樣')
})
