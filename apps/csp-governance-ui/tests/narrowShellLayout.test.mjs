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

test('desktop sidebar boundary is a separate resizer, not a drawer control', () => {
  assert.match(layout, /role="separator"/)
  assert.match(layout, /aria-label="調整側欄寬度"/)
  assert.match(layout, /v-if="!narrow"/)
  assert.match(layout, /shell__resizer/)
  assert.match(layout, /width:\s*6px/)
  assert.match(layout, /box-sizing:\s*content-box/)
  assert.match(layout, /flush:\s*'sync'/)
  assert.match(layout, /shouldEndSidebarDrag\(next\)/)
  assert.match(layout, /if \(shouldEndSidebarDrag\(next\)\) unbindPointer\.release\?\.\(\)/)
  assert.match(layout, /--c-accent/)
  assert.match(layout, /forced-colors:\s*active/)
  assert.match(layout, /Highlight/)
  assert.doesNotMatch(layout, /localStorage|sessionStorage/)
})

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

test('brand mark is a native dashboard link named 回到儀表板', () => {
  const brand = header.match(/<RouterLink\b[\s\S]*?<\/RouterLink>/)
  assert.ok(brand, 'TermLogo must sit inside a RouterLink')
  assert.match(brand[0], /class="topbar__brand"/)
  assert.match(brand[0], /:to="\{ name: 'Dashboard' \}"/)
  assert.match(brand[0], /aria-label="回到儀表板"/)
  assert.match(brand[0], /<TermLogo/)
  assert.match(brand[0], /@click="nav\.close\(\)"/)
  assert.doesNotMatch(brand[0], /preventDefault|stopPropagation|metaKey|ctrlKey|shiftKey|altKey|href=/)
  const style = header.match(/\.topbar__brand\s*\{[^}]*\}/)
  assert.ok(style, 'brand link needs its own hit area')
  assert.match(style[0], /min-width:\s*24px/)
  assert.match(style[0], /min-height:\s*24px/)
  assert.match(style[0], /white-space:\s*nowrap/)
  assert.doesNotMatch(style[0], /background:|border:|box-shadow:/)
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

test('login dark-theme brand filters stay on the logo, not html', () => {
  const login = readFileSync(resolve(ROOT, 'src/views/LoginView.vue'), 'utf8')
  assert.doesNotMatch(login, /:global\(\[data-theme/)
  assert.match(login, /:root\[data-theme="dark"\] \.login__brand-video-wrap/)
  assert.match(login, /:root\[data-theme="dark"\] \.login__brand-video/)
})
