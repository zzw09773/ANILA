// 中科院憑證卡登入流程 — 跟本機 CHT HiPKI 元件對話的 popup orchestration。
//
// 對齊 cht/login.html + cht/templates/popupForm.html 的 postMessage 協議：
//
//   1. 主站 fetch /api/auth/card/challenge → { challenge_token, nonce, ... }
//   2. 主站 window.open(`${cardComponentOrigin}/popupForm`)
//   3. popup 載入 → postMessage 給主站 `{func: "getTbs"}`
//   4. 主站收到 getTbs → postMessage 給 popup
//        `{func: "MakeSignature", pin, tbs: <nonce>, ...}`
//   5. popup → fetch /cht_api/sign 給本機元件
//   6. 本機元件回 PKCS#7 簽章 → popup postMessage 給主站
//        `{func: "sign", ret_code, signature, cardSN, ...}`
//   7. 主站 POST /api/auth/card/verify { challenge_token, signature, cardSN }
//
// 本檔包 step 2~6 成回 Promise 的 runCardSignPopup()，外加 step 1 / 7 兩支
// thin wrapper（requestCardChallenge / verifyCardSignature）。完整流程
// orchestration 留給 auth.jsx 的 loginWithCard，與既有 login() 並列。

import { authRequest, config } from "./api.js";

const DEFAULT_INSTALL_TIMEOUT_MS = 3500;

/**
 * @typedef {object} CardChallenge
 * @property {string} challenge_token
 * @property {string} nonce
 * @property {number} expires_in
 */

/**
 * @typedef {object} CardSignResult
 * @property {string} signature
 * @property {string|null} cardSN
 */

/**
 * Request a one-time signing challenge from CSP.
 * @returns {Promise<CardChallenge>}
 */
export async function requestCardChallenge() {
  return authRequest("/api/auth/card/challenge");
}

/**
 * Submit the signed challenge to CSP. Cookies will be set on success.
 * @param {{challenge_token: string, signature: string, card_serial?: string|null}} payload
 */
export async function verifyCardSignature(payload) {
  await authRequest("/api/auth/card/verify", {
    method: "POST",
    body: JSON.stringify({
      challenge_token: payload.challenge_token,
      signature: payload.signature,
      card_serial: payload.card_serial ?? null,
    }),
  });
}

/**
 * 開啟卡片簽章 popup，跑完 postMessage round-trip，回傳簽章。
 *
 * 結束條件（任一觸發 → cleanup + settle the Promise）：
 *   - 收到 `sign` 訊息 → resolve（ret_code=0）或 reject（其他）
 *   - install timeout 觸發（popup 開了但沒回 getTbs）→ reject「未安裝元件」
 *   - popup 被使用者手動關閉 → reject「使用者中斷」
 *   - popup window.open 直接被瀏覽器擋掉 → reject「無法開啟視窗」
 *
 * @param {{nonce: string, pin: string, componentOrigin?: string, installTimeoutMs?: number}} options
 * @returns {Promise<CardSignResult>}
 */
export function runCardSignPopup(options) {
  const {
    nonce,
    pin,
    componentOrigin = config.cardComponentOrigin,
    installTimeoutMs = DEFAULT_INSTALL_TIMEOUT_MS,
  } = options;

  if (!nonce) {
    return Promise.reject(new Error("缺少 nonce（challenge 未產生）"));
  }
  if (!pin) {
    return Promise.reject(new Error("請輸入 PIN 碼"));
  }
  if (!componentOrigin) {
    return Promise.reject(new Error("本機元件 origin 未設定"));
  }

  return new Promise((resolve, reject) => {
    const popup = window.open(
      `${componentOrigin}/popupForm`,
      "anila-card-sign",
      "height=200,width=200,left=100,top=20",
    );
    if (!popup) {
      reject(new Error("無法開啟簽章視窗（瀏覽器可能擋了 popup）"));
      return;
    }

    let settled = false;
    let installTimer = null;
    let pollTimer = null;

    function cleanup() {
      window.removeEventListener("message", handler);
      if (installTimer) {
        clearTimeout(installTimer);
        installTimer = null;
      }
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      if (popup && !popup.closed) {
        popup.close();
      }
    }

    function finalize(fn, value) {
      if (settled) return;
      settled = true;
      cleanup();
      fn(value);
    }

    function handler(event) {
      if (event.origin !== componentOrigin) return;
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return; // 不是 JSON，可能是其他 frame 的雜訊
      }
      if (!msg || typeof msg.func !== "string") return;

      if (msg.func === "getTbs") {
        // 元件已回應，取消 install timeout
        if (installTimer) {
          clearTimeout(installTimer);
          installTimer = null;
        }
        // 對齊 cht/login.html getTbsPackage() 的欄位
        const tbsPackage = {
          tbs: nonce,
          tbsEncoding: "NONE",
          hashAlgorithm: "SHA256",
          withCardSN: "false",
          pin,
          nonce: "",
          func: "MakeSignature",
          signatureType: "PKCS7",
        };
        popup.postMessage(JSON.stringify(tbsPackage), componentOrigin);
        return;
      }

      if (msg.func === "sign") {
        if (msg.ret_code !== 0) {
          finalize(
            reject,
            new Error(
              `PIN 錯誤或卡片驗證失敗（ret_code=${msg.ret_code}` +
                (msg.last_error ? `, last_error=${msg.last_error}` : "") +
                "）",
            ),
          );
          return;
        }
        if (!msg.signature) {
          finalize(reject, new Error("元件回傳缺少 signature"));
          return;
        }
        finalize(resolve, {
          signature: msg.signature,
          cardSN: msg.cardSN ?? null,
        });
        return;
      }
    }

    window.addEventListener("message", handler);

    installTimer = setTimeout(() => {
      finalize(
        reject,
        new Error(
          `尚未安裝中華電信本機元件（${componentOrigin}），` +
            "請先確認元件運作中或 cht/ mock 容器已啟動。",
        ),
      );
    }, installTimeoutMs);

    // 使用者手動關 popup（任何階段）也算中斷
    pollTimer = setInterval(() => {
      if (popup.closed) {
        finalize(reject, new Error("使用者中斷簽章流程"));
      }
    }, 400);
  });
}

/**
 * 完整 high-level 卡片登入：challenge → popup sign → verify。
 * Cookie 由 CSP /verify 種好；caller 後續可呼叫 /api/auth/me 取得 user。
 *
 * @param {{pin: string, componentOrigin?: string, installTimeoutMs?: number}} options
 * @returns {Promise<void>}
 */
export async function loginWithCard(options) {
  const challenge = await requestCardChallenge();
  const { signature, cardSN } = await runCardSignPopup({
    nonce: challenge.nonce,
    pin: options.pin,
    componentOrigin: options.componentOrigin,
    installTimeoutMs: options.installTimeoutMs,
  });
  await verifyCardSignature({
    challenge_token: challenge.challenge_token,
    signature,
    card_serial: cardSN,
  });
}
