import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

function readRouterSource() {
  return readFileSync(new URL('../src/router/index.js', import.meta.url), 'utf8')
}

test('AppLayout 子路由保留既有路由並把未知路徑交給 NotFound', () => {
  const source = readRouterSource()
  const layoutStart = source.indexOf("component: () => import('../components/layout/AppLayout.vue')")
  const childrenStart = source.indexOf('children: [', layoutStart)
  const childrenEnd = source.indexOf('\n    ],', childrenStart)

  assert.ok(layoutStart >= 0, '找不到 AppLayout route')
  assert.ok(childrenStart > layoutStart && childrenEnd > childrenStart, '找不到 AppLayout 子路由')

  const childrenSource = source.slice(childrenStart, childrenEnd)
  assert.match(childrenSource, /path:\s*'platform-links'/, '已知的 platform-links 路由不可消失')
  assert.match(childrenSource, /path:\s*':pathMatch\(\.\*\)\*'/, 'AppLayout 子路由必須保留 catch-all')
  assert.match(childrenSource, /name:\s*'NotFound'/, 'catch-all 必須保留 NotFound route 名稱')
  assert.match(childrenSource, /NotFoundView\.vue/, 'catch-all 必須渲染 NotFoundView')
})
