import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

test('製作 has a distinct route and navigation entry', () => {
  const app = source('./App.tsx')
  const header = source('./components/ProductHeader.tsx')
  assert.match(app, /path="\/outputs"/)
  assert.match(header, /label:\s*'製作'/)
  assert.match(header, /knowledgeHref\('\/outputs'\)/)
  assert.doesNotMatch(header, /產出中心/)
})

test('製作 does not invent a locally cached output count', () => {
  const page = source('./routes/OutputCenterPage.tsx')
  assert.doesNotMatch(page, /outputCount/)
  assert.doesNotMatch(page, /目前共保留/)
})

test('產出 cannot start with zero indexed documents', () => {
  const modal = source('./workspace/CommandModal.tsx')
  assert.match(modal, /if \(indexedDocs\.length === 0\)/)
  assert.match(modal, /disabled=\{busy \|\| indexedDocs\.length === 0\}/)
  assert.match(modal, /請先上傳資料並等待索引完成/)
})

test('logout clears the knowledge workspace and hard-redirects to configured login', () => {
  const store = source('./store/auth.ts')
  const dashboard = source('./routes/DashboardPage.tsx')
  assert.match(store, /logout:\s*async \(\) =>/)
  assert.match(store, /status:\s*'unauth'/)
  assert.match(dashboard, /await logout\(\)/)
  assert.match(dashboard, /window\.location\.replace\(loginHref\(\)\)/)
})

test('繪 tokens and the shared lockup are consumed by the knowledge workspace', () => {
  const tokens = source('./theme/tokens.ts')
  const theme = source('./theme/ThemeContext.tsx')
  const html = source('../index.html')
  const main = source('./main.tsx')
  const header = source('./components/ProductHeader.tsx')
  assert.match(tokens, /ink:\s*'#1B3A6B'/)
  assert.match(tokens, /signal:\s*'#1A5BB8'/)
  assert.match(tokens, /canvas:\s*'#F2F5F9'/)
  assert.match(tokens, /paper:\s*'#FFFFFF'/)
  assert.match(tokens, /bg:\s*NAMED\.canvas/)
  assert.match(tokens, /accent:\s*NAMED\.signal/)
  assert.match(theme, /theme:\s*'light'/)
  assert.match(theme, /return v === 'light' \|\| v === 'dark' \? v : 'light'/)
  assert.match(html, /ANILA · 院內 AI 工作平台/)
  assert.doesNotMatch(html, /ANILA LM/)
  assert.doesNotMatch(html, /#6361E0|#0b0d10|#7C4DFF/)
  assert.match(main, /shared\/tokens\.css/)
  assert.match(header, /ANILA_LOCKUP/)
  assert.match(header, /系統管理/)
})
