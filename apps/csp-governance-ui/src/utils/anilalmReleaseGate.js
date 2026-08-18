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
  return link.release_gate_code === 'anila_lm'
}

/**
 * **使用者面**清單用（儀表板的「平台 · 外部工具」卡）：閘門關閉時濾掉 ANILA LM。
 *
 * ⚠ 管理面（服務登記、服務存取）**不可以**套這個。
 * 2026-08-02 踩過：服務登記表在前端濾掉整列，連編輯／停用／刪除按鈕一起消失，
 * 管理員看不到也管不動，要停用只能手打 API。閘門關的是「可用」，不是「可管理」。
 * 管理面請改用 isReleaseGateClosedFor() 把那一列**標出來**，不要拿掉。
 */
export function filterPlatformLinksForRelease(links) {
  const list = Array.isArray(links) ? links : []
  if (ANILA_LM_LINK_VISIBLE) return list
  return list.filter((link) => !isAnilaLmPlatformLink(link))
}

/** 管理面用：這一列現在是不是被 release gate 關著（關著照樣顯示，只是要標）。 */
export function isReleaseGateClosedFor(link) {
  if (ANILA_LM_LINK_VISIBLE) return false
  return isAnilaLmPlatformLink(link)
}

/** 管理面的標籤與提示（說話對象是管理員，不是一般使用者）。 */
export const RELEASE_GATE_BADGE = '未開放'
export const RELEASE_GATE_HINT =
  '本 release 閘門關閉：使用者端看不到、也不能啟動此服務。' +
  '此列仍可編輯／停用／刪除。重開見 docs/runbooks/anilalm-release-gate.md'
