// 清單「讀不到」與「本來就是空的」必須是兩個不同的畫面狀態。
//
// 為什麼需要這個 helper
// --------------------
// UsageView 的 Phase G rollup、UsersView 的部門／模型／Agent 目錄，
// 以前都是 try/catch 清空：讀取失敗時停在 [],畫面跟「沒有資料」一模一樣。
// 管理員會照空清單的文案操作（部門只剩「— 無 —」、模型寫「尚未註冊」），
// 一次網路抖動就被當成真實空狀態。
//
// 與 allowList.js 同形狀，但語意更通用：空清單仍是一次成功讀取
//（可以儲存「清空」）；只有 failed 不得拿這份資料去覆寫後端。

/** 讀到了，而且有內容。 */
export const LOAD_READY = 'ready'
/** 讀到了，確定是空的。 */
export const LOAD_EMPTY = 'empty'
/** 沒讀到 —— 不知道是空的還是有東西。**不得**當成空清單。 */
export const LOAD_FAILED = 'failed'

/**
 * 執行一次清單讀取，把失敗保留成明確狀態，而不是靜默的空陣列。
 *
 * @param {() => Promise<{ data: unknown }>} fetcher 實際打 API 的函式
 * @returns {Promise<{ items: any[], loadFailed: boolean, error: unknown }>}
 */
export async function loadList(fetcher) {
  try {
    const { data } = await fetcher()
    return { items: Array.isArray(data) ? data : [], loadFailed: false, error: null }
  } catch (error) {
    return { items: [], loadFailed: true, error }
  }
}

/**
 * @param {{ items?: any[], loadFailed?: boolean }} result loadList() 的回傳
 * @returns {'ready'|'empty'|'failed'}
 */
export function loadStatus(result) {
  if (result?.loadFailed) return LOAD_FAILED
  return (result?.items || []).length ? LOAD_READY : LOAD_EMPTY
}

/**
 * 對應狀態的說明文字。failed 一定要說「讀不到」，而且不能沿用空清單的字。
 *
 * @param {'ready'|'empty'|'failed'} status
 * @param {{ empty: string, failed: string }} copy
 * @returns {string}
 */
export function loadNotice(status, copy) {
  if (status === LOAD_FAILED) return copy.failed
  if (status === LOAD_EMPTY) return copy.empty
  return ''
}

/**
 * 清單已成功讀到（含確定是空的）。失敗時不得拿這份資料去覆寫後端。
 *
 * @param {'ready'|'empty'|'failed'} status
 * @returns {boolean}
 */
export function loadSucceeded(status) {
  return status === LOAD_READY || status === LOAD_EMPTY
}
