// R2-3 — 裸 data.detail ratchet（母集合＝src/** 全部 .vue/.js，依形狀窮舉）。
//
// ⚠ 這支只認 `response.data.detail` 一種拼法，那條邊界的完整版寫在
//    `src/api/errors.js:14-20`——含「經 getRawDetail 取同一值的呼叫端它也
//    看不見（收斂器，設計如此）」。要判「這格綠代表什麼」時先去看那段。
//
// 為什麼不是四檔手寫清單
// ----------------------
// 既有的兩支 ratchet（healthOverview.test.mjs:190 與 alertSummary.test.mjs 尾巴）
// 各對一份手寫的四檔清單掃 /response\??\.data\??\.detail/g。Reviewer 記帳：
// 同一輪在 CollectionDetailView.vue:523 清掉一個裸 detail，但那支檔不在清單裡，
// 守衛對它失明。手寫清單遲早漏（CLAUDE.md 點名的格）。
//
// 這支把母集合換成「src/** 全部 .vue/.js 依形狀窮舉」。**豁免只三支**：
//   - `api/errors.js`         —— `getRawDetail` / `extractError` 的定義處，唯讀點。
//   - `utils/loginSurface.js`  —— 登入專用錯誤取得器（需區分字串／信封）。
//   - `utils/settingsView.js`  —— 設定專用 `extractDetail`（處理 422 陣列）。
// 其餘任何 .vue/.js 出現 `response?.data?.detail` 那條讀法 = 有人在繞過收斂器
// 直接碰後端錯誤 → 紅。
//
// ⚠ 反驗基準（known-exception counter-test）：母集合的 walker 必涵蓋根層檔
// `App.vue` / `main.js` —— 這是「glob `**/` 要求至少一層、會漏根層」那格
// 的實測反驗，不是靠相信 `readdirSync` 有帶根層。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// fileURLToPath 把 percent-encoded 的 URL 解回實體路徑（URL.pathname 不解碼）。
const SRC = fileURLToPath(new URL('../src/', import.meta.url))

/** 遞迴列出 src 底下所有檔案，相對 src 的路徑（含根層，不經 glob 星號）。 */
function walk(dir) {
  const out = []
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, ent.name)
    if (ent.isDirectory()) out.push(...walk(full))
    else out.push(full)
  }
  return out
}

function relOf(url) {
  return path.relative(path.dirname(SRC.pathname), url.pathname).replaceAll('\\', '/')
}

/** 這三支是「讀後端錯誤」的唯讀點；本質是收斂器，其餘都是消費者。 */
const EXEMPT = new Set(['api/errors.js', 'utils/loginSurface.js', 'utils/settingsView.js'])

test('母集合＝src/** 全部 .vue/.js,且涵蓋根層（App.vue/main.js）', () => {
  const rels = walk(SRC)
    .map((f) => path.relative(SRC, f).replaceAll('\\', '/'))

  const vueJs = rels.filter((r) => /\.(vue|js)$/.test(r))
  // 反驗（known-exception counter-test）：根層兩檔必須在母集合裡。
  for (const rootFile of ['App.vue', 'main.js']) {
    assert.ok(
      vueJs.includes(rootFile),
      'walker 漏了根層檔 ' + rootFile + ' —— glob 的 **/ 那格又重演',
    )
  }
  for (const exempt of EXEMPT) {
    assert.ok(vueJs.includes(exempt), '豁免檔不在母集合裡:' + exempt)
  }
})

test('豁免三支之外,src/** 任何 .vue/.js 不得裸讀 response.data.detail', () => {
  const rels = walk(SRC)
    .map((f) => path.relative(SRC, f).replaceAll('\\', '/'))
  const offenders = []
  for (const rel of rels) {
    if (!/\.(vue|js)$/.test(rel)) continue
    if (EXEMPT.has(rel)) continue
    let source = readFileSync(path.join(SRC, rel), 'utf8')
    // 與既有兩支 ratchet 同一套 strip：註解裡的提及不該讓守衛假紅。
    source = source
      .replace(/<!--[\s\S]*?-->/g, '')
      .split('\n')
      .filter((line) => !line.trimStart().startsWith('//'))
      .join('\n')
    if (/response\??\.data\??\.detail/.test(source)) offenders.push(rel)
  }
  const msg = offenders.length
    ? '這些檔裸讀 response.data.detail（繞過收斂器）:\n  - ' + offenders.join('\n  - ')
    : ''
  assert.deepEqual(offenders, [], msg)
})
