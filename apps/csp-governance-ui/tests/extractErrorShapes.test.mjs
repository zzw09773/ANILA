// R3 — extractError 對三種 detail 形狀的行為契約。
//
// 為什麼要有這檔
// ---------------
// R3-1 是把 `detail || fallback` 改成 `extractError(err, fallback)` 的
// 兩處（LoginView 註冊、AppHeader 改密碼）收進「物件形狀不掉進畫面上」；
// R3-2 是把 `typeof detail === 'string'` 單行守衛（會把 422 陣列掉成 generic
// fallback）一行換一行改成 extractError。這檔就是那兩條不變式的唯讀產測：
//
//   1. 物件形狀 → 取物件的 message，不是把整顆物件壓成 [object Object]。
//   2. 422 陣列形狀 → 顯示每條的中文 msg，不是掉成 fallback。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { extractError } from '../src/api/errors.js'

// ── 行為層：extractError 對三種形狀 ──────────────────────────────────────

test('字串 detail → 原樣回傳', () => {
  const err = { response: { data: { detail: '名稱必填' } } }
  assert.equal(extractError(err, '建立失敗'), '名稱必填')
})

test('R3-1:物件 detail → 取 message,不是 [object Object]', () => {
  const err = { response: { data: { detail: { code: 'x', message: '此帳號尚未開通' } } } }
  assert.equal(extractError(err, '註冊失敗'), '此帳號尚未開通')
})

test('R3-1:物件 detail 無 message → fallback,不是物件本身', () => {
  const err = { response: { data: { detail: { code: 'broken' } } } }
  assert.equal(extractError(err, '更新失敗'), '更新失敗')
})

test('R3-2:422 陣列 → 顯示每條 msg,不掉成 fallback', () => {
  const err = {
    response: {
      data: {
        detail: [
          { type: 'string_too_short', loc: ['body', 'name'], msg: 'String should have at least 1 character' },
          { type: 'missing', loc: ['body', 'description'], msg: 'Field required' },
        ],
      },
    },
  }
  const shown = extractError(err, '操作失敗')
  assert.ok(shown.includes('String should have at least 1 character'), `掉了第一條:${shown}`)
  assert.ok(shown.includes('Field required'), `掉了第二條:${shown}`)
  assert.ok(!shown.includes('操作失敗'), `陣列被掉成 fallback:${shown}`)
})

// ── 消費點護欄：R3-1 那兩處真的走 extractError ───────────────────────────

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

function code(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

test('LoginView 註冊失敗走 extractError,不再裸 (detail || fallback)', () => {
  const src = code(readSource('views/LoginView.vue'))
  assert.ok(src.includes("regError.value = extractError(e, '註冊失敗')"), 'LoginView 註冊失敗沒走 extractError')
  assert.ok(!src.includes("(detail || '註冊失敗')"), 'LoginView 仍殘留物件掉落寫法')
})

test('AppHeader 改密碼失敗走 extractError,不再裸 (detail || fallback)', () => {
  const src = code(readSource('components/layout/AppHeader.vue'))
  assert.ok(src.includes("pwError.value = extractError(e, '更新失敗')"), 'AppHeader 改密碼失敗沒走 extractError')
  assert.ok(!src.includes("(detail || '更新失敗')"), 'AppHeader 仍殘留物件掉落寫法')
})

// ── getRawDetail 的形狀存取收斂──R4-2 母集合窮舉 ─────────────────────────────────
//
// 為什麼不是「3 檔允許＋5 檔禁止」半寫清單：Reviewer 把 getRawDetail
// 種進 UsersView.vue（兩份清單之外）→ 166 全綠。清單守的是「這幾支還留著、
// 那幾支沒有了」，不是標題宣稱的「只有真需形狀的才留」——新檔用上它不會紅。
// 所以抄 bareDetailRatchet 同一套 walker：除具名 N 支，src/** 任何
// .vue/.js 出現 `getRawDetail(` 就紅。

const SRC_DIR = fileURLToPath(new URL('../src/', import.meta.url))

function walkVsJs(dir) {
  const out = []
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, ent.name)
    if (ent.isDirectory()) out.push(...walkVsJs(full))
    else if (/\.(vue|js)$/.test(ent.name)) out.push(full)
  }
  return out
}

// 唯二合法持有 getRawDetail( 的：定義處本身＋ModelsView 的 untrusted_host
// 分流（要 code/host/message/hint 全形狀，不可 collapse 成字串）。
const GET_RAW_EXEMPT = new Set(['api/errors.js', 'views/ModelsView.vue'])

test('除具名兩支,src/** 任何 .vue/.js 不得出現 getRawDetail(', () => {
  const offenders = []
  for (const full of walkVsJs(SRC_DIR)) {
    const rel = path.relative(SRC_DIR, full).replaceAll('\\', '/')
    if (GET_RAW_EXEMPT.has(rel)) continue
    if (code(readFileSync(full, 'utf8')).includes('getRawDetail(')) {
      offenders.push(rel)
    }
  }
  assert.deepEqual(
    offenders,
    [],
    '這些檔持有 getRawDetail(（繞過字串收斂、或半寫清單外的形狀存取）:\n  - ' + offenders.join('\n  - '),
  )
})

test('正向錨點:ModelsView 的 untrusted_host 分流確實還留著 getRawDetail', () => {
  // 負向斷言要有正向錨點——整棵樹若被 walker 掃空,上一條會恆綠。
  // 這條釘住「該留的那一格還在」,防 fail-closed 變成 fail-open。
  const src = code(readSource('views/ModelsView.vue'))
  assert.ok(src.includes('getRawDetail('), 'ModelsView 的形狀分流不見了——walk 掃空或誤清')
})
