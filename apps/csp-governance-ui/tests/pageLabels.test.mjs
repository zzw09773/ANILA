// 頁籤名稱要跟路由對得上。漏了一條，刊頭與分頁標題就退回顯示原始路徑。
// 只有 redirect 的路由（例如下線後的 classification-inventory）不必有名稱。
// 萬用字元的未知路徑也不在這張表裡。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

function read(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

function pageLabels(src) {
  const start = src.indexOf('const PAGE_LABELS = {')
  const end = src.indexOf('\n}', start)
  assert.ok(start >= 0 && end > start, '找不到 PAGE_LABELS')
  const labels = new Map()
  for (const match of src.slice(start, end).matchAll(/'([^']+)'\s*:\s*'([^']*)'/g)) {
    labels.set(match[1], match[2])
  }
  return labels
}

function sidebarLabels(src) {
  const labels = new Map()
  for (const match of src.matchAll(/path:\s*'([^']+)'\s*,\s*label:\s*'([^']+)'/g)) {
    labels.set(match[1], match[2])
  }
  return labels
}

function layoutPages(src) {
  const layoutStart = src.indexOf("component: () => import('../components/layout/AppLayout.vue')")
  const childrenStart = src.indexOf('children: [', layoutStart)
  const childrenEnd = src.indexOf('\n    ],', childrenStart)
  assert.ok(layoutStart >= 0 && childrenStart > layoutStart && childrenEnd > childrenStart, '找不到 AppLayout 子路由')
  const pages = []
  for (const chunk of src.slice(childrenStart, childrenEnd).split(/path:\s*'/).slice(1)) {
    const path = chunk.slice(0, chunk.indexOf("'"))
    const body = chunk.slice(chunk.indexOf("'") + 1)
    if (path.includes('pathMatch') || path.startsWith(':')) continue
    const redirectOnly = /redirect\s*:/.test(body) && !/component\s*:/.test(body)
    if (redirectOnly) continue
    pages.push(path === '' ? '/' : `/${path}`)
  }
  return pages
}

function labelFor(labels, path) {
  if (labels.has(path)) return labels.get(path)
  for (const key of labels.keys()) {
    if (key !== '/' && path.startsWith(key)) return labels.get(key)
  }
  return undefined
}

function concretePath(routePath) {
  const trimmed = routePath.replace(/\/:[^/]+/g, '')
  return trimmed || '/'
}

test('每個有畫面的路由都有頁籤名稱，而且跟側欄用字相同', () => {
  const labels = pageLabels(read('../src/components/layout/AppHeader.vue'))
  const sidebar = sidebarLabels(read('../src/components/layout/AppSidebar.vue'))
  const pages = layoutPages(read('../src/router/index.js'))
  assert.ok(labels.size >= 22, 'PAGE_LABELS 不該少於既有的對照')
  assert.ok(pages.includes('/skill-review'), '路由裡應該有 skill 審核')
  assert.equal(sidebar.get('/skill-review'), 'skill 審核')

  for (const routePath of pages) {
    const concrete = concretePath(routePath)
    const label = labelFor(labels, concrete)
    assert.ok(label, `${routePath} 沒有頁籤名稱，刊頭會顯示原始路徑`)
    if (sidebar.has(concrete)) {
      assert.equal(label, sidebar.get(concrete), `${routePath} 的頁籤應與側欄相同`)
    }
  }
})
