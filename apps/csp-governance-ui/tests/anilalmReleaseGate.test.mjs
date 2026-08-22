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

test('knowledge and output workspace is visible in this release', () => {
  assert.equal(ANILA_LM_LINK_VISIBLE, true)
})

test('isAnilaLmPlatformLink uses the backend code, not mutable prose or URL', () => {
  assert.equal(
    isAnilaLmPlatformLink({
      name: '後端改寫後的服務名稱',
      url: '/完全不同的入口',
      release_gate_code: 'anila_lm',
    }),
    true,
  )
  assert.equal(isAnilaLmPlatformLink({ name: 'ANILA LM', url: '/anilalm' }), false)
  assert.equal(isAnilaLmPlatformLink({ name: 'n8n', url: '/n8n' }), false)
  assert.equal(isAnilaLmPlatformLink(null), false)
})

test('filterPlatformLinksForRelease keeps ANILA LM when gate is open', () => {
  const links = [
    { id: 1, name: '改過名字', url: '/改過入口', release_gate_code: 'anila_lm' },
    { id: 2, name: 'n8n', url: '/n8n', release_gate_code: null },
  ]
  const filtered = filterPlatformLinksForRelease(links)
  assert.equal(filtered.length, 2)
})

test('isReleaseGateClosedFor does not mark the open row', () => {
  const link = { id: 1, name: '改過名字', url: '/改過入口', release_gate_code: 'anila_lm' }
  assert.equal(isReleaseGateClosedFor(link), !ANILA_LM_LINK_VISIBLE)
  assert.equal(isReleaseGateClosedFor({ id: 2, name: 'n8n', url: '/n8n', release_gate_code: null }), false)
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

test('治理中心由服務登記提供入口，不硬編部署位址', () => {
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
      `${rel} must obtain the knowledge-workspace URL from service registration`,
    )
  }
})
