/** 未處理警報的頁首橫幅。確認或解決後 open_count 歸零，橫幅消失。 */

export const OPEN_ALERT_BANNER_POLL_MS = 60_000

const SEVERITY_LABEL = {
  critical: '嚴重',
  high: '高',
  medium: '中',
  low: '低',
}

/**
 * @param {object|null|undefined} summary
 * @returns {{count:number,severity:string,text:string,href:string,linkLabel:string}|null}
 */
export function openAlertBannerModel(summary) {
  const open = Number(summary?.open_count)
  const count = Number.isFinite(open) && open > 0 ? Math.trunc(open) : 0
  if (count <= 0) return null
  const severity = typeof summary?.highest_open_severity === 'string'
    ? summary.highest_open_severity
    : ''
  const label = SEVERITY_LABEL[severity] || ''
  const text = label
    ? `有 ${count} 筆未處理警報，最高嚴重度是${label}。`
    : `有 ${count} 筆未處理警報。`
  return {
    count,
    severity,
    text,
    href: '/alerts',
    linkLabel: '查看警報',
  }
}

const listeners = new Set()

export function subscribeOpenAlertBanner(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function refreshOpenAlertBanner() {
  for (const listener of [...listeners]) {
    try {
      listener()
    } catch {
      // 一個訂閱失敗不擋其他頁。
    }
  }
}
