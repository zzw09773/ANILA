// 擁有者 2026-09-02 14:17：「我看到都是全白畫面欸」。
// 治理中心的 AppLayout 用 <transition mode="out-in"> 包 <ErrorPanel :key="$route.fullPath">。
// 整頁重新載入看得到內容，但點側欄換頁後 main 只剩 <!---->，永遠空白。
// 根因：ErrorPanel 的 render 直接回傳 slots.default() 這個陣列 → 元件根節點是 Fragment；
// Vue 的 renderer 移除 Fragment 時不會跑 transition.leave，out-in 模式等的 afterLeave
// 永遠不來，isLeaving 卡在 true，之後只畫佔位註解。
// 不變式：換掉 ErrorPanel 的內容（key 改變）之後，新內容一定要出現在 DOM 裡。
import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/dom.mjs' // happy-dom globals — must precede `vue`
import { createApp, defineComponent, h, nextTick, ref, Transition } from 'vue'

import { ErrorPanel } from '../src/components/errorPanel.js'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const PageA = defineComponent({ name: 'PageA', render: () => h('section', { id: 'page-a' }, '儀表板') })
const PageB = defineComponent({ name: 'PageB', render: () => h('section', { id: 'page-b' }, '使用者') })

test('inside <Transition mode="out-in">, swapping the keyed ErrorPanel shows the new page (not a bare comment)', async () => {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const current = ref('a')
  const app = createApp({
    render: () =>
      h(Transition, { mode: 'out-in' }, {
        default: () =>
          h(ErrorPanel, { key: current.value }, { default: () => h(current.value === 'a' ? PageA : PageB) }),
      }),
  })
  app.mount(el)
  await nextTick()
  assert.ok(el.querySelector('#page-a'), 'first page renders on mount')

  current.value = 'b'
  await nextTick()
  // 讓 transition 的 nextFrame／transitionend fallback 跑完
  for (let i = 0; i < 5 && !el.querySelector('#page-b'); i++) await sleep(30)

  assert.ok(el.querySelector('#page-b'), `after navigation the new page must be in the DOM, got: ${el.innerHTML}`)
  assert.equal(el.querySelector('#page-a'), null, 'old page is gone')
  app.unmount()
})
