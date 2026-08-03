// ANILA LM release gate — 治理中心不顯示關閉中的入口。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// utils 是 plain ESM；用動態 import。
const {
  ANILA_LM_LINK_VISIBLE,
  isAnilaLmPlatformLink,
  filterPlatformLinksForRelease,
  isReleaseGateClosedFor,
  RELEASE_GATE_BADGE,
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

test('isReleaseGateClosedFor marks the row instead of hiding it', () => {
  const link = { id: 1, name: 'ANILA LM', url: '/anilalm' }
  assert.equal(isReleaseGateClosedFor(link), !ANILA_LM_LINK_VISIBLE)
  assert.equal(isReleaseGateClosedFor({ id: 2, name: 'n8n', url: '/n8n' }), false)
  assert.equal(typeof RELEASE_GATE_BADGE, 'string')
  assert.ok(RELEASE_GATE_BADGE.length > 0)
})

// 使用者面 vs 管理面：閘門關的是「可用」，不是「可管理」。
// 2026-08-02：服務登記在前端濾掉整列，連編輯／停用／刪除按鈕一起消失，
// 管理員看不到也管不動，要停用只能手打 API。那不是 release gate。
test('使用者面（儀表板）過濾，管理面（服務登記／服務存取）不過濾只標記', () => {
  const dashboard = readFileSync(
    new URL('../src/views/DashboardView.vue', import.meta.url), 'utf8')
  assert.match(
    dashboard, /filterPlatformLinksForRelease/,
    'DashboardView（使用者面的外部工具卡）必須濾掉關著的入口')

  for (const rel of [
    '../src/views/PlatformLinksView.vue',
    '../src/views/ServiceAccessView.vue',
  ]) {
    const src = readFileSync(new URL(rel, import.meta.url), 'utf8')
    assert.doesNotMatch(
      src, /filterPlatformLinksForRelease/,
      `${rel} 是管理面，不得把整列濾掉（管理員會因此管不動它）`)
    assert.match(
      src, /isReleaseGateClosedFor/,
      `${rel} 必須把關著的那一列標出來`)
  }
})

test('沒有任何一頁硬編可點的 /anilalm 導覽', () => {
  for (const rel of [
    '../src/views/DashboardView.vue',
    '../src/views/PlatformLinksView.vue',
    '../src/views/ServiceAccessView.vue',
  ]) {
    const src = readFileSync(new URL(rel, import.meta.url), 'utf8')
    // 註解裡的歷史說明可留，只看真的會渲染出去的碼。
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
