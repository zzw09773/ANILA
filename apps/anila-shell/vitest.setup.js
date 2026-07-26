import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";

// ── localStorage polyfill(W0-6)────────────────────────────────────────────
//
// 問題:Node ≥ 22 內建了實驗性的 global `localStorage`,它會**蓋掉 jsdom 提供
// 的那一份**。在沒有 `--localstorage-file` 的情況下,那個 global 是一個
// 方法全缺的空物件(`typeof localStorage === "object"` 但 `getItem`/`setItem`/
// `removeItem`/`clear` 全是 undefined)。
//
// 後果(這是 original-sins M3 抓到的真實缺口,不是理論):shell 有 8 個源碼檔
// 用 localStorage(app.jsx / data.jsx / banners.jsx / changelog.jsx /
// runtime/api.js / runtime/conversations.js / runtime/classifyRetryQueue.js /
// spanTree.jsx),它們全部把存取包在 `try {} catch {}` 裡靜默吞掉 —— 於是在
// Node ≥22 的機器上這整條持久化路徑**從來沒有真的被執行過**,而 CI 鎖 node 20
// 所以永遠看不到。C3 那個「主題選了不會記住」的 bug 就是這樣長期存活的。
//
// 修法:在測試環境提供一份行為正確、in-memory 的實作,並在每個測試前重置。
// 這樣既讓持久化路徑真的被執行,也讓測試之間互不污染。
function createMemoryStorage() {
  let store = new Map();
  return {
    get length() {
      return store.size;
    },
    key(index) {
      return Array.from(store.keys())[index] ?? null;
    },
    getItem(key) {
      const k = String(key);
      return store.has(k) ? store.get(k) : null;
    },
    setItem(key, value) {
      store.set(String(key), String(value));
    },
    removeItem(key) {
      store.delete(String(key));
    },
    clear() {
      store = new Map();
    },
  };
}

function installStorage(name) {
  const storage = createMemoryStorage();
  // 用 defineProperty 而非賦值:Node 內建的那個 global 可能是 getter-only。
  Object.defineProperty(globalThis, name, {
    value: storage,
    configurable: true,
    writable: true,
  });
  if (typeof window !== "undefined" && window !== globalThis) {
    Object.defineProperty(window, name, {
      value: storage,
      configurable: true,
      writable: true,
    });
  }
  return storage;
}

installStorage("localStorage");
installStorage("sessionStorage");

beforeEach(() => {
  // 每個測試拿到乾淨的 storage,且方法齊全 —— 不再需要 try/catch 保護。
  installStorage("localStorage");
  installStorage("sessionStorage");
});

// Sprint 13 PR B2: ensure RTL unmounts components between tests so the
// jsdom body doesn't accumulate siblings (which trips getByRole's
// "found multiple elements" guard).
afterEach(() => {
  cleanup();
});
