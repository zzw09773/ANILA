/**
 * 「等待核准」畫面上那一行「去問誰」。
 *
 * 為什麼需要這個模組
 * ------------------
 * 首次刷卡登入的人只會看到「一旦管理員核准，下次刷卡即可登入」——沒有名字、
 * 沒有信箱、沒有窗口。上線那一週這是全院最多人看到的一頁，而看到它的人正好
 * 是還沒有帳號、無處可問的那一群。
 *
 * 做法上刻意不新增設定機制、不新增角色、不新增資料表：直接讀既有的公告橫幅
 * （管理員在治理中心維護）。有公告就顯示公告，沒有就給一句誠實的保底說法。
 *
 * 讀的是 `GET /api/banners/public`，不是 `/active`。`/active` 要 token，而這
 * 一頁的讀者**還沒有帳號** —— 那支端點對他們永遠回 401，擁有者貼了公告也只有
 * 已經進得來的人看得到。`/public` 只回管理員逐則勾過「登入頁公開」的那幾則，
 * 欄位只有 `{ level, content }`。
 *
 * ⚠ 這個 repo 是公開的：保底文案不得寫入任何真人姓名、信箱或內網位址。
 *
 * 純函式，無 Vue／DOM／axios 依賴。
 */

/** 沒有任何有效公告時的保底說法。刻意不指名任何人。 */
export const APPROVAL_CONTACT_FALLBACK =
  '等候期間如需查詢申請進度，請洽貴單位窗口或平台管理員。'

/**
 * 從 `/api/banners/public` 的回應挑出要顯示的那一則。
 *
 * 後端已依 `sort_order, id` 排序，所以取第一則有內容的即可。任何取不到／
 * 取到空陣列／取到壞資料的情況都退回保底文案，不會讓畫面空白。
 *
 * `is_active` 的檢查留著：`/public` 已經在後端濾掉停用的公告（欄位根本不送
 * 出來，`undefined !== false` 照樣通過），但這是純函式，餵什麼進來都得有個
 * 合理的答案。
 *
 * @param {Array<{content?: string, level?: string, is_active?: boolean}>|null|undefined} banners
 * @returns {{source: 'banner'|'fallback', text: string, level: string}}
 */
export function approvalContactNotice(banners) {
  const rows = Array.isArray(banners) ? banners : []
  const active = rows.find(
    (b) =>
      b &&
      b.is_active !== false &&
      typeof b.content === 'string' &&
      b.content.trim() !== '',
  )
  if (!active) {
    return { source: 'fallback', text: APPROVAL_CONTACT_FALLBACK, level: 'info' }
  }
  return {
    source: 'banner',
    text: active.content.trim(),
    level: typeof active.level === 'string' && active.level ? active.level : 'info',
  }
}
