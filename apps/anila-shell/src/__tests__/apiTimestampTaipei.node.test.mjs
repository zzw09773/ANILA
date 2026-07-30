/**
 * Fixed-epoch: API timestamps with an explicit UTC offset render as Taipei wall clock.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

test('API created_at with UTC offset localizes to Taipei 16:00', () => {
  const apiUtc = '2026-07-30T08:00:00.000000+00:00'
  const rendered = new Date(apiUtc).toLocaleString('zh-TW', {
    timeZone: 'Asia/Taipei',
    hour12: false,
  })
  assert.match(rendered, /16:00:00/)
})

test('Z suffix is equivalent to +00:00 for Taipei display', () => {
  const rendered = new Date('2026-07-30T08:00:00.000000Z').toLocaleString(
    'zh-TW',
    { timeZone: 'Asia/Taipei', hour12: false },
  )
  assert.match(rendered, /16:00:00/)
})
