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
 * 麥克風可用性的重探間隔(毫秒)。**與 anilalm 那份同值同語意。**
 *
 * 為什麼需要重探:原本只在掛載時探一次(`useEffect(..., [])`),所以解碼端
 * 「在開頁前」就掛掉會正確地把按鈕藏起來,「開頁之後」才掛掉卻留著一顆按了
 * 就壞的按鈕 —— 那是本專案定義的最糟失效模式(靜默成功)。而這一支是**首頁
 * 聊天室**在用的 hook:解碼端搬到算力中心之後,端點斷斷續續的機率遠高於本機
 * 容器,「麥克風永遠在、按下去永遠失敗」會是使用者最常遇到的那個畫面。
 *
 * 為什麼是 60 秒、而且只在分頁看得見的時候跑:
 *   * /asr/health 會**真的**向解碼端送一次探測請求(openai 協定是 100ms 靜音的
 *     辨識)。太密等於拿使用者的分頁去打算力中心。
 *   * 60s × 只算可見分頁 → 每個實際在看的分頁 1 次/分鐘。docker healthcheck
 *     另外 2 次/分鐘。
 *   * 代價:解碼端掛掉之後按鈕最多多留 60 秒,那 60 秒內按下去會拿到明確的
 *     錯誤訊息(WS 連不上),不是靜默。
 * 另外在 WebSocket 出錯時**立刻**重探一次,所以「錄到一半斷線」不用等滿 60 秒。
 */
const AVAILABILITY_REPROBE_MS = 60_000;

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
  // 重探時要知道現在是不是閒著:錄音中把按鈕收掉會讓使用者對著一個消失的
  // 控制項講話,而且 session 還活著沒人能停它。錄音中那一輪直接跳過,
  // 下一輪(或錄完之後的那一次)再收。
  const stateRef = useRef('idle');
  stateRef.current = state;
  const composingRef = useRef(false);
  const pendingRef = useRef([]);
  // appendText 每次 render 是新 closure(close 了 draft state);session 只建
  // 一次 → 用 ref 讓 callback 永遠拿到最新那份,否則會 append 到過期草稿上,
  // 蓋掉使用者後來打的字。
  const appendRef = useRef(appendText);
  appendRef.current = appendText;

  // 掛載時探一次,之後每 AVAILABILITY_REPROBE_MS 重探;分頁被切走就停,
  // 切回來立刻補一次(離開期間解碼端掛掉的話,回來就會看到正確的狀態)。
  const probeRef = useRef(() => {});
  useEffect(() => {
    let alive = true;

    const runProbe = () => {
      if (!alive) return;
      // 錄音中不動 available —— 見 stateRef 的說明。
      if (stateRef.current !== 'idle') return;
      void probeAsrAvailable().then((ok) => {
        if (alive && stateRef.current === 'idle') setAvailable(ok);
      });
    };
    probeRef.current = runProbe;

    runProbe();
    let timer = null;
    const start = () => {
      if (timer === null) timer = setInterval(runProbe, AVAILABILITY_REPROBE_MS);
    };
    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        runProbe();
        start();
      } else {
        stop();
      }
    };

    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      alive = false;
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
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
      onError: (message) => {
        setError(message);
        // 連線出錯 = 最有可能是解碼端沒了。不要讓使用者等滿一整個重探週期
        // 才發現按鈕該收起來。排到下一個 tick,讓 onState('idle') 先落地。
        setTimeout(() => probeRef.current(), 0);
      },
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
