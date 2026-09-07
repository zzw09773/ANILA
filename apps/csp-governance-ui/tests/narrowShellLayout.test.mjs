// 窄視窗治理中心不得把側欄堆在主內容上面（main 會被壓成一條縫），
// 頂欄按鈕也不得被 flex 壓成直排字。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const layout = readFileSync(resolve(ROOT, 'src/components/layout/AppLayout.vue'), 'utf8')
const header = readFileSync(resolve(ROOT, 'src/components/layout/AppHeader.vue'), 'utf8')
const sidebar = readFileSync(resolve(ROOT, 'src/components/layout/AppSidebar.vue'), 'utf8')
const nav = readFileSync(resolve(ROOT, 'src/composables/useShellNav.js'), 'utf8')
const css = readFileSync(resolve(ROOT, 'src/assets/styles/main.css'), 'utf8')

test('narrow layout uses an overlay drawer, not a stacked sidebar row', () => {
  assert.match(layout, /provideShellNav/)
  assert.match(layout, /shell__backdrop/)
  assert.match(layout, /translateX\(-105%\)/)
  assert.match(layout, /sidenav\.is-open/)
  assert.match(layout, /position:\s*absolute/)
})

test('header keeps action labels on one line and exposes a menu button', () => {
  assert.match(header, /topbar__menu/)
  assert.match(header, /white-space:\s*nowrap/)
  assert.match(header, /flex-shrink:\s*0/)
  assert.match(header, /帳號選單/)
  assert.match(header, /useShellNav/)
})

test('sidebar can slide open and closes on navigate', () => {
  assert.match(sidebar, /gov-sidenav/)
  assert.match(sidebar, /is-open/)
  assert.match(sidebar, /nav\.close\(\)/)
})

test('narrow breakpoint is 900px and compact account menu is 520px', () => {
  assert.match(nav, /max-width: 900px/)
  assert.match(nav, /max-width: 520px/)
})

test('tables can scroll horizontally and pin the last (action) column', () => {
  assert.match(css, /min-width:\s*36rem/)
  assert.match(css, /position:\s*sticky/)
  assert.match(css, /th:last-child/)
  assert.match(css, /white-space:\s*nowrap/)
})

test('status bar stays inside the viewport and is not eaten by overflow', () => {
  const tokens = readFileSync(resolve(ROOT, 'src/assets/styles/tokens.css'), 'utf8')
  const status = readFileSync(resolve(ROOT, 'src/components/layout/AppStatusBar.vue'), 'utf8')
  assert.match(tokens, /--shell-statusbar-h:\s*36px/)
  assert.match(layout, /minmax\(0,\s*1fr\)/)
  assert.match(layout, /height:\s*100%/)
  assert.doesNotMatch(layout, /height:\s*100dvh/)
  assert.match(status, /overflow:\s*hidden/)
  assert.match(css, /html[\s\S]*overflow:\s*hidden/)
})

test('middle pane cannot paint over the status bar', () => {
  const sidebar = readFileSync(resolve(ROOT, 'src/components/layout/AppSidebar.vue'), 'utf8')
  assert.match(layout, /\.shell__body[\s\S]*grid-template-rows:\s*minmax\(0,\s*1fr\)/)
  assert.match(layout, /\.shell__body[\s\S]*overflow:\s*hidden/)
  assert.match(layout, /\.shell__main[\s\S]*min-height:\s*0/)
  assert.match(sidebar, /min-height:\s*0/)
  assert.doesNotMatch(sidebar, /height:\s*100%/)
})
