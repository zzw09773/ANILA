// 畫面文案裡指路的位址,必須真的在 router 裡註冊過。
//
// 【原前提（2026-07-31，現已失效；保留作變更紀錄）】
// ServiceAccessView 與 DashboardView 的空狀態都叫操作者「去 /admin/platform-links
// 註冊」,但 router 註冊的是 `platform-links`(父層 path='/'),而且沒有
// catch-all —— 照著走只會得到空白畫面。這是當時的測試前提,不是目前事實。
//
// 【前提已改（2026-08-15）】
// router 現在在 AppLayout route 的 children 註冊 catch-all,沿用父層的登入守衛；
// 未知路徑會交給 NotFoundView,不再渲染空白畫面。`/admin/platform-links` 仍只
// 存在於 AppHeader 的麵包屑顯示對照表(segmentMap),那是給人看的字串,不是位址。
//
// 這個測試把「文案提到的治理路徑」和「router 真的有的路徑」對起來。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

/** router/index.js 裡所有 `path: '...'`,還原成絕對路徑(父層是 '/')。 */
function registeredPaths() {
  const src = readSource('router/index.js')
  const paths = new Set()
  for (const m of src.matchAll(/path:\s*'([^']*)'/g)) {
    const p = m[1]
    paths.add(p.startsWith('/') ? p : '/' + p)
  }
  return paths
}

test('router 有註冊 /platform-links', () => {
  assert.ok(registeredPaths().has('/platform-links'))
})

test('router 未註冊 /admin/platform-links,未知路徑由 AppLayout 子路由顯示 NotFound', () => {
  const paths = registeredPaths()
  assert.equal(paths.has('/admin/platform-links'), false)
  const src = readSource('router/index.js')
  const layoutStart = src.indexOf("component: () => import('../components/layout/AppLayout.vue')")
  const childrenStart = src.indexOf('children: [', layoutStart)
  const childrenEnd = src.indexOf('\n    ],', childrenStart)
  assert.ok(layoutStart >= 0, '找不到 AppLayout route')
  assert.ok(childrenStart > layoutStart && childrenEnd > childrenStart, '找不到 AppLayout 子路由')
  const layoutSource = src.slice(layoutStart, childrenEnd)
  const childrenSource = src.slice(childrenStart, childrenEnd)
  assert.match(layoutSource, /meta:\s*\{\s*requiresAuth:\s*true\s*\}/, 'AppLayout route 必須保留登入守衛')
  assert.match(childrenSource, /path:\s*':pathMatch\(\.\*\)\*'/, 'AppLayout 子路由必須保留 catch-all')
  assert.match(childrenSource, /NotFoundView\.vue/, 'catch-all 必須渲染 NotFoundView')
})

test('空狀態文案指的路徑,router 裡要找得到', () => {
  const paths = registeredPaths()
  for (const view of ['views/ServiceAccessView.vue', 'views/DashboardView.vue']) {
    const src = stripComments(readSource(view))
    for (const m of src.matchAll(/(\/[a-z0-9-]+(?:\/[a-z0-9-]+)*)\s*(?:註冊|綁定)/g)) {
      assert.ok(
        paths.has(m[1]),
        `${view} 的文案指向 ${m[1]},但 router 沒有這個 path`,
      )
    }
  }
})
