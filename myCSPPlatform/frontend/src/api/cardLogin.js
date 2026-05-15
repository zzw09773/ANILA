// 中科院憑證卡登入 — popup + postMessage orchestration（Vue SPA 版）。
//
// 對齊 cht/login.html + cht/templates/popupForm.html 的 postMessage 協議：
//
//   1. fetch /api/auth/card/challenge → { challenge_token, nonce, ... }
//   2. window.open(`${cardComponentOrigin}/popupForm`)
//   3. popup → 主站 postMessage `{func: "getTbs"}`
//   4. 主站 → popup postMessage `{func: "MakeSignature", pin, tbs: nonce, ...}`
//   5. popup → 本機元件 fetch /cht_api/sign
//   6. 本機元件 → popup → 主站 postMessage `{func: "sign", ret_code, signature, cardSN}`
//   7. POST /api/auth/card/verify { challenge_token, signature, cardSN }
//
// resource cleanup：popup / message listener / install timer / poll timer 任一
// 觸發 settle 時都會被清，避免 component unmount 時遺留 phantom session。

import { cardChallenge, cardVerify } from './auth'

// 中華電信 HiPKI 本機元件 (CHT MCAv2 PKCS#11) 預設 origin。
// 中科院內網 / dev 用 cht/ mock 都跑在 localhost:16888；只有特殊內網
// 部署需要覆寫時才設 VITE_CARD_COMPONENT_ORIGIN。
export const CARD_COMPONENT_ORIGIN =
  (import.meta.env.VITE_CARD_COMPONENT_ORIGIN || 'http://localhost:16888').replace(/\/$/, '')

const DEFAULT_INSTALL_TIMEOUT_MS = 3500

/**
 * 開啟卡片簽章 popup 並完成 postMessage round-trip。
 *
 * Settle 條件（任一觸發 → cleanup + Promise resolve/reject）：
 *   - 收到 `sign` 訊息 → resolve（ret_code=0）或 reject（其他）
 *   - install timeout 觸發（popup 開了但沒回 getTbs）→ reject「未安裝元件」
 *   - popup 被使用者手動關閉 → reject「使用者中斷」
 *   - popup window.open 直接被瀏覽器擋掉 → reject「無法開啟視窗」
 *
 * @param {{nonce: string, pin: string, componentOrigin?: string, installTimeoutMs?: number}} options
 * @returns {Promise<{signature: string, cardSN: string|null}>}
 */
export function runCardSignPopup(options) {
  const {
    nonce,
    pin,
    componentOrigin = CARD_COMPONENT_ORIGIN,
    installTimeoutMs = DEFAULT_INSTALL_TIMEOUT_MS,
  } = options

  if (!nonce) {
    return Promise.reject(new Error('缺少 nonce（challenge 未產生）'))
  }
  if (!pin) {
    return Promise.reject(new Error('請輸入 PIN 碼'))
  }
  if (!componentOrigin) {
    return Promise.reject(new Error('本機元件 origin 未設定'))
  }

  return new Promise((resolve, reject) => {
    const popup = window.open(
      `${componentOrigin}/popupForm`,
      'anila-card-sign',
      'height=200,width=200,left=100,top=20',
    )
    if (!popup) {
      reject(new Error('無法開啟簽章視窗（瀏覽器可能擋了 popup）'))
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
        const tbsPackage = {
          tbs: nonce,
          tbsEncoding: 'NONE',
          hashAlgorithm: 'SHA256',
          withCardSN: 'false',
          pin,
          nonce: '',
          func: 'MakeSignature',
          signatureType: 'PKCS7',
        }
        popup.postMessage(JSON.stringify(tbsPackage), componentOrigin)
        return
      }

      if (msg.func === 'sign') {
        if (msg.ret_code !== 0) {
          finalize(
            reject,
            new Error(
              `PIN 錯誤或卡片驗證失敗（ret_code=${msg.ret_code}` +
                (msg.last_error ? `, last_error=${msg.last_error}` : '') +
                '）',
            ),
          )
          return
        }
        if (!msg.signature) {
          finalize(reject, new Error('元件回傳缺少 signature'))
          return
        }
        finalize(resolve, {
          signature: msg.signature,
          cardSN: msg.cardSN ?? null,
        })
      }
    }

    window.addEventListener('message', handler)

    installTimer = setTimeout(() => {
      finalize(
        reject,
        new Error(
          `尚未安裝中華電信本機元件（${componentOrigin}），` +
            '請先確認元件運作中或 cht/ mock 容器已啟動。',
        ),
      )
    }, installTimeoutMs)

    pollTimer = setInterval(() => {
      if (popup.closed) {
        finalize(reject, new Error('使用者中斷簽章流程'))
      }
    }, 400)
  })
}

/**
 * 完整卡片登入 high-level 流程：challenge → popup sign → verify。
 *
 * 三種回傳形態（caller 用 ``result.status`` 判別）：
 * - ``{ status: 'ok' }`` — 登入成功，server 已種 cookie，caller 應 fetchUser + redirect
 * - ``{ status: 'pending_registration', ...payload }`` — 需要完成註冊，
 *   caller 應顯示「填單位」表單，submit 時用 ``payload.registration_token``
 * - ``{ status: 'pending_approval', ...payload }`` — 已填單位等核准，
 *   caller 顯示等待訊息
 *
 * @param {{pin: string, componentOrigin?: string}} options
 * @returns {Promise<{status: string, [key: string]: any}>}
 */
export async function loginWithCard(options) {
  const { data: challenge } = await cardChallenge()
  const { signature, cardSN } = await runCardSignPopup({
    nonce: challenge.nonce,
    pin: options.pin,
    componentOrigin: options.componentOrigin,
  })
  const verifyResp = await cardVerify({
    challenge_token: challenge.challenge_token,
    signature,
    card_serial: cardSN,
  })
  // 200 = 登入成功；202 = pending (registration / approval)。
  // axios 不會把 200/202 當錯誤 (我們在 cardVerify 內 validateStatus 也明示)，
  // 所以這裡直接看 status code 跟 payload。
  if (verifyResp.status === 200) {
    return { status: 'ok' }
  }
  // 202: pending — body 包含 status / employee_id / display_name / email / token
  return verifyResp.data
}
