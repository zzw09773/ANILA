import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { costCell, formatMonthUsage, moneyToMicros, summaryCostLabel } from '../src/utils/pricingDisplay.js'
import { exportQuery } from '../src/utils/usageExportRange.js'
import { deputyMayOpen } from '../src/router/deputyPages.js'

function read(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

test('未計價不顯示成 0，混合時同時保留金額', () => {
  assert.equal(summaryCostLabel({ cost_state: 'unpriced', cost: null }), null)
  assert.equal(summaryCostLabel({ cost: '0', cost_state: 'unpriced' }), null)
  assert.deepEqual(summaryCostLabel({ cost: '7', cost_state: 'priced', cost_currency: 'TWD' }), {
    text: '7 TWD',
    note: '',
  })
  assert.equal(summaryCostLabel({ cost: '3', cost_state: 'mixed', cost_currency: 'TWD' }).note, '部分未計價')
  assert.equal(costCell({ cost_state: 'unpriced', cost: null }), '未計價')
  assert.equal(costCell({ cost: '3', cost_state: 'mixed', cost_currency: 'TWD' }), '3 TWD · 部分未計價')
  assert.equal(formatMonthUsage({ month_tokens: 12, month_cost_state: 'unpriced' }), '12 · 未計價')
  assert.equal(formatMonthUsage({ month_tokens: 0 }), '0 · 未計價')
})

test('金額換成 micros，空值不是 0', () => {
  assert.equal(moneyToMicros(''), null)
  assert.equal(moneyToMicros('1.5'), 1_500_000)
  assert.throws(() => moneyToMicros('-1'), /不可為負/)
})

test('匯出區間最長一年，快捷與日期不能同時用', () => {
  assert.equal(exportQuery({ start: '2024-01-01', end: '2025-01-01' }).error, '單次匯出最長一年')
  assert.deepEqual(exportQuery({ start: '2024-01-01', end: '2024-12-31' }).params, {
    start: '2024-01-01',
    end: '2024-12-31',
  })
  assert.equal(exportQuery({ start: '2024-01-01', end: '2025-01-02' }).error, '單次匯出最長一年')
  assert.equal(exportQuery({ preset: 'this_month', start: '2024-01-01' }).error, '快捷鍵與自訂日期請擇一')
  assert.equal(exportQuery({ start: '2024-02-02', end: '2024-02-01' }).error, '結束日期不可早於開始日期')
  assert.equal(exportQuery({ preset: 'last_month' }).params.preset, 'last_month')
  assert.equal(exportQuery({ range: '7d' }).params.range, '7d')
})

test('模型頁有單價歷史，額度頁給管理員改、單位管理員只讀，代理管理員進不去', () => {
  const models = read('views/ModelsView.vue')
  assert.match(models, /ModelPriceEditor/)
  assert.match(models, /v-if="editingId && authStore\.isAdmin"/)
  const editor = read('components/ModelPriceEditor.vue')
  assert.match(editor, /data-testid="model-price-editor"/)
  assert.match(editor, /未計價/)
  const quotas = read('views/QuotasView.vue')
  assert.match(quotas, /data-testid="quota-readonly"/)
  assert.match(quotas, /單位管理員只能查看額度，不能修改/)
  assert.match(quotas, /authStore\.isAdmin/)
  assert.match(quotas, /最多超出同時在途的呼叫數/)
  assert.match(quotas, /這個對象還有模型未完整計價，不能用金額額度/)
  assert.match(quotas, /unpriced_message/)
  assert.match(editor, /貨幣已鎖定/)
  const sidebar = read('components/layout/AppSidebar.vue')
  const deputyStart = sidebar.indexOf('isDeputy && !authStore.isAdmin')
  const deputyEnd = sidebar.indexOf('const groups', deputyStart)
  assert.equal(sidebar.slice(deputyStart, deputyEnd).includes("'/quotas'"), false)
  assert.match(sidebar, /path: '\/quotas', label: '額度'/)
  const router = read('router/index.js')
  assert.ok(router.indexOf('requiresQuotaRead') < router.indexOf('requiresAdmin && !authStore.isAdmin'))
  assert.equal(deputyMayOpen('/quotas', { isDeputy: true, isAdmin: false }), false)
  const usage = read('views/UsageView.vue')
  assert.match(usage, /summaryCostLabel/)
  assert.match(usage, /showApiKeyCost/)
  assert.match(usage, /showUnitCost/)
  assert.match(usage, /cost_state/)
  assert.match(usage, /依單位（含下層）/)
  assert.match(usage, /列與列不能相加/)
  assert.match(usage, /v-if="!authStore\.isDeputy" title="被擋下的呼叫"/)
  assert.match(usage, /EXPORT_PRESETS/)
  assert.match(read('utils/usageExportRange.js'), /本月/)
  assert.match(read('utils/pricingDisplay.js'), /部分未計價/)
  const keys = read('views/ApiKeysView.vue')
  assert.match(keys, /本月用量/)
  assert.match(keys, /colspan="8"/)
  assert.match(keys, /formatMonthUsage/)
})
