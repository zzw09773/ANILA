import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { DEFAULT_MODEL_MAX_CONCURRENT, MODEL_LIST_REFRESH_MS, modelQueueHot } from '../src/utils/modelConcurrency.js'

function readView() {
  return readFileSync(new URL('../src/views/ModelsView.vue', import.meta.url), 'utf8')
}

test('chat menu uses rounded token badges and wraps phrases rather than characters', () => {
  const view = readView()
  const cell = view.match(/<td class="chat-menu-cell">([\s\S]*?)<\/td>/)?.[1]
  assert.ok(cell)
  assert.match(cell, /role="group" aria-label="對話選單標籤"/)
  assert.equal((cell.match(/<TermBadge\b/g) || []).length, 5)
  for (const label of ['平台入口', 'Router', '★ 全院預設', '★ 主簡報', '★ 主 embedding']) {
    assert.ok(cell.includes(label))
  }
  const fallback = cell.match(/<span v-if="([^"]+)" class="cell-meta">—<\/span>/)?.[1]
  assert.ok(fallback)
  const showFallback = new Function('model', `return ${fallback}`)
  assert.equal(showFallback({ name: 'anila-router' }), false)
  assert.equal(showFallback({ name: 'router-only', router_enabled: true }), false)
  assert.equal(showFallback({ name: 'plain' }), true)
  assert.doesNotMatch(view, /primary-pill/)
  assert.match(view, /\.chat-menu-cell\s*\{[^}]*min-width:\s*12rem/)
  assert.match(view, /\.chat-menu-badges\s*\{[^}]*flex-wrap:\s*wrap/)
  assert.match(view, /\.chat-menu-badges :deep\(\.term-badge\)\s*\{[^}]*white-space:\s*nowrap/)
  assert.match(cell, /v-if="canEditAudience\(model\)"/)
  assert.match(cell, /<button type="button"[^>]*@click="openEditModal\(model, \{ focusGrants: true \}\)">可使用對象<\/button>/)
  const css = readFileSync(new URL('../src/assets/styles/main.css', import.meta.url), 'utf8')
  assert.match(css, /\.term-badge\s*\{[^}]*border-radius:\s*var\(--r-pill\)/)
  assert.match(css, /\.term-badge--warn\s*\{[^}]*var\(--c-warn-soft\)/)
})

test('model names retain readable width with local keyboard-scrollable table', () => {
  const view = readView()
  assert.match(view, /class="models-table-scroll" role="region" aria-label="已註冊模型表格，可水平捲動" tabindex="0"/)
  assert.match(view, /<th class="model-name-cell">名稱<\/th>/)
  assert.match(view, /<td class="model-name-cell">/)
  assert.match(view, /\.model-name-cell\s*\{[^}]*min-width:\s*16rem/)
  assert.match(view, /\.models-table-scroll\s*\{[^}]*overflow-x:\s*auto/)
  assert.match(view, /\.models-table\s*\{[^}]*min-width:\s*1400px/)
})

test('queue highlight is only when someone is waiting', () => {
  assert.equal(DEFAULT_MODEL_MAX_CONCURRENT, 16)
  assert.equal(MODEL_LIST_REFRESH_MS, 15000)
  assert.equal(modelQueueHot(1), true)
  assert.equal(modelQueueHot(0), false)
  assert.equal(modelQueueHot(null), false)
  assert.equal(modelQueueHot(undefined), false)
})

test('models page prefills 16 and shows live inflight and queue', () => {
  const view = readView()
  assert.match(view, /DEFAULT_MODEL_MAX_CONCURRENT/)
  assert.match(view, /處理中/)
  assert.match(view, /排隊中/)
  assert.match(view, /modelQueueHot\(model\.queue_length\)/)
  assert.match(view, /MODEL_LIST_REFRESH_MS/)
  assert.match(view, /onUnmounted/)
  assert.match(view, /clearInterval/)
  assert.match(view, /setInterval/)
})
