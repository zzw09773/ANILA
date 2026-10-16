import { onIdentityInvalidated } from '../api/identityEpoch.js'

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

// 此狀態只決定本機摘要是否仍可展示，不代替 /me 或後端授權。
let generation = 0
let actor = null
let active = true
let observing = false
const resets = new Set()

function resetActor(nextActor) {
  actor = nextActor
  active = nextActor !== null
  generation += 1
  for (const listener of [...resets]) {
    try { listener(active) } catch { /* 一個訂閱失敗不擋其他頁。 */ }
  }
}

function observeIdentity() {
  if (observing || typeof window === 'undefined') return
  observing = true
  window.addEventListener('anila:identity', (event) => {
    const id = event.detail?.userId
    const next = typeof id === 'string' || typeof id === 'number' ? String(id) : null
    if (next !== actor || next === null) resetActor(next)
  })
  window.addEventListener('pagehide', () => resetActor(null))
  onIdentityInvalidated(() => resetActor(null))
}

export function bannerIdentityToken() {
  observeIdentity()
  return { generation, active }
}

export function bannerIdentityCurrent(token) {
  return active && token?.active === true && token.generation === generation
}

export function onBannerSummaryReset(listener) {
  observeIdentity()
  resets.add(listener)
  return () => resets.delete(listener)
}

export function clearBannerSummaries() {
  resetActor(null)
}

let current = null
const listeners = new Set()

function deliver(summary) {
  current = summary
  for (const listener of [...listeners]) {
    try { listener(current) } catch { /* 一個訂閱失敗不擋其他頁。 */ }
  }
}

onBannerSummaryReset(() => deliver(null))

export function publishUnreadFeedback(summary, token = bannerIdentityToken()) {
  if (!bannerIdentityCurrent(token)) return
  // 不保留與此橫幅無關的完整摘要。
  const model = unreadFeedbackBannerModel(summary)
  deliver(model ? { unread_feedback_count: model.count } : null)
}

export function subscribeUnreadFeedback(listener) {
  observeIdentity()
  listeners.add(listener)
  listener(current)
  return () => listeners.delete(listener)
}
