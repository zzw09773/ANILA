/**
 * 把 asrStream 的核心接成 React hook。
 *
 * 這一層刻意薄:複雜的東西(錄音、重採樣、WS、協定不變式)都在 asrStream.js,
 * 那份在兩個 app 之間 vendor;本檔是 anilalm 專屬的 React 綁定,anila-shell
 * 會有一份對應的 .js 版本(它沒有 TypeScript)。
 *
 * 規格見 docs/planning/asr-voice-input-plan.md §2.5。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createAsrSession,
  probeAsrAvailable,
  watchSpeechStatus,
  type AsrSession,
  type AsrState,
} from './asrStream'

// 純邏輯放在 asrStream.js(兩個 app 共用的那份)—— 一來 anila-shell 也要用,
// 二來測試基礎設施在 anila-shell 那邊(anilalm 目前沒有 vitest)。
export { appendTranscript } from './asrStream'

interface UseAsrInputOptions {
  /** 定稿文字要接到哪(通常是 setComposer 的 append)。 */
  appendText: (text: string) => void
  /** LLM 回覆中 → 不可啟動新錄音(輸入框此時是 disabled 的)。 */
  busy: boolean
}

export interface UseAsrInputResult {
  /** ASR 有沒有部署(probe /asr/health)。false 時不要渲染麥克風按鈕。 */
  available: boolean
  state: AsrState
  /** 即時預覽文字。**不進輸入框** —— 顯示在輸入框下方一行。 */
  partial: string
  error: string | null
  clearError: () => void
  toggle: () => void
  /** 綁到 textarea 的 onCompositionStart / onCompositionEnd。 */
  onCompositionStart: () => void
  onCompositionEnd: () => void
}

export function useAsrInput({ appendText, busy }: UseAsrInputOptions): UseAsrInputResult {
  const [available, setAvailable] = useState(false)
  const [state, setState] = useState<AsrState>('idle')
  const [partial, setPartial] = useState('')
  const [error, setError] = useState<string | null>(null)

  const sessionRef = useRef<AsrSession | null>(null)
  // 重探時要知道現在是不是閒著:錄音中把按鈕收掉會讓使用者對著一個消失的
  // 控制項講話,而且 session 還活著沒人能停它。錄音中那一輪直接跳過,
  // 下一輪(或錄完之後的那一次)再收。
  const stateRef = useRef<AsrState>('idle')
  stateRef.current = state
  // 使用者正在用注音/拼音組字。組字中 append 會打斷 composition、游標亂跳,
  // 所以要緩衝到 compositionend 再吐出來。
  const composingRef = useRef(false)
  const pendingRef = useRef<string[]>([])
  // appendText 每次 render 都是新的 closure(它 close 了 composer state),
  // 但 session 只建立一次 —— 用 ref 讓 callback 永遠拿到最新的那一份,
  // 否則會 append 到過期的草稿上,把使用者後來打的字蓋掉。
  const appendRef = useRef(appendText)
  appendRef.current = appendText

  // 載入時問一次治理中心；聚焦或回到前景再問。沒有每分鐘的定時器。
  const probeRef = useRef<() => void>(() => {})
  useEffect(() => {
    let alive = true

    const runProbe = () => {
      if (!alive) return
      if (stateRef.current !== 'idle') return
      void probeAsrAvailable().then((ok) => {
        if (alive && stateRef.current === 'idle') setAvailable(ok)
      })
    }
    probeRef.current = runProbe
    const stop = watchSpeechStatus(runProbe)
    return () => {
      alive = false
      stop()
    }
  }, [])

  const flushPending = useCallback(() => {
    if (!pendingRef.current.length) return
    const text = pendingRef.current.join('')
    pendingRef.current = []
    appendRef.current(text)
  }, [])

  const deliver = useCallback(
    (text: string) => {
      if (composingRef.current) {
        pendingRef.current.push(text)
        return
      }
      appendRef.current(text)
    },
    []
  )

  const ensureSession = useCallback((): AsrSession => {
    if (sessionRef.current) return sessionRef.current
    sessionRef.current = createAsrSession({
      onFinal: deliver,
      onPartial: setPartial,
      onPartialClear: () => setPartial(''),
      onState: setState,
      onError: (message) => {
        setError(message)
        // 連線出錯 = 最有可能是解碼端沒了。不要讓使用者等滿一整個重探週期
        // 才發現按鈕該收起來。排到下一個 tick,讓 onState('idle') 先落地。
        setTimeout(() => probeRef.current(), 0)
      },
    })
    return sessionRef.current
  }, [deliver])

  useEffect(() => {
    return () => {
      sessionRef.current?.dispose()
      sessionRef.current = null
    }
  }, [])

  const toggle = useCallback(() => {
    const session = ensureSession()
    if (session.isRecording()) {
      session.stop()
      return
    }
    // 對著鎖住的輸入框講話 = 講完沒地方去。busy 時不開新錄音。
    if (busy) return
    setError(null)
    void session.start()
  }, [busy, ensureSession])

  const onCompositionStart = useCallback(() => {
    composingRef.current = true
  }, [])

  const onCompositionEnd = useCallback(() => {
    composingRef.current = false
    flushPending()
  }, [flushPending])

  return {
    available,
    state,
    partial,
    error,
    clearError: () => setError(null),
    toggle,
    onCompositionStart,
    onCompositionEnd,
  }
}
