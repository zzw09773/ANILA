// 統一把後端錯誤收成可顯示字串。
//
// 本樹沒有 W2-12 錯誤信封(`error.message`);治理 UI 既有慣例是讀
// `e.response?.data?.detail`。detail 可能是字串、物件或 422 陣列 —— 裸插值
// 會變成 `[object Object]`。這個 helper 只處理 legacy detail 三種形狀,
// 不假裝信封契約存在。
//
// ⚠ 只讀不變式（R2-3）：`err?.response?.data?.detail` 這個讀法，**只准住在
// 這一個模組**（連同 `utils/loginSurface.js` 的登入專用取得器與
// `utils/settingsView.js` 的設定專用取得器）。其餘 `src/**` 的任何 .vue/.js
// 出現它,就代表有人繞過收斂器直接碰後端錯誤——`tests/bareDetailRatchet.test.mjs`
// 照這個形狀窮舉,那三支之外一律算違規。
//
// 目前的 ratchet 只認 `response.data.detail` 這一種拼法（含 optional-chaining
// 的 `?.` 變體）。解構形（`const { detail } = err.response.data`）、別名變數、
// `payload?.detail` 等**不會被認到**——這份「只認一種拼法」的樣式清單
// **不會自己長大**，未來若採納別種拼法，要在 ratchet 的 regex 裡加、不是只加到這一行。
//
// ⚠ 另一格它看不見的（LOW-2）：**經 `getRawDetail` 取到同一個值的呼叫端，
// ratchet 一樣失明**——那是收斂器（設計如此），「誰還該擁有一次形狀存取」
// 另由 `tests/extractErrorShapes.test.mjs` 那條護欄守，不是這份 regex 守。
// 這份規則自己不會長大；要它看得見更多，就去加 regex、加護欄，不要只在這裡補字。

/**
 * @param {unknown} err
 * @param {string} fallback
 * @returns {string}
 */
export function extractError(err, fallback = '操作失敗') {
  const detail = getRawDetail(err)
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

/**
 * 取得最常見的後端錯誤欄位 `detail`，但**承接它形狀不明**，原物未改。
 * 供需要把 detail 的**外在形狀**告訴呼叫端的收斂器使用（例：`extractDetail`
 * 要處理 422 陣列；登入要區分字串與信封物件）。回 `undefined` 表示沒有。
 * 不在此處轉型或 trim,以免把原始值改寫。
 * @param {unknown} err
 */
export function getRawDetail(err) {
  return err?.response?.data?.detail
}

