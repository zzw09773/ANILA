// 中科院憑證卡登入 — 可重用 popup orchestration + backend API wrapper。
//
// 整套流程拆成五個 primitive,LoginView 可自行串接:
//
//   detectCard()          → 偵測卡片,回 cardSN + cert claims (employeeId / name / email)
//   generateTbs()         → 跟 backend 要 challenge (一次性 nonce, JWT 包裝)
//   signWithCard()        → PIN + nonce 進本機元件 → 回 PKCS#7 簽章
//   submitSignature()     → POST /api/auth/card/verify, backend 解 PKCS#7 抽身分驗證
//   loginWithCard()       → 上面四步串成一個 high-level 入口
//
// 為什麼用 popup:本機元件 (中華電信 HiPKI / cht/ mock) 在 ``localhost:16888``,
// 主站在 ``https://172.16.120.35``。瀏覽器 CORS 擋直接 fetch,所以開 popup
// (popup 本身 same-origin localhost:16888 → fetch 可達),用 ``postMessage``
// 跨 origin 把結果回主站。
//
// 對應 cht/templates/popupForm.html 的協議:
//
//   1. 主站 window.open(`${componentOrigin}/popupForm`)
//   2. popup → 主站 ``{func: "getTbs"}``
//   3. 主站 → popup ``{func: "GetUserCert" | "MakeSignature", ...payload}``
//   4. popup fetch 本機元件 ``/cht_api/pkcs11info`` 或 ``/cht_api/sign``
//   5. popup → 主站 ``{func: "pkcs11info" | "sign", ...result}``
//   6. popup window.close()

import { cardChallenge, cardVerify } from './auth'

// 中華電信 HiPKI 本機元件 (CHT MCAv2 PKCS#11) 預設 origin。
// 內網 prod 跟 dev cht/ mock 都跑 localhost:16888;特殊部署用 VITE_ 覆寫。
export const CARD_COMPONENT_ORIGIN =
  (import.meta.env.VITE_CARD_COMPONENT_ORIGIN || 'http://localhost:16888').replace(/\/$/, '')

const DEFAULT_INSTALL_TIMEOUT_MS = 3500
const POPUP_FEATURES = 'height=200,width=200,left=100,top=20'

// ─── Errors ────────────────────────────────────────────────────────────────

export class CardComponentNotInstalledError extends Error {
  constructor(origin) {
    super(`尚未安裝中華電信本機元件 (${origin}),請確認元件運作中。`)
    this.name = 'CardComponentNotInstalledError'
  }
}

export class CardNotInsertedError extends Error {
  constructor() {
    super('卡片未插入或本機元件未偵測到卡片。請插入卡片後重試。')
    this.name = 'CardNotInsertedError'
  }
}

export class CardSignAbortedError extends Error {
  constructor(msg) {
    super(msg || '使用者中斷簽章流程')
    this.name = 'CardSignAbortedError'
  }
}

export class CardSignFailedError extends Error {
  constructor(retCode, lastError) {
    super(
      `PIN 錯誤或卡片簽章失敗 (ret_code=${retCode}` +
        (lastError ? `, last_error=${lastError}` : '') +
        ')',
    )
    this.name = 'CardSignFailedError'
    this.retCode = retCode
  }
}

// ─── Internal: shared popup round-trip ─────────────────────────────────────

/**
 * 開一次 popup,送 getTbs 回 payload,等對應 result func 回來。
 *
 * @param {object} opts
 * @param {string} opts.componentOrigin   本機元件 origin
 * @param {object} opts.getTbsPayload     popup 問 getTbs 時要回給它的物件
 *                                        (必含 ``func: "GetUserCert" | "MakeSignature"``)
 * @param {string} opts.expectedResultFunc 預期 popup 回的 result func 名稱
 *                                        (``"pkcs11info"`` 或 ``"sign"``)
 * @param {number} [opts.installTimeoutMs] 等元件回應的 timeout
 * @returns {Promise<object>}  popup 回的完整 message data
 */
function runPopupRoundTrip({
  componentOrigin,
  getTbsPayload,
  expectedResultFunc,
  installTimeoutMs = DEFAULT_INSTALL_TIMEOUT_MS,
}) {
  if (!componentOrigin) {
    return Promise.reject(new Error('本機元件 origin 未設定'))
  }

  return new Promise((resolve, reject) => {
    const popup = window.open(
      `${componentOrigin}/popupForm`,
      'anila-card-session',
      POPUP_FEATURES,
    )
    if (!popup) {
      reject(new Error('無法開啟簽章視窗 (瀏覽器可能擋了 popup)'))
      return
    }

    let settled = false
    let installTimer = null
    let pollTimer = null

    function cleanup() {
      window.removeEventListener('message', handler)
      if (installTimer) {
        clearTimeout(installTimer)
        installTimer = null
      }
      if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
      }
      if (popup && !popup.closed) {
        popup.close()
      }
    }

    function finalize(fn, value) {
      if (settled) return
      settled = true
      cleanup()
      fn(value)
    }

    function handler(event) {
      if (event.origin !== componentOrigin) return
      let msg
      try {
        msg = JSON.parse(event.data)
      } catch {
        return
      }
      if (!msg || typeof msg.func !== 'string') return

      if (msg.func === 'getTbs') {
        if (installTimer) {
          clearTimeout(installTimer)
          installTimer = null
        }
        popup.postMessage(JSON.stringify(getTbsPayload), componentOrigin)
        return
      }

      if (msg.func === expectedResultFunc) {
        finalize(resolve, msg)
      }
    }

    window.addEventListener('message', handler)

    installTimer = setTimeout(() => {
      finalize(reject, new CardComponentNotInstalledError(componentOrigin))
    }, installTimeoutMs)

    pollTimer = setInterval(() => {
      if (popup.closed) {
        finalize(reject, new CardSignAbortedError())
      }
    }, 400)
  })
}

// ─── 1. 卡片偵測 ────────────────────────────────────────────────────────────

/**
 * 解析 X.509 subjectDN 字串成 key-value object (lowercase keys)。
 *
 * @param {string} dn 例:``"C=TW,O=國家中山科學研究院,CN=鄒惠翔,serialNumber=1090868"``
 * @returns {Object<string, string>}
 */
function parseSubjectDN(dn) {
  if (!dn) return {}
  const result = {}
  for (const part of dn.split(',')) {
    const trimmed = part.trim()
    const eq = trimmed.indexOf('=')
    if (eq <= 0) continue
    const key = trimmed.slice(0, eq).trim().toLowerCase()
    const value = trimmed.slice(eq + 1).trim()
    if (key && value) result[key] = value
  }
  return result
}

/**
 * 從 pkcs11info 回應挑出 signer cert (digitalSignature usage 且非 CA),
 * 抽 employee_id / display_name / email。
 *
 * @param {object} info pkcs11info 完整回應
 * @returns {{cardSN: string, employeeId: string, displayName: string, email: string}}
 * @throws CardNotInsertedError 找不到 slot / token / signer cert
 */
function extractCardClaims(info) {
  const slot = info?.slots?.[0]
  const token = slot?.token
  if (!token) {
    throw new CardNotInsertedError()
  }

  const cardSN = token.serialNumber || null
  const certs = Array.isArray(token.certs) ? token.certs : []

  // signer cert 特徵:有 email (CA 沒) + usage 含 digitalSignature + subjectDN
  // 帶 serialNumber (員工編號)。挑第一張符合,優先 RSA (label "RSACert1") 但
  // 不強制 — 真實卡可能只有 EC。
  const signer = certs.find(
    c => c?.email && /digitalsignature/i.test(c?.usage || '')
       && /serialnumber=/i.test(c?.subjectDN || ''),
  )
  if (!signer) {
    throw new CardNotInsertedError()
  }

  const dn = parseSubjectDN(signer.subjectDN)
  const employeeId = dn.serialnumber
  const displayName = signer.subjectCN || dn.cn
  const email = signer.email

  if (!employeeId || !displayName) {
    throw new CardNotInsertedError()
  }

  return { cardSN, employeeId, displayName, email }
}

/**
 * 卡片偵測:popup → ``GetUserCert`` → pkcs11info → 抽 cert claims。
 *
 * @param {{componentOrigin?: string, installTimeoutMs?: number}} [opts]
 * @returns {Promise<{cardSN: string, employeeId: string, displayName: string, email: string}>}
 */
export async function detectCard({
  componentOrigin = CARD_COMPONENT_ORIGIN,
  installTimeoutMs = DEFAULT_INSTALL_TIMEOUT_MS,
} = {}) {
  const result = await runPopupRoundTrip({
    componentOrigin,
    // 對齊 cht/templates/popupForm.html:func==='GetUserCert' → fetch pkcs11info。
    // withCert=true 在真實 HiPKI 是 query param,mock 永遠夾帶 cert,不影響。
    getTbsPayload: { func: 'GetUserCert', withCert: 'true' },
    expectedResultFunc: 'pkcs11info',
    installTimeoutMs,
  })

  if (result.ret_code != null && result.ret_code !== 0) {
    throw new CardNotInsertedError()
  }

  return extractCardClaims(result)
}

// ─── 2. 產生 TBS (向 backend 取 challenge) ──────────────────────────────────

/**
 * 跟 backend 要一次性 challenge。``nonce`` 是給卡片簽的內容,``challenge_token``
 * 是 JWT 包裝後讓 backend 之後驗回 round-trip。
 *
 * @returns {Promise<{challenge_token: string, nonce: string, expires_in: number}>}
 */
export async function generateTbs() {
  const { data } = await cardChallenge()
  return data
}

// ─── 3. 簽章 ────────────────────────────────────────────────────────────────

/**
 * 簽章:popup → ``MakeSignature`` → cht_api/sign → 回 PKCS#7 簽章。
 *
 * @param {{pin: string, tbs: string, componentOrigin?: string}} opts
 * @returns {Promise<{signature: string, cardSN: string|null}>}
 */
export async function signWithCard({
  pin,
  tbs,
  componentOrigin = CARD_COMPONENT_ORIGIN,
  installTimeoutMs = DEFAULT_INSTALL_TIMEOUT_MS,
} = {}) {
  if (!pin) throw new Error('請輸入 PIN 碼')
  if (!tbs) throw new Error('缺少 TBS (challenge 未產生)')

  const result = await runPopupRoundTrip({
    componentOrigin,
    // 對齊 cht/login.html getTbsPackage() 欄位。
    getTbsPayload: {
      tbs,
      tbsEncoding: 'NONE',
      hashAlgorithm: 'SHA256',
      withCardSN: 'false',
      pin,
      nonce: '',
      func: 'MakeSignature',
      signatureType: 'PKCS7',
    },
    expectedResultFunc: 'sign',
    installTimeoutMs,
  })

  if (result.ret_code !== 0) {
    throw new CardSignFailedError(result.ret_code, result.last_error)
  }
  if (!result.signature) {
    throw new CardSignFailedError(null, '元件回傳缺少 signature')
  }

  return {
    signature: result.signature,
    cardSN: result.cardSN ?? null,
  }
}

// ─── 4. 提交給後端 ─────────────────────────────────────────────────────────

/**
 * 把簽章送到 backend 驗證 + 建 session。Backend 會重新 parse PKCS#7 抽
 * 真實身分 (不信任 frontend supplied employee_id),所以這步是「最終真相」。
 *
 * @param {{challenge_token: string, signature: string, cardSN?: string|null}} payload
 * @returns {Promise<{status: 'ok'} | {status: 'pending_registration', registration_token: string, ...} | {status: 'pending_approval', ...}>}
 */
export async function submitSignature(payload) {
  const resp = await cardVerify({
    challenge_token: payload.challenge_token,
    signature: payload.signature,
    card_serial: payload.cardSN ?? null,
  })
  // 200 = 登入成功;202 = pending (registration / approval)。
  if (resp.status === 200) {
    return { status: 'ok' }
  }
  // 202 body:{ status: 'pending_registration' | 'pending_approval', ... }
  return resp.data
}

// ─── 5. High-level 入口 ────────────────────────────────────────────────────

/**
 * 完整登入流程:偵測 (optional) → challenge → sign → submit。
 *
 * UI 可選兩種模式:
 * 1. **直接登入** — caller 只給 PIN,內部跳過 detect 直接跑 challenge/sign/submit
 *    (跟舊版 cardLogin.js 行為相容)。
 * 2. **detect-first** — caller 先呼叫 ``detectCard()`` 顯示使用者資訊,確認後
 *    再呼叫 ``loginWithCard()``。第二個 popup 仍會跑 sign 流程。
 *
 * 第二種 mode 多開一次 popup (detect 跟 sign 是兩個獨立 popup session),好處
 * 是 user 可以在輸 PIN 前先看到自己的卡片資訊。
 *
 * @param {{pin: string, componentOrigin?: string}} options
 * @returns {Promise<{status: 'ok'|'pending_registration'|'pending_approval', [key: string]: any}>}
 */
export async function loginWithCard({ pin, componentOrigin = CARD_COMPONENT_ORIGIN } = {}) {
  const challenge = await generateTbs()
  const { signature, cardSN } = await signWithCard({
    pin,
    tbs: challenge.nonce,
    componentOrigin,
  })
  return await submitSignature({
    challenge_token: challenge.challenge_token,
    signature,
    cardSN,
  })
}
