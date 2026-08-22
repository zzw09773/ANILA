// R4 最後一格 — 錯誤輔助函式的「被呼叫名字解析得到」守衛。
//
// 為什麼要有這格
// ---------------
// scan-and-move 式重構（在數十支檔間搬動錯誤處理符號）最會出「呼叫了一個
// 沒 import、也沒定義的名字」。此 app 沒有 eslint、scripts 只有 vite build
// ＋ node --test —— build 過 ≠ 能用（Vue SFC 不檢查未定義變數，這是執行期
// ReferenceError），而 npm test 對「被呼叫的名字存在嗎」也不管。
//
// 符號維護（照審查長那條「導得出來的就不要手打」）：
//   - **可導出那半**（extractError / getRawDetail / getLoginErrorMessage /
//     getLoginErrorCode / extractDetail）從錯誤模組的 `export function|const`
//     窮舉——新 helper 加進那些模組，這份清單**自己會長大**。
//   - **幽靈那半**（apiDetail / blobApiDetail / errDetail）export 定義 0，
//     只存在於重構壞掉的那一刻。這半**只能手寫**——照 ratchet 規矩把
//     「這份清單不會自己長大」寫在旁邊，不寫「會被抓到」。
//   - **範圍判斷（刻意）**：loginSurface 的 getLoginErrorMessage／
//     getLoginErrorCode 屬「錯誤輔助函式家族」納入；其餘登入／卡登 helpers
//     （detectCard、resolveNextDestination…）不屬錯誤家族，不納。
//
// 判準（負向反驗）：把某支檔的 `extractError` import 拿掉，這裡必須紅。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC_DIR = fileURLToPath(new URL('../src/', import.meta.url))

// ── 可導出那半：從錯誤模組的 export 窮舉（自己會長大）──────────────────
const EXPORTING_MODULES = ['api/errors.js', 'utils/loginSurface.js', 'utils/settingsView.js']

function exportedHelperNames(rel) {
  const src = readFileSync(path.join(SRC_DIR, rel), 'utf8')
  const names = new Set()
  for (const [, n] of src.matchAll(/(?:export\s+)(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g)) {
    names.add(n)
  }
  for (const [, n] of src.matchAll(/export\s+const\s+([A-Za-z_$][\w$]*)\s*=/g)) {
    names.add(n)
  }
  return names
}

/** 幽靈那半：export 定義 0、只存在於壞掉的那一刻。手寫，不會自己長大。 */
const GHOST_SYMBOLS = ['apiDetail', 'blobApiDetail', 'errDetail']

function walkVsJs(dir) {
  const out = []
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, ent.name)
    if (ent.isDirectory()) out.push(...walkVsJs(full))
    else if (/\.(vue|js)$/.test(ent.name)) out.push(full)
  }
  return out
}

/** 本檔自己定義的名字：function / const 箭頭（原始碼直接抓，不清註解）。 */
function definedNames(source) {
  const names = new Set()
  for (const [, n] of source.matchAll(/(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g)) {
    names.add(n)
  }
  for (const [, n] of source.matchAll(/\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async)/g)) {
    names.add(n)
  }
  return names
}

/** 本檔 import 進來的名字（原始碼直接抓 { a, b } 與 { a as b }）。 */
function importedNames(source) {
  const names = new Set()
  for (const m of source.matchAll(/import\s*\{([^}]*)\}\s*from\s*['"][^'"]+['"]/g)) {
    for (const part of m[1].split(',')) {
      const item = part.trim()
      if (!item) continue
      const pieces = item.split(/\s+as\s+/)
      names.add(pieces[pieces.length - 1].trim())
    }
  }
  return names
}

/** 呼叫位：name( ，但不是函式宣告自己也不是 import/export 行。 */
function calledNames(source, symbols) {
  const out = new Set()
  for (const sym of symbols) {
    const re = new RegExp(`\\b${sym}\\s*\\(`)
    if (re.test(source)) out.add(sym)
  }
  return out
}

test('錯誤輔助函式:任何呼叫都必須在該檔解析得到(import 或定義)', () => {
  // 可導出那半＋幽靈那半合起來是這格要鎖的符號集合。
  const exporting = new Set(
    EXPORTING_MODULES.flatMap((rel) => [...exportedHelperNames(rel)]),
  )
  const allSymbols = [...new Set([...exporting, ...GHOST_SYMBOLS])]

  const problems = []
  for (const full of walkVsJs(SRC_DIR)) {
    const rel = path.relative(SRC_DIR, full).replaceAll('\\', '/')
    const source = readFileSync(full, 'utf8')
    const defs = definedNames(source)
    const imps = importedNames(source)
    const called = calledNames(source, allSymbols)
    for (const name of called) {
      // 定義檔自己不算(它 define 而非 import)。
      if (defs.has(name)) continue
      if (imps.has(name)) continue
      problems.push(`${rel} 呼叫 ${name}( 但無 import 也無定義`)
    }
  }
  assert.deepEqual(problems, [], '未解析的錯誤輔助函式呼叫:\n  - ' + problems.join('\n  - '))
})

test('正向錨點:可導出那半確實從 export 窮舉出來(不是空清單)', () => {
  const exporting = new Set(
    EXPORTING_MODULES.flatMap((rel) => [...exportedHelperNames(rel)]),
  )
  assert.ok(exporting.has('extractError'), '窮舉漏 extractError——空清單會讓守衛恆綠')
  assert.ok(exporting.has('getRawDetail'), '窮舉漏 getRawDetail')
  // 審查長點名的兩個已漏的真 export：它們也要被導出式窮舉自動含住。
  assert.ok(exporting.has('getLoginErrorMessage'), '導出式窮舉漏 getLoginErrorMessage')
  assert.ok(exporting.has('getLoginErrorCode'), '導出式窮舉漏 getLoginErrorCode')
  assert.ok(exporting.has('extractDetail'), '導出式窮舉漏 extractDetail')
})
