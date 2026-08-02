/**
 * ANILA LM 暫時關閉閘門（本 release 給高層預覽用）。
 *
 * 重開：把 ANILA_LM_LINK_VISIBLE 改成 true，並依
 * docs/runbooks/anilalm-release-gate.md 一併打開 nginx / shell。
 * 治理中心採「不顯示」而非停用列 — 管理員不該看到一扇關著的門。
 */
export const ANILA_LM_LINK_VISIBLE = false

/** 辨識平台連結／服務登記裡的 ANILA LM 入口。 */
export function isAnilaLmPlatformLink(link) {
  if (!link || typeof link !== 'object') return false
  const name = String(link.name || '').trim().toLowerCase()
  const url = String(link.url || link.entry_url || '').trim().toLowerCase()
  if (name === 'anila lm' || name === 'anilalm') return true
  // /anilalm、/anilalm/、含 query 的深連
  if (/(?:^|\/)anilalm(?:\/|$|\?)/i.test(url)) return true
  return false
}

/** 儀表板／服務列表用：閘門關閉時濾掉 ANILA LM。 */
export function filterPlatformLinksForRelease(links) {
  const list = Array.isArray(links) ? links : []
  if (ANILA_LM_LINK_VISIBLE) return list
  return list.filter((link) => !isAnilaLmPlatformLink(link))
}
