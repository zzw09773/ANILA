// 允許清單「讀不到」與「本來就是空的」必須是兩個不同的畫面狀態。
//
// 為什麼需要這個 helper
// --------------------
// 本週最嚴重的缺陷是 UsersView 的 `try { ... } catch {}`:讀不到某人的模型
// 允許清單時,catch 吞掉錯誤、清單停在 `[]`,畫面跟「這個人本來就沒有模型」
// 完全一樣。管理員照常操作,一次網路抖動就靜默撤光一個人的權限。
//
// ApiKeysView 有同一形狀的 catch(讀 /users/me/allowed-models 與 /models),
// 失敗時落到「允許清單中沒有模型 · 請聯絡管理員」——使用者會去找管理員,
// 管理員看自己的畫面一切正常。因此把「讀取結果 → 畫面狀態」抽成純函式,
// 讓失敗有自己的狀態值、自己的訊息,而且不能被當成空清單。

/** 讀到了,而且有內容。 */
export const ALLOW_LIST_READY = 'ready'
/** 讀到了,確定是空的(真的沒有被指派)。 */
export const ALLOW_LIST_EMPTY = 'empty'
/** 沒讀到 —— 不知道是空的還是有東西。**不得**當成空清單。 */
export const ALLOW_LIST_UNREAD = 'unread'

/**
 * 執行一次允許清單讀取,把失敗保留成一個明確的狀態,而不是靜默的空陣列。
 *
 * @param {() => Promise<{ data: unknown }>} fetcher 實際打 API 的函式
 * @returns {Promise<{ items: any[], loadFailed: boolean, error: unknown }>}
 */
export async function loadAllowList(fetcher) {
  try {
    const { data } = await fetcher()
    return { items: Array.isArray(data) ? data : [], loadFailed: false, error: null }
  } catch (error) {
    // 失敗時 items 一定是空的,但 loadFailed 必須把「這是空的」和
    // 「我不知道」分開 —— 呼叫端只准看 allowListStatus() 的結果。
    return { items: [], loadFailed: true, error }
  }
}

/**
 * @param {{ items?: any[], loadFailed?: boolean }} result loadAllowList() 的回傳
 * @returns {'ready'|'empty'|'unread'}
 */
export function allowListStatus(result) {
  if (result?.loadFailed) return ALLOW_LIST_UNREAD
  return (result?.items || []).length ? ALLOW_LIST_READY : ALLOW_LIST_EMPTY
}

/**
 * 對應狀態的說明文字。`unread` 一定要說「讀不到」,而且不能沿用空清單的字。
 *
 * @param {'ready'|'empty'|'unread'} status
 * @param {{ empty: string, unread: string }} copy
 * @returns {string}
 */
export function allowListNotice(status, copy) {
  if (status === ALLOW_LIST_UNREAD) return copy.unread
  if (status === ALLOW_LIST_EMPTY) return copy.empty
  return ''
}

/**
 * 只有真的讀到、而且有內容,才可以送出以允許清單為內容的表單。
 * 讀不到就送出 = 拿一份不知道對不對的清單去覆寫後端。
 *
 * @param {'ready'|'empty'|'unread'} status
 * @returns {boolean}
 */
export function allowListUsable(status) {
  return status === ALLOW_LIST_READY
}
