import { Component, type ErrorInfo, type ReactNode } from 'react'

// Top-level error boundary.
//
// W0-7(補救計畫 Wave 0)修正三件既有缺陷:
//   1. **不再把 `error.stack` 與 `componentStack` 渲染給使用者看**。這是涉密
//      平台,向一般使用者洩漏內部檔案路徑與元件名沒有好處;而且 error.message
//      在資料渲染元件崩潰時可能夾帶使用者內容或文件內文。改為顯示短代碼,
//      完整資訊仍然進 console(這本來就是本檔存在的理由,保留)。
//   2. **`localStorage.clear()` 改為只清 `anilalm` 前綴**。三個 SPA 同源
//      (nginx 的 /anila/、/anilalm、/ 都在同一個 server block),原本的
//      clear() 會一併清掉 shell 的 `anila-folders`/`anila-dismissed-banners`
//      與 governance 的 `anila.theme` —— 使用者在 anilalm 點一次「清除並重載」,
//      shell 的資料夾分類全部消失。
//   3. **不寫死深色 hex**。原本 `#0B0D10`/`#E8EAED`/`#7C7BFF` 完全無視主題。
//      崩潰時不能依賴 React context,所以直接讀 `document.body` 的 light class
//      (ThemeContext 就是 toggle 這個 class),純 DOM 讀取、不依賴任何狀態。

interface State {
  error: Error | null
  code: string | null
}

interface Props {
  children: ReactNode
}

interface Palette {
  bg: string
  fg: string
  fgMuted: string
  surface: string
  border: string
  danger: string
  accent: string
}

const DARK: Palette = {
  bg: '#0B0D10',
  fg: '#E8EAED',
  fgMuted: '#9AA3AE',
  surface: '#13161B',
  border: '#262B34',
  danger: '#FF6B6B',
  accent: '#7C7BFF',
}

const LIGHT: Palette = {
  bg: '#f6f7f9',
  fg: '#1a2029',
  fgMuted: '#555b65',
  surface: '#ffffff',
  border: '#dbdee2',
  danger: '#ba3630',
  accent: '#2b4c7e',
}

/**
 * (name, message) → 6 碼 base36 短代碼(FNV-1a,決定性)。
 * 與 apps/anila-shell/src/errorBoundary.jsx 及 governance 的 errorHandler.js
 * 刻意採同一演算法,方便三個 app 的代碼交叉比對。
 */
export function errorCode(input: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(36).toUpperCase().padStart(6, '0').slice(-6)
}

/** 清掉本 app 自己的 localStorage key,不動同源其他 SPA 的。 */
export function clearOwnStorage(storage: Storage = localStorage): string[] {
  const removed: string[] = []
  for (let i = storage.length - 1; i >= 0; i -= 1) {
    const key = storage.key(i)
    if (key && (key.startsWith('anilalm:') || key.startsWith('anilalm-') || key.startsWith('studio.'))) {
      storage.removeItem(key)
      removed.push(key)
    }
  }
  return removed
}

function currentPalette(): Palette {
  // ThemeContext 的實作是 document.body.classList.toggle('light')
  const isLight = typeof document !== 'undefined' && document.body?.classList.contains('light')
  return isLight ? LIGHT : DARK
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, code: null }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error, code: errorCode(`${error.name}:${error.message}`) }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    const code = this.state.code ?? errorCode(`${error.name}:${error.message}`)
    // 完整資訊只進 console —— minified JS stack 沒用,component stack 才有
    // source-level display name,這是排錯真正需要的東西。
    console.error(`[ANILA LM crash ${code}]`, error)
    console.error(`[ANILA LM crash ${code}] component stack:`, info.componentStack)
  }

  reset = () => this.setState({ error: null, code: null })

  handleClearAndReload = () => {
    clearOwnStorage()
    // Use the configured base path (same source App.tsx derives the router
    // basename from) so recovery works under any deploy mount point.
    window.location.replace(import.meta.env.BASE_URL || '/')
  }

  render() {
    if (!this.state.error) return this.props.children

    const c = currentPalette()

    return (
      <div
        role="alert"
        style={{
          minHeight: '100dvh',
          padding: 32,
          background: c.bg,
          color: c.fg,
          fontFamily: 'ui-monospace, monospace',
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'center',
        }}
      >
        <div style={{ maxWidth: 560, width: '100%' }}>
          <h1 style={{ fontSize: 18, color: c.danger, marginTop: 0 }}>這個畫面發生錯誤</h1>
          <p style={{ fontSize: 13, lineHeight: 1.7, color: c.fgMuted }}>
            你的產出與知識庫內容沒有遺失。請先試「重試」;若同一個畫面重複發生,
            請把下面的錯誤代碼提供給平台管理員。
          </p>
          <div
            style={{
              background: c.surface,
              border: `1px solid ${c.border}`,
              padding: '8px 12px',
              borderRadius: 8,
              fontSize: 13,
              margin: '16px 0',
            }}
          >
            錯誤代碼:<strong>{this.state.code}</strong>
          </div>
          <button
            type="button"
            onClick={this.reset}
            style={{
              padding: '8px 14px',
              borderRadius: 8,
              border: `1px solid ${c.accent}`,
              background: 'transparent',
              color: c.accent,
              cursor: 'pointer',
              fontFamily: 'inherit',
              fontSize: 13,
              marginRight: 8,
            }}
          >
            重試
          </button>
          <button
            type="button"
            onClick={this.handleClearAndReload}
            style={{
              padding: '8px 14px',
              borderRadius: 8,
              border: `1px solid ${c.border}`,
              background: c.surface,
              color: c.fg,
              cursor: 'pointer',
              fontFamily: 'inherit',
              fontSize: 13,
            }}
          >
            清除本頁設定並重新載入
          </button>
        </div>
      </div>
    )
  }
}
