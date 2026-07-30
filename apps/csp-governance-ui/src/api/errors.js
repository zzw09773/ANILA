// 統一把後端錯誤收成可顯示字串。
//
// 本樹沒有 W2-12 錯誤信封(`error.message`);治理 UI 既有慣例是讀
// `e.response?.data?.detail`。detail 可能是字串、物件或 422 陣列 —— 裸插值
// 會變成 `[object Object]`。這個 helper 只處理 legacy detail 三種形狀,
// 不假裝信封契約存在。

/**
 * @param {unknown} err
 * @param {string} fallback
 * @returns {string}
 */
export function extractError(err, fallback = '操作失敗') {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && typeof item.msg === 'string') {
          return item.msg
        }
        return ''
      })
      .filter(Boolean)
    if (parts.length) return parts.join('; ')
  }
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string' && detail.message.trim()) {
      return detail.message
    }
    if (typeof detail.detail === 'string' && detail.detail.trim()) {
      return detail.detail
    }
  }
  if (typeof err?.message === 'string' && err.message && !err.response) {
    return err.message
  }
  return fallback
}
