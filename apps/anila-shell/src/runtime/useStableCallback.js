// 引用穩定的事件 handler —— 補救計畫 W2-9 的前置條件。
//
// 為什麼需要:`React.memo` 用預設淺比較,**任何**一個 prop 的 identity 變了
// 就整個白做。`app.jsx` 傳給 `MessageBubble` 的 9 個 handler 全是元件本體內的
// function 宣告(每次 render 都是新的物件),`onPickFollowUp` 更是 inline
// closure —— 不先把它們釘穩,memo 化只會讓 bundle 變大而不會少一次 render。
//
// 為什麼 `useCallback` 救不了:這些 handler 讀 `messagesByConv` /
// `conversations` / `agents`,而 `messagesByConv` 在串流期間**每個 token 都在
// 變**。把它放進 deps 等於沒有 memo;不放進 deps 就是 stale closure(讀到上一
// 個 token 的訊息陣列)—— 兩條都不行。
//
// 所以走 latest-ref(React RFC 的 `useEvent`)模式:對外的函式身分固定一次,
// 內部永遠轉呼叫最新那份閉包。ref 在 `useLayoutEffect` 更新 —— 在 paint 之前、
// 任何事件觸發之前,所以 handler 看到的一定是最新 render 的閉包。
//
// ⚠ 這個 hook **只給事件 handler**。不要用它包會在 render 期間被呼叫的函式
// (那會讀到上一輪的閉包)。

import { useCallback, useLayoutEffect, useRef } from "react";

/**
 * @template {(...args: any[]) => any} F
 * @param {F|undefined|null} fn
 * @returns {(...args: Parameters<F>) => ReturnType<F>|undefined}
 */
export function useStableCallback(fn) {
  const ref = useRef(fn);
  useLayoutEffect(() => {
    ref.current = fn;
  });
  return useCallback((...args) => {
    const current = ref.current;
    return typeof current === "function" ? current(...args) : undefined;
  }, []);
}
