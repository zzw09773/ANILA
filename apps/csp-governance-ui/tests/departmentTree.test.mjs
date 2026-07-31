// 部門三層樹的顯示邏輯（院部 → 研究所／中心 → 組／科）。
//
// 為什麼是這個形狀的測試
// ----------------------
// governance 沒有 vitest / jsdom（runner 是 `node --test tests/*.test.mjs`），
// 所以沿用本目錄已確立的兩層作法：
//
//   1. **行為層**：純函式 departmentTree.js 直接 import 斷言。
//   2. **原始碼層護欄**：本包真正會壞的是**呼叫端**。helper 全綠但
//      DepartmentsView 的 payload 忘了帶 parent_id，畫面上看起來一切正常，
//      建出來的每一個部門卻都是根節點——這正是這次要修的那個 bug 的原貌，
//      而行為測試一個都不會紅。所以直接讀 .vue 原始碼把呼叫端釘住。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  ancestorNames,
  departmentDepth,
  departmentOptions,
  departmentPath,
  flattenTree,
  indexById,
  isSelfOrDescendant,
} from '../src/utils/departmentTree.js'

// 院部 → {航空研究所 → 氣動組, 船艦研究所 → 企劃組}；兩個所各有一個「企劃組」
const FLAT = [
  { id: 1, name: '院部', parent_id: null, is_active: true },
  { id: 2, name: '航空研究所', parent_id: 1, is_active: true },
  { id: 3, name: '船艦研究所', parent_id: 1, is_active: true },
  { id: 4, name: '氣動組', parent_id: 2, is_active: true },
  { id: 5, name: '企劃組', parent_id: 2, is_active: true },
  { id: 6, name: '企劃組', parent_id: 3, is_active: true },
  { id: 7, name: '已裁撤組', parent_id: 3, is_active: false },
]

// ── 行為層 ────────────────────────────────────────────────────────────────

test('departmentPath 攤開完整祖先路徑', () => {
  const byId = indexById(FLAT)
  assert.equal(departmentPath(1, byId), '院部')
  assert.equal(departmentPath(2, byId), '院部 / 航空研究所')
  assert.equal(departmentPath(4, byId), '院部 / 航空研究所 / 氣動組')
  assert.deepEqual(ancestorNames(4, byId), ['院部', '航空研究所', '氣動組'])
})

test('同名的組靠路徑分得出來（這是全域唯一改成同父唯一之後的重點）', () => {
  const byId = indexById(FLAT)
  assert.equal(departmentPath(5, byId), '院部 / 航空研究所 / 企劃組')
  assert.equal(departmentPath(6, byId), '院部 / 船艦研究所 / 企劃組')
  assert.notEqual(departmentPath(5, byId), departmentPath(6, byId))
})

test('departmentDepth 根節點為 1', () => {
  const byId = indexById(FLAT)
  assert.equal(departmentDepth(1, byId), 1)
  assert.equal(departmentDepth(2, byId), 2)
  assert.equal(departmentDepth(4, byId), 3)
  assert.equal(departmentDepth(999, byId), 0)
})

test('departmentOptions 預設只給使用中的部門，且每一層都可選', () => {
  const opts = departmentOptions(FLAT)
  const ids = opts.map((o) => o.id)
  assert.ok(ids.includes(1), '最上層（院部）要可選：有人直屬院部')
  assert.ok(ids.includes(2), '所要可選：所底下沒有組時人就掛在所')
  assert.ok(ids.includes(4), '組要可選')
  assert.ok(!ids.includes(7), '已停用的不列入')
  assert.equal(opts.every((o) => o.label.length > 0), true)
})

test('departmentOptions 排除自己與子孫（避免把部門掛到自己底下）', () => {
  const ids = departmentOptions(FLAT, { excludeSubtreeOf: 2 }).map((o) => o.id)
  assert.ok(!ids.includes(2), '不可以選自己')
  assert.ok(!ids.includes(4), '不可以選自己的子孫')
  assert.ok(!ids.includes(5), '不可以選自己的子孫')
  assert.ok(ids.includes(1) && ids.includes(3), '其他分支仍然可選')
})

test('isSelfOrDescendant 認得自己與子孫', () => {
  const byId = indexById(FLAT)
  assert.equal(isSelfOrDescendant(2, 2, byId), true)
  assert.equal(isSelfOrDescendant(4, 1, byId), true)
  assert.equal(isSelfOrDescendant(3, 2, byId), false)
  assert.equal(isSelfOrDescendant(4, null, byId), false)
})

test('flattenTree 攤平巢狀樹並標出深度與有無子節點', () => {
  const tree = [
    {
      id: 1,
      name: '院部',
      children: [
        { id: 2, name: '航空研究所', children: [{ id: 4, name: '氣動組', children: [] }] },
        { id: 3, name: '船艦研究所', children: [] },
      ],
    },
  ]
  assert.deepEqual(flattenTree(tree), [
    { id: 1, depth: 1, hasChildren: true },
    { id: 2, depth: 2, hasChildren: true },
    { id: 4, depth: 3, hasChildren: false },
    { id: 3, depth: 2, hasChildren: false },
  ])
})

test('環狀 parent_id 不可以把分頁轉死', () => {
  // 資料庫層擋不住人工改壞的環；前端撞到要停下來而不是無限迴圈。
  const cyclic = [
    { id: 1, name: 'A', parent_id: 2, is_active: true },
    { id: 2, name: 'B', parent_id: 1, is_active: true },
  ]
  const byId = indexById(cyclic)
  assert.deepEqual(ancestorNames(1, byId), ['B', 'A'])
  assert.equal(departmentOptions(cyclic).length, 2)
})

// ── 原始碼層護欄：呼叫端 ──────────────────────────────────────────────────

const departmentsView = readFileSync(
  new URL('../src/views/DepartmentsView.vue', import.meta.url),
  'utf8',
)
const usersView = readFileSync(
  new URL('../src/views/UsersView.vue', import.meta.url),
  'utf8',
)
const departmentsApi = readFileSync(
  new URL('../src/api/departments.js', import.meta.url),
  'utf8',
)

test('DepartmentsView 的 payload 一定要帶 parent_id', () => {
  // 少了這一行，操作者選了母單位也沒用，建出來的每個部門都是根節點。
  assert.match(departmentsView, /parent_id:\s*form\.value\.parent_id/)
})

test('DepartmentsView 有母單位下拉，而且「最上層」是明講的選項', () => {
  assert.match(departmentsView, /v-model="form\.parent_id"/)
  assert.match(departmentsView, /:value="null"[^>]*>\s*—\s*不隸屬任何單位/)
})

test('DepartmentsView 用 /tree 排層級，並且畫得出縮排', () => {
  assert.match(departmentsApi, /getDepartmentTree/)
  assert.match(departmentsView, /getDepartmentTree/)
  assert.match(departmentsView, /row\.depth - 1/)
})

test('建立最上層部門仍然是一鍵（預設 parent_id = null）', () => {
  // 擁有者的長期指示：不可以把系統改嚴。要求先選母單位才能建部門就是退步。
  assert.match(departmentsView, /openCreateModal\(null\)/)
  assert.match(departmentsView, /parent\?\.id \?\? null/)
})

test('UsersView 的部門選單顯示完整路徑，不是光禿禿的名稱', () => {
  assert.match(usersView, /departmentChoices/)
  assert.match(usersView, /\{\{ d\.label \}\}/)
  assert.doesNotMatch(
    usersView,
    /v-for="d in activeDepartments"/,
    '扁平的 activeDepartments 選單會讓兩個同名的組分不出來',
  )
})
