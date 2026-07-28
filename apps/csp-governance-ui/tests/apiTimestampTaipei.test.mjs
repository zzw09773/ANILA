/**
 * Fixed-epoch: API timestamps with an explicit UTC offset render as Taipei wall clock.
 *
 * Regression for naive ISO strings (no offset) being parsed as local time —
 * which made UTC 11:19 look like Taipei 11:19 in views that pin
 * timeZone: 'Asia/Taipei'.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

test('API created_at with UTC offset localizes to Taipei 19:19:42', () => {
  const apiUtc = '2026-07-27T11:19:42.679819+00:00'
  const rendered = new Date(apiUtc).toLocaleString('zh-TW', {
    timeZone: 'Asia/Taipei',
    hour12: false,
  })
  assert.match(rendered, /19:19:42/)
})

test('Z suffix is equivalent to +00:00 for Taipei display', () => {
  const rendered = new Date('2026-07-27T11:19:42.679819Z').toLocaleString(
    'zh-TW',
    { timeZone: 'Asia/Taipei', hour12: false },
  )
  assert.match(rendered, /19:19:42/)
})
