// 治理中心的錯誤網子（finding-no-error-boundary-20260820，MEDIUM）。
//
// 工單要的是 B（面板層 onErrorCaptured＋fallback 渲染，讓洞變成可見的錯誤區塊）
// 加 A（app.config.errorHandler 當 log 通道）。這裡用 @vue/server-renderer 真的把
// 一個會拋的元件渲染在面板裡——「有 ErrorBoundary 這個字」不算數，畫面上要有字。
//   ① 同一次渲染：可讀訊息＋時間戳，同一個節點沒有 stack／路徑／主機名；
//   ② 會拋的元件真的拋，文字由面板產生；
//   ③ 沒有可對上伺服器 log 的關聯碼，所以只放時間戳。
// 已知邊界：這是最小重現，不是含 router-view/store 的真 app（工單「已知未驗」那格）。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import './helpers/dom.mjs' // happy-dom globals — must precede `vue`
import { createApp, defineComponent, h, nextTick } from 'vue'

// 真的 mount 在 DOM 裡（happy-dom）：SSR 一次成形，errorCaptured 之後不會重繪，
// 用 renderToString 量不到 fallback；client 端才是真實路徑。

function mount(child, onError) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp({ render: () => h(ErrorPanel, null, { default: () => h(child) }) })
  app.config.errorHandler = onError ?? (() => {})
  app.mount(el)
  return { el, app }
}

import { ErrorPanel, describeErrorForOperator, installErrorHandler } from '../src/components/errorPanel.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')

const Boom = defineComponent({
  name: 'Boom',
  setup() {
    throw new Error('Cannot read properties of null at /srv/anila/apps/csp-governance-ui/src/views/X.vue on host csp-internal.local')
  },
})

const Fine = defineComponent({ name: 'Fine', render: () => h('p', '正常內容') })

test('a throwing child renders as a readable error block, with no internals (①②③)', async () => {
  const { el } = mount(Boom)
  await nextTick()
  const html = el.innerHTML
  // ① 正向錨點：面板產生的文字，測試沒有自己寫進去
  assert.match(html, /role="alert"/u)
  assert.match(html, /系統發生錯誤/u)
  assert.match(html, /重新整理|聯繫維運/u)
  assert.match(html, /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/u)
  // ① 負向斷言：同一段 HTML 不洩內部資訊
  assert.doesNotMatch(html, /X\.vue/u)
  assert.doesNotMatch(html, /\/srv\//u)
  assert.doesNotMatch(html, /csp-internal/u)
  assert.doesNotMatch(html, /Cannot read properties/u)
  assert.doesNotMatch(html, /關聯碼|correlation/iu)
})

test('a healthy child renders untouched (the net does not alter the normal path)', async () => {
  const { el } = mount(Fine)
  await nextTick()
  const html = el.innerHTML
  assert.match(html, /正常內容/u)
  assert.doesNotMatch(html, /role="alert"/u)
})

test('describeErrorForOperator never leaks message, stack, paths or hosts', () => {
  const text = describeErrorForOperator(new Error('boom at /srv/x on host.internal'), new Date('2026-09-02T01:02:03Z'))
  assert.match(text, /系統發生錯誤/u)
  assert.match(text, /2026-09-02T01:02/u)
  assert.doesNotMatch(text, /boom|\/srv\/|host\.internal/u)
})

test('installErrorHandler wires app.config.errorHandler as the log channel (A)', () => {
  const logged = []
  const app = { config: {} }
  installErrorHandler(app, { error: (...args) => logged.push(args) })
  assert.equal(typeof app.config.errorHandler, 'function')
  app.config.errorHandler(new Error('x'), null, 'render')
  assert.equal(logged.length, 1)
})

test('main.js installs the handler and AppLayout wraps the routed view in the panel (wiring)', () => {
  const main = readFileSync(resolve(ROOT, 'src/main.js'), 'utf8')
  const layout = readFileSync(resolve(ROOT, 'src/components/layout/AppLayout.vue'), 'utf8')
  assert.match(main, /installErrorHandler\(app/u)
  assert.match(layout, /<ErrorPanel[\s\S]*<component :is="Component" \/>[\s\S]*<\/ErrorPanel>/u)
})
