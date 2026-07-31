/**
 * 告警摘要的純函式層（W3-3④）。
 *
 * 為什麼需要這一支
 * ----------------
 * `/api/alerts/summary` **早就有 `high_count`**，但沒有人把它擺在首頁 ——
 * 管理者要看到「現在有幾個高嚴重度告警」得先自己想到去點警報頁。
 * air-gapped 內網沒有 mail relay（有沒有是組織事實，屬決策件），所以首版的
 * 「通知」只能是：首頁一張卡 + 警報頁輪詢。這支模組是那張卡的邏輯本體。
 */

/** 後端還沒回來、或回了壞形狀時的安全底線。 */
export const EMPTY_ALERT_SUMMARY = Object.freeze({
  open_count: 0,
  acknowledged_count: 0,
  resolved_count: 0,
  high_count: 0,
})

function count(raw, key) {
  const n = Number(raw?.[key])
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0
}

/**
 * 正規化後端摘要。缺欄位／非數字一律 0，不讓 NaN 進到畫面上。
 * @param {object|null|undefined} raw
 */
export function normalizeAlertSummary(raw) {
  return {
    open_count: count(raw, 'open_count'),
    acknowledged_count: count(raw, 'acknowledged_count'),
    resolved_count: count(raw, 'resolved_count'),
    high_count: count(raw, 'high_count'),
  }
}

/** 高嚴重度（high / critical 且未解決）> 0 = 要人現在看。 */
export function isAlertSummaryUrgent(raw) {
  return normalizeAlertSummary(raw).high_count > 0
}

/**
 * 卡片 tone：`high_count > 0` → danger（**視覺上必須與平時不同**，這是驗收⑤）；
 * 只有待處理 → warn；全清空 → ok。
 */
export function alertSummaryTone(raw) {
  const s = normalizeAlertSummary(raw)
  if (s.high_count > 0) return 'danger'
  if (s.open_count > 0) return 'warn'
  return 'ok'
}

/** 一行繁中結論，講「要不要動作」而不是只報數字。 */
export function alertSummaryHeadline(raw) {
  const s = normalizeAlertSummary(raw)
  if (s.high_count > 0) {
    return `${s.high_count} 個高嚴重度告警未解決 — 請立即處理`
  }
  if (s.open_count > 0) {
    return `${s.open_count} 個告警待處理`
  }
  if (s.acknowledged_count > 0) {
    return `${s.acknowledged_count} 個告警已確認、尚未解決`
  }
  return '目前無待處理告警'
}
