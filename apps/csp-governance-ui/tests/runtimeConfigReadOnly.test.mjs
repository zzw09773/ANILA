// Task 1 — governance UI must not present a save control for runtime_config.
//
// PATCH /api/agents/{id}/runtime-config already returns 410. The expensive
// lesson is a button that makes the admin believe something was saved when
// it was not. This test pins the honest read-only surface.

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

test('AgentRuntimeConfigView 沒有儲存／清除覆寫控制項', () => {
  const src = readSource('views/AgentRuntimeConfigView.vue')
  assert.doesNotMatch(src, /儲存執行設定/)
  assert.doesNotMatch(src, /handleSave/)
  assert.doesNotMatch(src, /handleClear/)
  assert.doesNotMatch(src, /setAgentRuntimeConfig/)
  assert.match(src, /runtime-config-retired-banner/)
  assert.match(src, /唯讀/)
  assert.match(src, /未出貨/)
})

test('agents.js 不再匯出會發 PATCH 的 setAgentRuntimeConfig', () => {
  const src = readSource('api/agents.js')
  assert.doesNotMatch(src, /export const setAgentRuntimeConfig/)
  assert.doesNotMatch(src, /\.patch\(`\/api\/agents\/\$\{id\}\/runtime-config`/)
  assert.match(src, /export const getAgentRuntimeConfig/)
})
