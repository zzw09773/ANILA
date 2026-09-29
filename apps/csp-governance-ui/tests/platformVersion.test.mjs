import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { platformVersionLabel } from '../src/utils/platformVersion.js'

test('platformVersionLabel shows the baked version or 未知', () => {
  assert.equal(platformVersionLabel('2026.09.29-1'), '2026.09.29-1')
  assert.equal(platformVersionLabel('  2026.09.29-2  '), '2026.09.29-2')
  assert.equal(platformVersionLabel(''), '未知')
  assert.equal(platformVersionLabel('   '), '未知')
  assert.equal(platformVersionLabel(null), '未知')
})

test('dashboard asks the platform version endpoint', () => {
  const src = readFileSync(
    new URL('../src/views/DashboardView.vue', import.meta.url),
    'utf8',
  )
  assert.match(src, /\/api\/platform-version/)
  assert.match(src, /版本 \{\{ platformVersion \}\}/)
  assert.match(src, /platformVersionLabel/)
})
