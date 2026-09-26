import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const agents = readFileSync(new URL('../src/views/DeveloperAgentsView.vue', import.meta.url), 'utf8')
const guide = readFileSync(new URL('../src/views/DeveloperGuideView.vue', import.meta.url), 'utf8')

test('註冊與編輯都說明底層模型是實際使用的模型，更換要重新送審', () => {
  const hint = '此 agent 實際使用的模型；更換需重新送審'
  const register = agents.indexOf('label="基礎模型"')
  const edit = agents.indexOf('label="基礎模型"', register + 1)
  assert.ok(register >= 0 && edit > register)
  assert.match(agents.slice(register, edit), new RegExp(hint))
  assert.match(agents.slice(edit), new RegExp(hint))
  assert.doesNotMatch(agents, /用量歸屬對象/)
})

test('開發指南寫明上線用量算提問者，開發者金鑰只做 lab 測試', () => {
  assert.match(guide, /提問者/)
  assert.match(guide, /LLM_API_KEY/)
  assert.match(guide, /測試/)
})
