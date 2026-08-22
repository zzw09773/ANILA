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
  const studio = source('./workspace/WSStudio.tsx')
  const sources = source('./workspace/notebookSources.ts')
  const sidebar = source('./workspace/WSSidebar.tsx')
  assert.match(sources, /先上傳或等索引完成/)
  assert.match(sources, /請在左側至少勾一份來源/)
  assert.match(sources, /isIndexedSource/)
  assert.match(sources, /詳細簡報/)
  assert.match(sources, /口講用短頁/)
  assert.match(sources, /documentIds: input.sources.map/)
  assert.match(studio, /startNotebookSlides/)
  assert.match(studio, /還沒有產出。從製作開始。/)
  assert.match(sidebar, /sourceCountLabel/)
  assert.match(sidebar, /toggleSource/)
  assert.match(modal, /使用左側已選/)
  const chat = source('./workspace/WSChat.tsx')
  assert.match(chat, /sourceCountLabel/)
  assert.match(studio, /filter\(\(f\) => f\.k !== 'slides'\)/)
  assert.doesNotMatch(studio, /自動觸發 download/)
})

test('簡報 preview can regenerate one slide and open the source chunk', () => {
  const viewer = source('./workspace/ArtifactViewer.tsx')
  const api = source('./api/studio.ts')
  const workspace = source('./routes/WorkspacePage.tsx')
  assert.match(viewer, /重做這一頁/)
  assert.match(viewer, /本頁來源/)
  assert.match(viewer, /regenerateSlide/)
  assert.match(viewer, /setFocusSourceId/)
  assert.match(viewer, /這一頁沒有對到段落/)
  assert.doesNotMatch(viewer, /sources\.slice\(0, 3\)/)
  assert.match(api, /slides\/\$\{slideNumber\}\/regenerate/)
  assert.match(api, /document_ids: input.documentIds/)
  assert.match(workspace, /searchParams.get\('studio'\) === '1'/)
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
