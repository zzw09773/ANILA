/** 未讀使用者回饋的頁首通知。筆數來自警報摘要的同一次回應。 */

/**
 * @param {object|null|undefined} summary
 * @returns {{count:number,text:string,href:string}|null}
 */
export function unreadFeedbackBannerModel(summary) {
  const raw = Number(summary?.unread_feedback_count)
  const count = Number.isFinite(raw) && raw > 0 ? Math.trunc(raw) : 0
  if (count <= 0) return null
  return {
    count,
    text: `有 ${count} 筆新的使用者回饋`,
    href: '/feedback',
  }
}

let current = null
const listeners = new Set()

export function publishUnreadFeedback(summary) {
  current = summary ?? null
  for (const listener of [...listeners]) {
    try {
      listener(current)
    } catch {
      // 一個訂閱失敗不擋其他頁。
    }
  }
}

export function subscribeUnreadFeedback(listener) {
  listeners.add(listener)
  listener(current)
  return () => listeners.delete(listener)
}
