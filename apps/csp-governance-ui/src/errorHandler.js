// 全域錯誤處理 —— W0-7(補救計畫 Wave 0)。
//
// 為什麼需要:`src/main.js` 原本全檔 10 行、無 `app.config.errorHandler`,
// `App.vue` 也無 `onErrorCaptured`。任何 render 期 throw = 白畫面零訊息,
// 而這是**管理員唯一的入口**(治理後台)。
//
// 為什麼用 DOM banner 而不是 Vue 元件:錯誤發生時 Vue 元件樹本身可能已經
// 壞掉,再靠它渲染 fallback 不可靠。這裡直接把 banner 插進 document.body,
// 不依賴任何 Vue 狀態。
//
// 與 shell 的 errorBoundary.jsx 同一姿態:**不對使用者顯示 stack**,只給短
// 代碼;完整資訊進 console。代碼演算法兩邊刻意相同,方便交叉比對。

/**
 * (name, message) → 6 碼 base36 短代碼(FNV-1a,決定性)。
 * 與 apps/anila-shell/src/errorBoundary.jsx 的 errorCode() 必須保持一致。
 * @param {string} input
 * @returns {string}
 */
export function errorCode(input) {
  let h = 0x811c9dc5
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(36).toUpperCase().padStart(6, '0').slice(-6)
}

const BANNER_ID = 'anila-global-error-banner'

function showBanner(code) {
  // 已經有 banner 就只更新代碼,不疊加(同一次操作可能連續 throw)
  const existing = document.getElementById(BANNER_ID)
  if (existing) {
    const slot = existing.querySelector('[data-code]')
    if (slot) slot.textContent = code
    return
  }

  const el = document.createElement('div')
  el.id = BANNER_ID
  el.setAttribute('role', 'alert')
  el.style.cssText = [
    'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:99999',
    'padding:12px 16px',
    'background:var(--c-danger-soft, rgba(176,54,54,0.09))',
    'color:var(--c-fg-1, #1b2230)',
    'border-bottom:2px solid var(--c-danger, #b03636)',
    'font-size:13px', 'line-height:1.6',
    'display:flex', 'gap:12px', 'align-items:center', 'flex-wrap:wrap',
  ].join(';')

  const msg = document.createElement('span')
  msg.textContent = '治理後台發生錯誤,部分畫面可能無法操作。錯誤代碼:'
  const codeEl = document.createElement('strong')
  codeEl.setAttribute('data-code', '')
  codeEl.textContent = code

  const reload = document.createElement('button')
  reload.type = 'button'
  reload.textContent = '重新載入'
  reload.style.cssText = [
    'padding:4px 12px', 'border-radius:6px', 'cursor:pointer',
    'border:1px solid var(--c-danger, #b03636)',
    'background:transparent', 'color:var(--c-danger, #b03636)',
    'font-size:12px', 'font-family:inherit',
  ].join(';')
  reload.addEventListener('click', () => window.location.reload())

  const dismiss = document.createElement('button')
  dismiss.type = 'button'
  dismiss.textContent = '關閉'
  dismiss.setAttribute('aria-label', '關閉錯誤提示')
  dismiss.style.cssText = [
    'padding:4px 12px', 'border-radius:6px', 'cursor:pointer',
    'border:1px solid var(--c-border, #dfe3ea)',
    'background:transparent', 'color:var(--c-fg-2, #48505f)',
    'font-size:12px', 'font-family:inherit',
  ].join(';')
  dismiss.addEventListener('click', () => el.remove())

  el.append(msg, codeEl, reload, dismiss)
  document.body.appendChild(el)
}

/**
 * 掛上全域錯誤處理。回傳 unregister 供測試使用。
 * @param {import('vue').App} app
 */
export function installErrorHandler(app) {
  app.config.errorHandler = (err, _instance, info) => {
    const e = err instanceof Error ? err : new Error(String(err))
    const code = errorCode(`${e.name}:${e.message}`)
    console.error(`[ANILA governance crash ${code}]`, e)
    console.error(`[ANILA governance crash ${code}] vue info:`, info)
    showBanner(code)
  }

  // 未捕捉的 Promise rejection 也接起來 —— 12 處空 catch 之外的漏網面
  const onRejection = (event) => {
    const reason = event?.reason
    const e = reason instanceof Error ? reason : new Error(String(reason))
    const code = errorCode(`${e.name}:${e.message}`)
    console.error(`[ANILA governance unhandled rejection ${code}]`, e)
    showBanner(code)
  }
  window.addEventListener('unhandledrejection', onRejection)
  return () => window.removeEventListener('unhandledrejection', onRejection)
}
