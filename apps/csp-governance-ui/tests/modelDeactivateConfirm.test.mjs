// 停用模型前要告訴管理員有幾段對話還綁著它，而且不會自動改綁。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { deactivateConfirm } from '../src/utils/modelDeactivate.js'

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

test('有對話在用時，確認文案要寫出數量且說明不會自動改綁', () => {
  const gate = deactivateConfirm({
    id: 9,
    display_name: 'glm-5.3-flash',
    router_conversation_count: 33,
  })
  assert.equal(gate.danger, true)
  assert.match(gate.message, /33/)
  assert.match(gate.message, /對話/)
  assert.match(gate.message, /不會自動/)
})

test('沒有對話在用時，不捏造受影響數量', () => {
  const gate = deactivateConfirm({ id: 1, router_conversation_count: 0 })
  assert.equal(gate.message.includes('0'), false)
  assert.match(gate.message, /停用此模型/)
})

test('ModelsView 停用確認要採用對話數，而不是固定一句話', () => {
  const source = stripComments(readSource('views/ModelsView.vue'))
  const start = source.indexOf('async function handleDeactivate')
  const handler = source.slice(start, source.indexOf('async function handleActivate(id)', start))
  assert.ok(handler.length > 0, '找不到 handleDeactivate')
  assert.match(handler, /deactivateConfirm\(/)
  assert.match(handler, /router_conversation_count/)
})
