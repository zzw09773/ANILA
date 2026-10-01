/** 成本顯示。未計價不顯示 0。 */

export function summaryCostLabel(summary) {
  if (!summary) return null
  const state = summary.cost_state || 'unpriced'
  if (state === 'unpriced' || summary.cost == null) return null
  const currency = summary.cost_currency ? ` ${summary.cost_currency}` : ''
  return {
    text: `${summary.cost}${currency}`,
    note: state === 'mixed' ? '部分未計價' : '',
  }
}

export function costCell(row) {
  if (!row || row.cost_state === 'unpriced' || row.cost == null) return '未計價'
  const currency = row.cost_currency ? ` ${row.cost_currency}` : ''
  if (row.cost_state === 'mixed') return `${row.cost}${currency} · 部分未計價`
  return `${row.cost}${currency}`
}

export function formatMonthUsage(row) {
  const tokens = Number(row?.month_tokens || 0).toLocaleString('zh-TW')
  if (!row || row.month_cost_state === 'unpriced' || row.month_cost == null) {
    return `${tokens} · 未計價`
  }
  const currency = row.month_cost_currency ? ` ${row.month_cost_currency}` : ''
  const mixed = row.month_cost_state === 'mixed' ? ' · 部分未計價' : ''
  return `${tokens} · ${row.month_cost}${currency}${mixed}`
}

/** 畫面上的金額改成 micros。空值是不限，不是 0。 */
export function moneyToMicros(value) {
  if (value == null || String(value).trim() === '') return null
  const text = String(value).trim()
  if (!/^\d+(\.\d{1,6})?$/.test(text)) {
    throw new Error('金額最多六位小數，且不可為負')
  }
  const [whole, frac = ''] = text.split('.')
  return Number(whole) * 1_000_000 + Number((frac + '000000').slice(0, 6))
}
