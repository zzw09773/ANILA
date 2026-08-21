// 服務登記的圖示欄必須是伺服器清單的下拉,不能再是自由文字。
// 自由文字 + placeholder `workflow` 是假控制項:填了、存了、首頁卡片不畫。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

function readView() {
  return readFileSync(new URL('../src/views/PlatformLinksView.vue', import.meta.url), 'utf8')
}

function readApi() {
  return readFileSync(new URL('../src/api/platformLinks.js', import.meta.url), 'utf8')
}

test('圖示欄是 select,不是自由文字 input', () => {
  const src = readView()
  assert.match(src, /<select v-model="form\.icon"/)
  assert.doesNotMatch(src, /<input[^>]*v-model="form\.icon"/)
  assert.doesNotMatch(src, /placeholder="workflow"/)
})

test('圖示清單來自伺服器 GET /api/platform-links/icons', () => {
  const view = readView()
  const api = readApi()
  assert.match(view, /清單來自伺服器 GET \/api\/platform-links\/icons/)
  assert.match(api, /\/api\/platform-links\/icons/)
  assert.match(view, /listPlatformLinkIcons/)
})

test('儲存失敗 toast 走 apiDetail，不把 FastAPI 422 陣列顯示成 [object Object]', () => {
  const src = readView()
  assert.match(src, /function apiDetail\(/)
  assert.match(src, /toast\(apiDetail\(e, '儲存失敗'\)/)
  assert.doesNotMatch(src, /toast\(e\.response\?\.data\?\.detail \|\| '儲存失敗'/)
})

function braceBlockAfter(src, needle) {
  const idx = src.indexOf(needle)
  assert.ok(idx >= 0, `missing marker ${needle}`)
  const brace = src.indexOf('{', idx)
  assert.ok(brace >= 0, `missing { after ${needle}`)
  let depth = 0
  for (let i = brace; i < src.length; i++) {
    if (src[i] === '{') depth += 1
    else if (src[i] === '}') {
      depth -= 1
      if (depth === 0) return src.slice(brace, i + 1)
    }
  }
  assert.fail(`unclosed { after ${needle}`)
}

function quotedStrings(block) {
  return new Set([...block.matchAll(/["']([A-Za-z][\w-]*)["']/g)].map((m) => m[1]))
}

function jsIdentKeys(block) {
  return new Set([...block.matchAll(/^\s*([A-Za-z_][\w-]*)\s*:/gm)].map((m) => m[1]))
}

test('後端允許清單 / SPA 對照表 / 儀表板 GLYPHS / 後端測試鏡像 四組 key 必須鎖步', () => {
  const backend = readFileSync(new URL('../../../services/csp/app/schemas/service_icon.py', import.meta.url), 'utf8')
  const spa = readFileSync(new URL('../../anila-shell/src/services.jsx', import.meta.url), 'utf8')
  const dashboard = readFileSync(new URL('../src/components/dashboard/PlatformCard.vue', import.meta.url), 'utf8')
  const expected = readFileSync(new URL('../../../services/csp/tests/test_platform_link_icons.py', import.meta.url), 'utf8')

  const sources = {
    'services/csp/app/schemas/service_icon.py': quotedStrings(braceBlockAfter(backend, 'ALLOWED_SERVICE_ICONS')),
    'apps/anila-shell/src/services.jsx': jsIdentKeys(braceBlockAfter(spa, 'const SERVICE_ICONS')),
    'apps/csp-governance-ui/src/components/dashboard/PlatformCard.vue': jsIdentKeys(braceBlockAfter(dashboard, 'const GLYPHS')),
    'services/csp/tests/test_platform_link_icons.py': quotedStrings(braceBlockAfter(expected, 'EXPECTED_SERVICE_ICONS')),
  }

  const canonical = sources['services/csp/app/schemas/service_icon.py']
  assert.ok(canonical.size > 0, 'failed to parse ALLOWED_SERVICE_ICONS')

  const lines = []
  for (const [path, keys] of Object.entries(sources)) {
    assert.ok(keys.size > 0, `failed to parse keys from ${path}`)
    const missing = [...canonical].filter((k) => !keys.has(k)).sort()
    const extra = [...keys].filter((k) => !canonical.has(k)).sort()
    if (missing.length) lines.push(`${path} missing ${JSON.stringify(missing)}`)
    if (extra.length) lines.push(`${path} extra ${JSON.stringify(extra)}`)
  }
  assert.equal(lines.length, 0, `icon key-set drift (lagging files named):\n${lines.join('\n')}`)
})
