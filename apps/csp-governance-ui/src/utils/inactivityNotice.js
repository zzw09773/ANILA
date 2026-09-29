/** 閒置停用試算／執行結果。筆數來自警報摘要的同一次回應。 */

/**
 * @param {object|null|undefined} summary
 * @returns {{phase:string,count:number,days:number,text:string,href:string}|null}
 */
export function inactivityNoticeModel(summary) {
  const notice = summary?.inactivity_notice
  if (!notice || typeof notice !== 'object') return null
  const count = Number(notice.count)
  const days = Number(notice.days)
  if (!Number.isFinite(count) || count <= 0) return null
  if (!Number.isFinite(days) || days <= 0) return null
  const n = Math.trunc(count)
  const d = Math.trunc(days)
  const phase = notice.phase === 'applied' ? 'applied' : 'preview'
  const text = phase === 'applied'
    ? `最近一次排程已停用 ${n} 個超過 ${d} 天未登入的帳號`
    : `試算：有 ${n} 個帳號超過 ${d} 天未登入，下一次每日排程才會停用`
  return { phase, count: n, days: d, text, href: '/users' }
}

let current = null
const listeners = new Set()

export function publishInactivityNotice(summary) {
  current = summary ?? null
  for (const listener of [...listeners]) {
    try {
      listener(current)
    } catch {
      // 一個訂閱失敗不擋其他頁。
    }
  }
}

export function subscribeInactivityNotice(listener) {
  listeners.add(listener)
  listener(current)
  return () => listeners.delete(listener)
}
