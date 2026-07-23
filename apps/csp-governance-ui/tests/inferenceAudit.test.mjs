import test from 'node:test'
import assert from 'node:assert/strict'

import {
  actionLabel,
  buildInferenceAuditParams,
  formatMetadata,
  localInputToIso,
  statusLabel,
  statusVariant,
  truncateDetail,
} from '../src/utils/inferenceAudit.js'

test('buildInferenceAuditParams omits empty filters and applies defaults', () => {
  const params = buildInferenceAuditParams({
    username: '  ',
    ip: '',
    action: '',
    q: '  hello  ',
    from: '',
    to: '',
    limit: 50,
    offset: 0,
  })

  assert.deepEqual(Object.keys(params).sort(), ['limit', 'offset', 'q'].sort())
  assert.equal(params.q, 'hello')
  assert.equal(params.limit, 50)
  assert.equal(params.offset, 0)
})

test('buildInferenceAuditParams serializes filters and caps limit', () => {
  const params = buildInferenceAuditParams({
    username: ' alice ',
    ip: ' 10.0.0.1 ',
    action: 'inference.chat',
    q: '機密',
    from: '2026-07-01T00:00',
    to: '2026-07-23T23:59',
    limit: 900,
    offset: 100,
  })

  assert.equal(params.username, 'alice')
  assert.equal(params.ip, '10.0.0.1')
  assert.equal(params.action, 'inference.chat')
  assert.equal(params.q, '機密')
  assert.equal(params.limit, 500)
  assert.equal(params.offset, 100)
  assert.match(params.from, /Z$/)
  assert.match(params.to, /Z$/)
  assert.ok(!Number.isNaN(Date.parse(params.from)))
  assert.ok(!Number.isNaN(Date.parse(params.to)))
})

test('buildInferenceAuditParams export mode drops pagination', () => {
  const params = buildInferenceAuditParams(
    {
      username: 'bob',
      action: 'inference.agent',
      limit: 50,
      offset: 25,
    },
    { includePagination: false },
  )

  assert.deepEqual(params, {
    username: 'bob',
    action: 'inference.agent',
  })
})

test('localInputToIso returns undefined for blank input', () => {
  assert.equal(localInputToIso(''), undefined)
  assert.equal(localInputToIso(null), undefined)
  assert.equal(localInputToIso('   '), undefined)
})

test('action and status helpers use zh-TW labels', () => {
  assert.equal(actionLabel('inference.rag_query'), 'RAG 查詢')
  assert.equal(actionLabel('inference.embed'), '向量嵌入')
  assert.equal(actionLabel('unknown.x'), 'unknown.x')
  assert.equal(statusLabel('success'), '成功')
  assert.equal(statusLabel('denied'), '拒絕')
  assert.equal(statusLabel('error'), '錯誤')
  assert.equal(statusVariant('success'), 'ok')
  assert.equal(statusVariant('denied'), 'warn')
  assert.equal(statusVariant('error'), 'danger')
})

test('ISO created_at with UTC offset localizes correctly in Asia/Taipei', () => {
  // API now emits tz-aware UTC; browsers must not treat naive as local.
  const iso = '2026-07-23T04:00:00+00:00'
  const rendered = new Date(iso).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei' })
  assert.match(rendered, /12:00:00/)
})

test('truncateDetail and formatMetadata', () => {
  assert.equal(truncateDetail(null), '—')
  assert.equal(truncateDetail('short'), 'short')
  assert.equal(truncateDetail('a'.repeat(90)).endsWith('…'), true)
  assert.equal(formatMetadata(null), null)
  assert.equal(formatMetadata('{"a":1}'), '{\n  "a": 1\n}')
  assert.equal(formatMetadata('not-json'), 'not-json')
})
