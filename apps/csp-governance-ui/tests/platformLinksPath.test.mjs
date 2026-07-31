// 畫面文案裡指路的位址,必須真的在 router 裡註冊過。
//
// ServiceAccessView 與 DashboardView 的空狀態都叫操作者「去 /admin/platform-links
// 註冊」,但 router 註冊的是 `platform-links`(父層 path='/'),而且沒有
// catch-all —— 照著走只會得到空白畫面。`/admin/platform-links` 只存在於
// AppHeader 的麵包屑顯示對照表(segmentMap),那是給人看的字串,不是位址。
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

test('router 沒有註冊 /admin/platform-links,也沒有 catch-all', () => {
  const paths = registeredPaths()
  assert.equal(paths.has('/admin/platform-links'), false)
  const src = readSource('router/index.js')
  assert.doesNotMatch(src, /path:\s*'\/:pathMatch/, '若日後加了 catch-all,本測試的前提要重寫')
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
