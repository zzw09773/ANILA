// ANILA LM release gate — 治理中心不顯示關閉中的入口。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// utils 是 plain ESM；用動態 import。
const {
  ANILA_LM_LINK_VISIBLE,
  isAnilaLmPlatformLink,
  filterPlatformLinksForRelease,
} = await import('../src/utils/anilalmReleaseGate.js')

test('release gate flag defaults to hidden for this freeze', () => {
  assert.equal(ANILA_LM_LINK_VISIBLE, false)
})

test('isAnilaLmPlatformLink matches name and /anilalm url', () => {
  assert.equal(isAnilaLmPlatformLink({ name: 'ANILA LM', url: '/anilalm' }), true)
  assert.equal(isAnilaLmPlatformLink({ name: 'other', entry_url: 'https://x/anilalm/' }), true)
  assert.equal(isAnilaLmPlatformLink({ name: 'n8n', url: '/n8n' }), false)
  assert.equal(isAnilaLmPlatformLink(null), false)
})

test('filterPlatformLinksForRelease drops ANILA LM when gate is closed', () => {
  const links = [
    { id: 1, name: 'ANILA LM', url: '/anilalm' },
    { id: 2, name: 'n8n', url: '/n8n' },
  ]
  const filtered = filterPlatformLinksForRelease(links)
  if (ANILA_LM_LINK_VISIBLE) {
    assert.equal(filtered.length, 2)
  } else {
    assert.deepEqual(filtered.map((l) => l.name), ['n8n'])
  }
})

test('DashboardView / PlatformLinksView / ServiceAccessView apply the filter', () => {
  for (const rel of [
    '../src/views/DashboardView.vue',
    '../src/views/PlatformLinksView.vue',
    '../src/views/ServiceAccessView.vue',
  ]) {
    const src = readFileSync(new URL(rel, import.meta.url), 'utf8')
    assert.match(
      src,
      /filterPlatformLinksForRelease/,
      `${rel} must filter ANILA LM via filterPlatformLinksForRelease`,
    )
    // 不得再硬編可點的 /anilalm 導覽（註解裡的歷史說明可留）
    const code = src
      .replace(/<!--[\s\S]*?-->/g, '')
      .split('\n')
      .filter((line) => !line.trimStart().startsWith('//'))
      .join('\n')
    assert.doesNotMatch(
      code,
      /href\s*=\s*['"][^'"]*\/anilalm/,
      `${rel} must not hard-code an /anilalm href while the gate is closed`,
    )
  }
})
