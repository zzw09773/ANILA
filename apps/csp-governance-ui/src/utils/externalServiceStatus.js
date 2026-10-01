/**
 * 外部服務頁的探測結果，以及存檔後對照語音閘道實際在用的位址。
 * 純函式，沒有 Vue／DOM。探測的請求在平台上打，這一頁只顯示回傳。
 */

export const SPEECH_GATEWAY_POLL_MS = 10_000
export const SPEECH_GATEWAY_POLL_DEADLINE_MS = 5 * 60 * 1000
export const SPEECH_GATEWAY_APPLIED = '語音閘道已改用新位址'
export const SPEECH_GATEWAY_PENDING = '語音閘道仍在使用舊位址，通常 1～2 分鐘內會更新'
export const SPEECH_GATEWAY_UNCONFIRMED = '語音閘道 5 分鐘內未改用新位址，請檢查語音閘道日誌'
export const SPEECH_GATEWAY_DISABLED = '語音辨識已停用。麥克風不會出現。'
export const SPEECH_GATEWAY_UNCONFIGURED = '未設定語音辨識。麥克風不會出現。'

export function probeResultText(item) {
  const status = item?.health_status
  const reason = typeof item?.health_detail === 'string' ? item.health_detail.trim() : ''
  if (status === 'healthy') return '健康'
  if (status === 'unhealthy') return reason ? `失敗 — ${reason}` : '失敗'
  if (status === 'disabled') return '未啟用'
  return reason ? `未知 — ${reason}` : '未知'
}

/** 比對用。去掉帳密、忽略主機大小寫與路徑尾端的斜線。憑證不會留在結果裡。 */
export function normalizeServiceUrl(value) {
  const raw = String(value ?? '').trim()
  if (!raw) return ''
  try {
    const url = new URL(raw)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return ''
    const hostname = url.hostname.toLowerCase()
    const host = hostname.includes(':') ? `[${hostname}]` : hostname
    const port = url.port ? `:${url.port}` : ''
    let path = url.pathname || ''
    if (path.length > 1) path = path.replace(/\/+$/, '')
    return `${url.protocol}//${host}${port}${path}${url.search || ''}`
  } catch {
    return ''
  }
}

export function serviceUrlsMatch(saved, reported) {
  return normalizeServiceUrl(saved) === normalizeServiceUrl(reported)
}

export function speechGatewayMessage(matched, elapsedMs, deadlineMs = SPEECH_GATEWAY_POLL_DEADLINE_MS) {
  if (matched) return SPEECH_GATEWAY_APPLIED
  if (Number(elapsedMs) >= deadlineMs) return SPEECH_GATEWAY_UNCONFIRMED
  return SPEECH_GATEWAY_PENDING
}

/** 存檔回應的 enabled / configured。只有非空且已啟用的位址才接著問閘道。 */
export function speechSaveFollowUp(saved) {
  const enabled = Boolean(saved?.enabled)
  const baseUrl = saved?.base_url || ''
  if (!enabled) return { text: SPEECH_GATEWAY_DISABLED, watch: false }
  const configured = saved?.configured == null
    ? Boolean(normalizeServiceUrl(baseUrl))
    : Boolean(saved.configured)
  if (!configured || !normalizeServiceUrl(baseUrl)) {
    return { text: SPEECH_GATEWAY_UNCONFIGURED, watch: false }
  }
  return { text: SPEECH_GATEWAY_PENDING, watch: true }
}

/**
 * 讀閘道現成的 /asr/health。503 仍帶 decode_url，不能讓 axios 把它當成失敗丟掉。
 * 回應不含憑證；比對前仍會再剝一層 userinfo。
 */
export async function readAsrHealth(http, options = {}) {
  const config = { validateStatus: () => true }
  if (options?.signal) config.signal = options.signal
  if (Number.isFinite(options?.timeout) && options.timeout > 0) {
    config.timeout = options.timeout
  }
  const response = await http.get('/asr/health', config)
  if (!response || response.data == null) return null
  return response.data
}

/**
 * 存檔後立刻問一次，之後約每 10 秒再問，最多 5 分鐘。
 * 停用或未設定不輪詢。已啟用的非空位址對上就停；到點還沒對上就改口請人看日誌。
 * 每次 health 的 timeout 不超過剩餘時間，停止時中止尚未回來的請求。
 * 回傳停止函式。
 */
export function watchSpeechGateway({
  savedBaseUrl,
  enabled,
  configured,
  fetchHealth,
  onStatus,
  intervalMs = SPEECH_GATEWAY_POLL_MS,
  deadlineMs = SPEECH_GATEWAY_POLL_DEADLINE_MS,
  now = () => Date.now(),
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (id) => clearTimeout(id),
}) {
  if (enabled === false) {
    onStatus(SPEECH_GATEWAY_DISABLED)
    return () => {}
  }
  if (configured === false || !normalizeServiceUrl(savedBaseUrl)) {
    onStatus(SPEECH_GATEWAY_UNCONFIGURED)
    return () => {}
  }

  const startedAt = now()
  let timer = null
  let controller = null
  let stopped = false

  async function tick() {
    if (stopped) return
    const elapsedMs = now() - startedAt
    const remaining = deadlineMs - elapsedMs
    if (remaining <= 0) {
      onStatus(speechGatewayMessage(false, elapsedMs, deadlineMs))
      stopped = true
      return
    }
    const request = new AbortController()
    controller = request
    let matched = false
    try {
      const body = await fetchHealth({
        signal: request.signal,
        timeout: remaining,
      })
      if (stopped || request.signal.aborted) return
      const reported = body && typeof body === 'object' ? body.decode_url : ''
      matched = Boolean(normalizeServiceUrl(savedBaseUrl)) && serviceUrlsMatch(savedBaseUrl, reported)
    } catch {
      matched = false
    }
    if (stopped) return
    const after = now() - startedAt
    onStatus(speechGatewayMessage(matched, after, deadlineMs))
    if (matched || after >= deadlineMs) {
      stopped = true
      return
    }
    timer = schedule(tick, intervalMs)
  }

  tick()

  return () => {
    stopped = true
    if (controller) controller.abort()
    if (timer != null) {
      cancel(timer)
      timer = null
    }
  }
}
