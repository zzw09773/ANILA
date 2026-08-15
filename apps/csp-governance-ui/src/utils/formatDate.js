// 日期時間顯示的單一來源。
//
// 為什麼需要這一支
// ----------------
// `formatDate` 一度在 12 個 view 裡各自複製，每一份的時區、24/12 小時制、
// 空值處理都不同步：有的顯示 UTC（差 8 小時）、有的隨瀏覽器環境漂移、
// 有的 12 小時制。格式會漂移，正是因為「同一個概念、12 份自訂義」。
// 這裡統一成一份，讓 locale 字面量只存在於此處。

/** locale 常數：圖表軸等有自己密度需求的格式，也要從這裡共用同一組。 */
export const DATE_LOCALE = 'zh-TW'
export const DATE_TIME_ZONE = 'Asia/Taipei'
export const DATE_HOUR_CYCLE = 'h23'

/**
 * 已知例外：`utils/healthOverview.js` 的 `formatCheckedAt()`。
 * 它是 ISO 運維格式（`YYYY-MM-DD HH:mm:ss`）＋手動 pad，且刻意靠「內網
 * 瀏覽器時區＝台北」這個環境事實，不走 toLocaleString——刻意不併入這裡。
 */

/** 空值（null/undefined/空字串）的顯示佔位。 */
export const DATE_EMPTY_PLACEHOLDER = '—'

/**
 * 把 API 時間字串格式化成「台北 24 小時制」的顯示字串。
 *
 * 兩段語意：
 *   - null / undefined / 空字串 → '—'（與多數畫面一致）
 *   - 非空但解析失敗 → 回原文（顯示錯資料的原樣，好過用 '—' 把資料吞掉）
 *
 * @param {string|null|undefined} value ISO 時間字串
 * @returns {string}
 */
export function formatDate(value) {
  if (value == null || value === '') return DATE_EMPTY_PLACEHOLDER
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString(DATE_LOCALE, {
    timeZone: DATE_TIME_ZONE,
    hourCycle: DATE_HOUR_CYCLE,
  })
}
