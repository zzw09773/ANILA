import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// localStorage polyfill —— 與 apps/anila-shell/vitest.setup.js 同一個理由:
// Node ≥22 內建的實驗性 global `localStorage` 會蓋掉 jsdom 那份,且方法全缺
// (`typeof localStorage === "object"` 但 getItem/setItem/removeItem/clear
// 都是 undefined)。anilalm 有 `anilalm:theme` 等 key,不修就測不到。
function createMemoryStorage(): Storage {
  let store = new Map<string, string>()
  return {
    get length() {
      return store.size
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null
    },
    getItem(key: string) {
      const k = String(key)
      return store.has(k) ? (store.get(k) as string) : null
    },
    setItem(key: string, value: string) {
      store.set(String(key), String(value))
    },
    removeItem(key: string) {
      store.delete(String(key))
    },
    clear() {
      store = new Map()
    },
  } as Storage
}

function installStorage(name: 'localStorage' | 'sessionStorage') {
  const storage = createMemoryStorage()
  Object.defineProperty(globalThis, name, { value: storage, configurable: true, writable: true })
  if (typeof window !== 'undefined' && window !== (globalThis as unknown as Window)) {
    Object.defineProperty(window, name, { value: storage, configurable: true, writable: true })
  }
}

beforeEach(() => {
  installStorage('localStorage')
  installStorage('sessionStorage')
})

afterEach(() => {
  cleanup()
})
