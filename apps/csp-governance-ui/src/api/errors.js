// 統一的錯誤解讀 helper —— W2-12(補救計畫 Wave 2)。
//
// 為什麼需要這個檔
// ----------------
// 治理後台有 102 處(22 檔)直接寫 `e.response?.data?.detail || '失敗'`,把
// 後端的 `detail` 當字串插進訊息。但 CSP 的 `detail` **不保證是字串**:
//
//   - `HTTPException(detail="字串")`  → 字串         ✔ 這 102 處只對這種有效
//   - `HTTPException(detail={...})`   → 物件         ✘ 渲染成 `[object Object]`
//   - `RequestValidationError`(422)  → array        ✘ 渲染成 `[object Object]`
//
// 使用者看到 `[object Object]` 等於什麼都沒看到,而這是**管理員唯一的入口**。
//
// W2-12 在後端加了統一信封 `{ error: { code, message, details, request_id },
// detail: <legacy> }`。這個 helper 是前端的對應面:
//
//   1. 優先讀 `error.message`(新契約)。
//   2. 讀不到才退回 legacy `detail`,並且**三種形狀都處理**,絕不 `[object Object]`。
//
// 為什麼是純函式、零依賴:要能被 `node --test` 直接 import 驗證(與既有三支
// 測試同一套,不引入 jsdom / vitest)。所以這裡用 duck typing 認 axios error,
// 不 import axios。

/**
 * 從錯誤取出機器可讀的 error code。
 *
 * **UI 分流只准依賴這個**,不准比對 message 的自然語言內容 —— 後端改一個字
 * 就靜默壞掉的教訓已經發生過一次(`LoginView.vue` 的 `'等待核准'` 子字串比對)。
 *
 * @param {unknown} err
 * @returns {string|null} 例如 `'AUTH_PENDING_APPROVAL'`;拿不到就 null
 */
export function extractErrorCode(err) {
  const code = err?.response?.data?.error?.code
  if (typeof code === 'string' && code) return code
  // 過渡期 fallback:舊後端沒有信封,但 4 處 typed error 的 legacy dict
  // 已經帶了 `code`(例:`untrusted_host`)。
  const legacy = err?.response?.data?.detail
  if (legacy && typeof legacy === 'object' && typeof legacy.code === 'string') {
    return legacy.code || null
  }
  return null
}

/**
 * 從錯誤取出 `error.details`(結構化補充,例:pydantic 逐欄位錯誤)。
 * @param {unknown} err
 * @returns {unknown}
 */
export function extractErrorDetails(err) {
  return err?.response?.data?.error?.details ?? null
}

/**
 * 從錯誤取出追蹤用的 request id(W3-3 之後才會普遍有值)。
 * @param {unknown} err
 * @returns {string|null}
 */
export function extractRequestId(err) {
  const id = err?.response?.data?.error?.request_id
  return typeof id === 'string' && id ? id : null
}

/**
 * 讀「typed error」的補充欄位,新舊契約都認。
 *
 * 後端的 typed 400 會帶結構化欄位讓 UI 做事,例如 `api/models.py:357` 的
 * `untrusted_host` 帶 `host` —— 治理 UI 靠它把主機加進受信任清單後重試。
 * 信封把這些欄位收進 `error.details`,但 legacy `detail` dict 裡也還有一份
 * (過渡期雙寫),所以兩邊都查。
 *
 * @param {unknown} err
 * @param {string} name 欄位名,例如 `'host'`
 * @returns {unknown} 找不到回 undefined
 */
export function extractErrorField(err, name) {
  const details = err?.response?.data?.error?.details
  if (details && typeof details === 'object' && name in details) return details[name]
  const legacy = err?.response?.data?.detail
  if (legacy && typeof legacy === 'object' && !Array.isArray(legacy) && name in legacy) {
    return legacy[name]
  }
  return undefined
}

/**
 * 把 pydantic 的 422 array 壓成人看得懂的一行。
 * @param {unknown[]} items
 * @returns {string}
 */
function formatValidationItems(items) {
  const parts = []
  for (const item of items.slice(0, 3)) {
    if (typeof item === 'string') {
      parts.push(item)
      continue
    }
    if (!item || typeof item !== 'object') {
      parts.push(String(item))
      continue
    }
    const loc = Array.isArray(item.loc)
      ? item.loc.filter((x) => !['body', 'query', 'path'].includes(String(x)))
      : []
    const where = loc.length ? loc.join('.') : '請求內容'
    parts.push(`${where}: ${item.msg || '格式不正確'}`)
  }
  let out = parts.join('；')
  if (items.length > 3) out += `（另有 ${items.length - 3} 項）`
  return out
}

/**
 * 把任意 legacy `detail` 值轉成字串,**永不回傳 `[object Object]`**。
 * @param {unknown} detail
 * @returns {string} 轉不出有意義的字串時回 ''
 */
function stringifyLegacyDetail(detail) {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return formatValidationItems(detail)
  if (typeof detail === 'object') {
    // 既有 4 處後端 typed dict detail 的 `{code, message}` 範本
    if (typeof detail.message === 'string' && detail.message) return detail.message
    if (typeof detail.msg === 'string' && detail.msg) return detail.msg
    if (typeof detail.detail === 'string' && detail.detail) return detail.detail
    // 最後手段:JSON.stringify 而非隱式 toString。醜,但**可讀**,
    // 而 `[object Object]` 是零資訊。
    try {
      const json = JSON.stringify(detail)
      return json && json !== '{}' ? json : ''
    } catch {
      return ''
    }
  }
  return String(detail)
}

/**
 * 取出要顯示給使用者的錯誤訊息。
 *
 * 優先序:`error.message`(新信封) → legacy `detail`(三種形狀都處理) →
 * `fallback` → `err.message` → 泛用字串。
 *
 * @param {unknown} err 通常是 axios error,但任何東西都不會爆
 * @param {string} [fallback] 呼叫端的情境訊息(例:'刪除失敗')
 * @returns {string} 永遠是非空字串
 */
export function extractError(err, fallback) {
  const enveloped = err?.response?.data?.error?.message
  if (typeof enveloped === 'string' && enveloped) return enveloped

  const legacy = stringifyLegacyDetail(err?.response?.data?.detail)
  if (legacy) return legacy

  if (fallback) return fallback
  if (typeof err?.message === 'string' && err.message) return err.message
  return '操作失敗，請稍後再試'
}

// ── 登入分流 ────────────────────────────────────────────────────────────────

/** 後端在帳號尚未被 admin 核准時回的 code。 */
export const AUTH_PENDING_APPROVAL = 'AUTH_PENDING_APPROVAL'
/** 使用者已完成刷卡但還沒填單位。 */
export const AUTH_PENDING_REGISTRATION = 'AUTH_PENDING_REGISTRATION'
/** 帳號已切成 SSO-only,要引導改按單一登入。 */
export const AUTH_LOCAL_PASSWORD_DISABLED = 'AUTH_LOCAL_PASSWORD_DISABLED'

const PENDING_CODES = new Set([AUTH_PENDING_APPROVAL, AUTH_PENDING_REGISTRATION])

/**
 * 判斷登入失敗要走哪個 UI 分支。
 *
 * **這支函式存在的唯一理由是防再犯。** `LoginView.vue:419` 原本寫
 * `detail.includes('等待核准')` —— 後端把訊息改成「帳號審核中」就靜默壞掉,
 * 使用者會看到泛用錯誤而不是待核准說明頁,而且沒有任何測試會紅。
 *
 * 現在 code 是契約,訊息文字可以自由改寫。舊後端(還沒有 `error.code`)靠
 * 子字串比對當**過渡期 fallback**,一個 release 後可以拿掉。
 *
 * @param {unknown} err
 * @returns {{ message: string, isPending: boolean, code: string|null,
 *             ssoOnly: boolean }}
 */
export function classifyLoginError(err) {
  const code = extractErrorCode(err)
  const message = extractError(err, '登入失敗 — 請檢查帳號密碼')

  if (code) {
    return {
      message,
      code,
      isPending: PENDING_CODES.has(code),
      ssoOnly: code === AUTH_LOCAL_PASSWORD_DISABLED,
    }
  }

  // 過渡期 fallback:後端還沒帶 code 時才走這裡。
  const lowered = message.toLowerCase()
  return {
    message,
    code: null,
    isPending: message.includes('等待核准') || lowered.includes('pending'),
    ssoOnly: false,
  }
}
