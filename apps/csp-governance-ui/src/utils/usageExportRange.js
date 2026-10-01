/** 用量匯出的快捷與自訂日期。最長一年，結束日含當天。 */

export const EXPORT_PRESETS = [
  { value: 'this_month', label: '本月' },
  { value: 'last_month', label: '上月' },
  { value: 'this_quarter', label: '本季' },
]

function plusOneYear(iso) {
  const [year, month, day] = iso.split('-').map(Number)
  const next = new Date(Date.UTC(year + 1, month - 1, day))
  if (next.getUTCMonth() !== month - 1) {
    return `${year + 1}-${String(month).padStart(2, '0')}-28`
  }
  const mm = String(next.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(next.getUTCDate()).padStart(2, '0')
  return `${next.getUTCFullYear()}-${mm}-${dd}`
}

function nextDay(iso) {
  const [year, month, day] = iso.split('-').map(Number)
  const next = new Date(Date.UTC(year, month - 1, day + 1))
  const mm = String(next.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(next.getUTCDate()).padStart(2, '0')
  return `${next.getUTCFullYear()}-${mm}-${dd}`
}

export function exportQuery({ preset, start, end, range } = {}) {
  const chosen = preset || ''
  const from = start || ''
  const to = end || ''
  if (chosen && (from || to)) {
    return { error: '快捷鍵與自訂日期請擇一' }
  }
  if (from || to) {
    if (!from || !to) return { error: '請同時提供開始與結束日期' }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from) || !/^\d{4}-\d{2}-\d{2}$/.test(to)) {
      return { error: '日期格式須為 YYYY-MM-DD' }
    }
    if (to < from) return { error: '結束日期不可早於開始日期' }
    if (nextDay(to) > plusOneYear(from)) return { error: '單次匯出最長一年' }
    return { params: { start: from, end: to } }
  }
  if (chosen) return { params: { preset: chosen } }
  return { params: { range: range || '24h' } }
}
