// 統一的錯誤解讀 helper —— W2-12(補救計畫 Wave 2)。
//
// 為什麼從 client.ts 拆出來
// ------------------------
// 原本 `explainError` 住在 `client.ts`,而那個模組在 import 時就建立 axios
// instance 並讀 env。要單獨測「錯誤怎麼解讀」就得連帶拖起整個 client。純函式
// 拆到這裡,`client.ts` 再 re-export,24 個既有 import site 一行都不用改。
//
// 修的是什麼
// ----------
// 舊版 `explainError` 處理 string 與 array,**但不處理 dict**。CSP 有 4 處會回
// `detail={"code","message"}`,命中時兩條 typeof 檢查都不成立,fallthrough 到
// `` `${status} ${message}` `` → 使用者看到 **`"503 Request failed"`**,後端寫好
// 的整條中文說明完全丟失。W2-12 的後端信封 `{error:{code,message,...}}` 是新的
// 首選來源;legacy `detail` 三種形狀全部保留處理,舊部署不會斷。

/** 統一錯誤信封的內層(見 `services/csp/app/schemas/errors.py`)。 */
export interface ApiErrorBody {
  code: string
  message: string
  details?: unknown
  request_id?: string | null
}

/** 錯誤回應的完整 body。`detail` 是過渡期雙寫欄位,下一個 release 移除。 */
export interface ApiErrorEnvelope {
  error?: ApiErrorBody
  detail?: unknown
}

function errorBody(err: unknown): ApiErrorEnvelope | undefined {
  // duck typing 而非 `axios.isAxiosError`:這個模組刻意不 import axios,
  // 才能被單元測試直接載入。
  const response = (err as { response?: { data?: unknown } } | undefined)?.response
  const data = response?.data
  return data && typeof data === 'object' ? (data as ApiErrorEnvelope) : undefined
}

/**
 * 取出機器可讀的 error code。**UI 分流只准依賴這個**,不准比對 message 文字。
 * 過渡期也認 legacy dict detail 裡的 `code`。
 */
export function extractErrorCode(err: unknown): string | null {
  const body = errorBody(err)
  const code = body?.error?.code
  if (typeof code === 'string' && code) return code
  const legacy = body?.detail
  if (legacy && typeof legacy === 'object' && !Array.isArray(legacy)) {
    const legacyCode = (legacy as { code?: unknown }).code
    if (typeof legacyCode === 'string' && legacyCode) return legacyCode
  }
  return null
}

/** 取出 `error.details`(結構化補充)。 */
export function extractErrorDetails(err: unknown): unknown {
  return errorBody(err)?.error?.details ?? null
}

/** 取出追蹤用的 request id(W3-3 之後才會普遍有值)。 */
export function extractRequestId(err: unknown): string | null {
  const id = errorBody(err)?.error?.request_id
  return typeof id === 'string' && id ? id : null
}

function formatValidationItems(items: unknown[]): string {
  return items
    .map((d) => {
      if (typeof d === 'string') return d
      if (d && typeof d === 'object') {
        const msg = (d as { msg?: unknown }).msg
        if (typeof msg === 'string' && msg) return msg
        return JSON.stringify(d)
      }
      return String(d)
    })
    .join('; ')
}

/**
 * 把 legacy `detail` 轉成字串,**永不回傳 `[object Object]`**。
 * 回 `null` 代表「沒有可用訊息」,呼叫端才會往下 fallback。
 */
function stringifyLegacyDetail(detail: unknown): string | null {
  if (detail == null) return null
  if (typeof detail === 'string') return detail || null
  if (Array.isArray(detail)) {
    // pydantic 的 422 逐欄位錯誤。這條行為與改動前逐字相同。
    const formatted = formatValidationItems(detail)
    return formatted || null
  }
  if (typeof detail === 'object') {
    // 這就是原本掉進 `"503 Request failed"` 的那條路。
    const record = detail as { message?: unknown; msg?: unknown; detail?: unknown }
    for (const candidate of [record.message, record.msg, record.detail]) {
      if (typeof candidate === 'string' && candidate) return candidate
    }
    try {
      const json = JSON.stringify(detail)
      return json && json !== '{}' ? json : null
    } catch {
      return null
    }
  }
  return String(detail)
}

/**
 * 把任意錯誤格式化成給使用者看的 toast 訊息。
 *
 * 優先序:信封 `error.message` → legacy `detail`(字串 / array / dict 三種形狀)
 * → `status + message` → `err.message` → `String(err)`。
 */
export function explainError(err: unknown): string {
  const body = errorBody(err)

  const enveloped = body?.error?.message
  if (typeof enveloped === 'string' && enveloped) return enveloped

  const legacy = stringifyLegacyDetail(body?.detail)
  if (legacy) return legacy

  const axiosLike = err as
    | { response?: { status?: number }; message?: string }
    | undefined
  const status = axiosLike?.response?.status
  const message = typeof axiosLike?.message === 'string' ? axiosLike.message : ''
  if (status) return `${status} ${message}`.trim()
  if (err instanceof Error) return err.message
  if (typeof err === 'string') return err
  if (message) return message
  return String(err)
}
