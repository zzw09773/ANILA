// W0-7 驗收:治理後台的全域錯誤處理。
//
// 為什麼這支測試存在:`src/main.js` 原本全檔 10 行、零 errorHandler,
// 任何 render 期 throw = 白畫面零訊息,而這是**管理員唯一的入口**。
//
// 本檔用 node --test(與既有兩支測試同一套,不引入新依賴)。因為 errorHandler
// 的 banner 是純 DOM 操作(刻意不依賴 Vue,錯誤發生時元件樹可能已壞),
// 這裡自備一個極小的 document stub 就能完整驗證,不需要 jsdom。

import test from 'node:test'
import assert from 'node:assert/strict'

import { errorCode, installErrorHandler } from '../src/errorHandler.js'

// ── 最小 DOM stub ──────────────────────────────────────────────────────────
function makeElement(tag) {
  return {
    tagName: tag.toUpperCase(),
    id: '',
    style: { cssText: '' },
    children: [],
    textContent: '',
    _attrs: {},
    setAttribute(k, v) {
      this._attrs[k] = v
    },
    getAttribute(k) {
      return this._attrs[k] ?? null
    },
    addEventListener() {},
    append(...nodes) {
      this.children.push(...nodes)
    },
    appendChild(node) {
      this.children.push(node)
      return node
    },
    remove() {},
    querySelector() {
      return null
    },
  }
}

function installDomStub() {
  const body = makeElement('body')
  const byId = new Map()
  globalThis.document = {
    body,
    createElement: makeElement,
    getElementById: (id) => byId.get(id) ?? null,
  }
  // body.appendChild 時登記 id,讓 getElementById 生效
  const origAppend = body.appendChild.bind(body)
  body.appendChild = (node) => {
    if (node.id) byId.set(node.id, node)
    return origAppend(node)
  }
  const listeners = new Map()
  globalThis.window = {
    addEventListener: (ev, fn) => listeners.set(ev, fn),
    removeEventListener: (ev) => listeners.delete(ev),
    location: { reload() {} },
    _listeners: listeners,
  }
  return { body, byId, listeners }
}

function collectText(node) {
  let out = node.textContent || ''
  for (const child of node.children || []) out += collectText(child)
  return out
}

// ── errorCode ─────────────────────────────────────────────────────────────
test('errorCode 是決定性的,且與 shell/anilalm 同演算法(6 碼 base36 大寫)', () => {
  assert.equal(errorCode('Error:kaboom'), errorCode('Error:kaboom'))
  assert.notEqual(errorCode('Error:a'), errorCode('Error:b'))
  for (const s of ['', 'x', 'Error:long '.repeat(20)]) {
    const code = errorCode(s)
    assert.equal(code.length, 6)
    assert.match(code, /^[0-9A-Z]{6}$/)
  }
})

// ── installErrorHandler ───────────────────────────────────────────────────
test('render 期 throw 會掛上 banner 並顯示短代碼,而不是白畫面', () => {
  const dom = installDomStub()
  const app = { config: {} }
  const errors = []
  const origError = console.error
  console.error = (...args) => errors.push(args.map(String).join(' '))
  try {
    installErrorHandler(app)
    assert.equal(typeof app.config.errorHandler, 'function')
    app.config.errorHandler(new Error('kaboom'), null, 'render function')
  } finally {
    console.error = origError
  }

  const banner = dom.byId.get('anila-global-error-banner')
  assert.ok(banner, 'banner 應該被插進 document.body')
  assert.equal(banner.getAttribute('role'), 'alert')

  const text = collectText(banner)
  assert.ok(text.includes(errorCode('Error:kaboom')), 'banner 應含短代碼')
  assert.ok(text.includes('重新載入'))

  // 完整資訊進 console,不進畫面
  assert.ok(errors.join('\n').includes('[ANILA governance crash'))
})

test('**不把 stack 或原始 message 渲染給使用者**(涉密要求)', () => {
  const dom = installDomStub()
  const app = { config: {} }
  const origError = console.error
  console.error = () => {}
  try {
    installErrorHandler(app)
    app.config.errorHandler(new Error('機密文件內容不該外洩'), null, 'render')
  } finally {
    console.error = origError
  }
  const text = collectText(dom.byId.get('anila-global-error-banner'))
  assert.ok(!text.includes('機密文件內容不該外洩'), 'message 不得外顯')
  assert.ok(!/\bat\s+\w+/.test(text), 'stack frame 不得外顯')
})

test('連續 throw 不會疊加多個 banner', () => {
  const dom = installDomStub()
  const app = { config: {} }
  const origError = console.error
  console.error = () => {}
  try {
    installErrorHandler(app)
    app.config.errorHandler(new Error('a'), null, 'render')
    app.config.errorHandler(new Error('b'), null, 'render')
  } finally {
    console.error = origError
  }
  const banners = dom.body.children.filter((n) => n.id === 'anila-global-error-banner')
  assert.equal(banners.length, 1)
})

test('未捕捉的 Promise rejection 也會被接住(12 處空 catch 之外的漏網面)', () => {
  const dom = installDomStub()
  const app = { config: {} }
  const origError = console.error
  console.error = () => {}
  try {
    const uninstall = installErrorHandler(app)
    const handler = dom.listeners.get('unhandledrejection')
    assert.equal(typeof handler, 'function', '應註冊 unhandledrejection')
    handler({ reason: new Error('boom') })
    assert.ok(dom.byId.get('anila-global-error-banner'), 'rejection 也要顯示 banner')
    uninstall()
    assert.equal(dom.listeners.has('unhandledrejection'), false, 'uninstall 應可移除')
  } finally {
    console.error = origError
  }
})
