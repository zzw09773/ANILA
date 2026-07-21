/**
 * asrStream 核心的 React 綁定(anila-shell 版)。
 *
 * 這是 `apps/anilalm/src/asr/useAsrInput.ts` 的對應版本 —— 邏輯相同,只是
 * anila-shell 沒有 TypeScript 所以寫成 .js、拿掉型別標註。**改一邊的行為就要
 * 同步另一邊。** 純錄音/WS/協定邏輯不在這裡,在共用的 asrStream.js(兩個 app
 * 各 vendor 一份相同副本)。
 *
 * 規格見 docs/planning/asr-voice-input-plan.md §2.5。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { createAsrSession, probeAsrAvailable } from './asrStream.js';

export { appendTranscript } from './asrStream.js';

/**
 * @param {object}   opts
 * @param {(text: string) => void} opts.appendText  定稿要接到哪
 * @param {boolean}  opts.busy   LLM 回覆中 → 不可啟動新錄音
 */
export function useAsrInput({ appendText, busy }) {
  const [available, setAvailable] = useState(false);
  const [state, setState] = useState('idle');
  const [partial, setPartial] = useState('');
  const [error, setError] = useState(null);

  const sessionRef = useRef(null);
  const composingRef = useRef(false);
  const pendingRef = useRef([]);
  // appendText 每次 render 是新 closure(close 了 draft state);session 只建
  // 一次 → 用 ref 讓 callback 永遠拿到最新那份,否則會 append 到過期草稿上,
  // 蓋掉使用者後來打的字。
  const appendRef = useRef(appendText);
  appendRef.current = appendText;

  useEffect(() => {
    let alive = true;
    void probeAsrAvailable().then((ok) => {
      if (alive) setAvailable(ok);
    });
    return () => {
      alive = false;
    };
  }, []);

  const flushPending = useCallback(() => {
    if (!pendingRef.current.length) return;
    const text = pendingRef.current.join('');
    pendingRef.current = [];
    appendRef.current(text);
  }, []);

  const deliver = useCallback((text) => {
    // 注音組字中不能 append —— 會打斷 composition、游標亂跳。緩衝到
    // compositionend 再吐。
    if (composingRef.current) {
      pendingRef.current.push(text);
      return;
    }
    appendRef.current(text);
  }, []);

  const ensureSession = useCallback(() => {
    if (sessionRef.current) return sessionRef.current;
    sessionRef.current = createAsrSession({
      onFinal: deliver,
      onPartial: setPartial,
      onPartialClear: () => setPartial(''),
      onState: setState,
      onError: setError,
    });
    return sessionRef.current;
  }, [deliver]);

  useEffect(() => {
    return () => {
      sessionRef.current?.dispose();
      sessionRef.current = null;
    };
  }, []);

  const toggle = useCallback(() => {
    const session = ensureSession();
    if (session.isRecording()) {
      session.stop();
      return;
    }
    // 對著鎖住的輸入框講話 = 講完沒地方去。
    if (busy) return;
    setError(null);
    void session.start();
  }, [busy, ensureSession]);

  const onCompositionStart = useCallback(() => {
    composingRef.current = true;
  }, []);

  const onCompositionEnd = useCallback(() => {
    composingRef.current = false;
    flushPending();
  }, [flushPending]);

  return {
    available,
    state,
    partial,
    error,
    clearError: () => setError(null),
    toggle,
    onCompositionStart,
    onCompositionEnd,
  };
}
