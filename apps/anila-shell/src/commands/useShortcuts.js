// 全域快捷鍵掛載 hook —— 只認登錄表裡 `global: true` 的條目。
//
// 設計約束(需求書明列):
//   * 焦點在輸入框內時 ⌘K / ⌘/ 仍要能觸發 → 因此不做 "ignore when
//     event.target is input" 的過濾;全域鍵一律是 mod 系組合鍵,不會撞到打字。
//   * 純 Esc / Enter 等既有區域行為不可被搶 → 登錄表裡那些條目 global=false,
//     這裡根本不會比對到它們。
//   * 注音組字中(isComposing)一律放行給 IME。

import { useEffect, useRef } from "react";
import { resolveGlobalShortcut } from "./shortcuts.js";

/**
 * @param {Record<string, (e: KeyboardEvent) => void>} handlers
 *        key = shortcut id(見 shortcuts.js);沒有 handler 的 id 不會被攔。
 * @param {{ enabled?: boolean }} [options]
 */
export function useShortcuts(handlers, { enabled = true } = {}) {
  const handlersRef = useRef(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });

  useEffect(() => {
    if (!enabled || typeof window === "undefined") return undefined;
    const onKeyDown = (event) => {
      if (event.isComposing || event.keyCode === 229) return;
      const shortcut = resolveGlobalShortcut(event);
      if (!shortcut) return;
      const handler = handlersRef.current?.[shortcut.id];
      if (typeof handler !== "function") return;
      event.preventDefault();
      handler(event);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
