// 單位管理員頁：計數、分組、繼承在純函式；路由、側欄、頁籤只是接線。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  HR_MANAGED_NOTE,
  INHERITED_NOTE,
  alreadyDirect,
  assignBlockReason,
  canRevoke,
  countDirect,
  departmentAdminRows,
  filterDepartmentRows,
  formatAssignedFraction,
  formatReportableError,
  inheritedAssignments,
  inheritedWhy,
  kindLabel,
  orderDepartmentRows,
  originKind,
  summarize,
  titlesText,
  unitsForPerson,
  whyText,
} from '../src/utils/unitAdmins.js'

function read(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

const DEPARTMENTS = [
  { id: 1, name: '院部', parent_id: null, is_active: true },
  { id: 2, name: '航空研究所', parent_id: 1, is_active: true },
  { id: 3, name: '船艦研究所', parent_id: 1, is_active: true },
  { id: 4, name: '氣動組', parent_id: 2, is_active: true },
  { id: 5, name: '停用組', parent_id: 3, is_active: false },
]

const TREE = [
  {
    id: 1,
    children: [
      { id: 2, children: [{ id: 4, children: [] }] },
      { id: 3, children: [{ id: 5, children: [] }] },
    ],
  },
]

const ASSIGNMENTS = [
  {
    id: 10,
    user_id: 100,
    department_id: 2,
    source: 'manual',
    display_name: '王小明',
    employee_no: '100',
    revoked_at: null,
  },
  {
    id: 11,
    user_id: 101,
    department_id: 2,
    source: 'hr',
    display_name: '林主管',
    employee_no: '101',
    hr_titles: ['組長', '副組長'],
    revoked_at: null,
  },
  {
    id: 13,
    user_id: 103,
    department_id: 3,
    source: 'manual',
    display_name: '已撤銷',
    employee_no: '103',
    revoked_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 14,
    user_id: 101,
    department_id: 4,
    source: 'hr',
    display_name: '林主管',
    employee_no: '101',
    hr_titles: ['組長'],
    revoked_at: null,
  },
]

test('直接人數不含人資，也不含上層與已撤銷', () => {
  assert.deepEqual(countDirect(ASSIGNMENTS, 2), { assigned: 1, hr: 1 })
  assert.deepEqual(countDirect(ASSIGNMENTS, 4), { assigned: 0, hr: 1 })
  assert.deepEqual(countDirect(ASSIGNMENTS, 3), { assigned: 0, hr: 0 })
  assert.deepEqual(countDirect(ASSIGNMENTS, 1), { assigned: 0, hr: 0 })
})

test('下層看得到上層的人，但標成繼承，不算進自己的數字', () => {
  const inherited = inheritedAssignments(4, DEPARTMENTS, ASSIGNMENTS)
  const ids = inherited.map((row) => row.id).sort((a, b) => a - b)
  assert.deepEqual(ids, [10, 11])
  assert.equal(inherited.every((row) => row.inherited), true)
  const fromAviation = inherited.find((row) => row.id === 10)
  assert.equal(fromAviation.inheritedFromName, '航空研究所')
  assert.match(inheritedWhy(fromAviation), /繼承自「航空研究所」/)
  assert.match(inheritedWhy(fromAviation), /不計入這個單位的數字/)
  assert.doesNotMatch(formatAssignedFraction(0, 3), /1/)

  const rows = departmentAdminRows(DEPARTMENTS, ASSIGNMENTS, {
    limit: 3,
    departments: [
      { department_id: 1, assigned_count: 1, hr_count: 0 },
      { department_id: 2, assigned_count: 1, hr_count: 1 },
      { department_id: 3, assigned_count: 0, hr_count: 0 },
      { department_id: 4, assigned_count: 0, hr_count: 1 },
      { department_id: 5, assigned_count: 0, hr_count: 0 },
    ],
  })
  const aero = rows.find((row) => row.id === 4)
  assert.equal(aero.assignedCount, 0)
  assert.equal(aero.hrCount, 1)
  assert.equal(aero.fraction, '0／3')
  assert.equal(aero.inherited.length, 2)
  assert.equal(aero.covered, true)
  assert.equal(aero.assigned.length, 0)
})

test('沒有管理員是自己與上層都沒有人；人資主管人數不把同一人算兩次', () => {
  const rows = departmentAdminRows(DEPARTMENTS, ASSIGNMENTS, { limit: 3, departments: [] })
  const ordered = orderDepartmentRows(rows, TREE)
  assert.deepEqual(ordered.map((row) => row.id), [1, 2, 4, 3, 5])
  assert.equal(ordered.find((row) => row.id === 4).depth, 3)

  const uncovered = filterDepartmentRows(ordered, { uncoveredOnly: true })
  assert.deepEqual(uncovered.map((row) => row.id), [1, 3, 5])

  const byPath = filterDepartmentRows(ordered, { query: '航空' })
  assert.deepEqual(byPath.map((row) => row.id), [2, 4])

  const summary = summarize(ordered, ASSIGNMENTS)
  assert.equal(summary.uncovered, 3)
  assert.equal(summary.full, 0)
  assert.equal(summary.hrPeople, 1)
})

test('上限與人數以伺服器為準；伺服器沒列到的單位才用清單自己算', () => {
  const withRoot = [
    ...ASSIGNMENTS,
    {
      id: 12,
      user_id: 102,
      department_id: 1,
      source: 'manual',
      revoked_at: null,
    },
  ]
  const rows = departmentAdminRows(DEPARTMENTS, withRoot, {
    limit: 4,
    departments: [{ department_id: 2, assigned_count: 2, hr_count: 0 }],
  })
  const aviation = rows.find((row) => row.id === 2)
  assert.equal(aviation.assignedCount, 2)
  assert.equal(aviation.hrCount, 0)
  assert.equal(aviation.fraction, '2／4')
  assert.equal(aviation.assigned.length, 1)
  const root = rows.find((row) => row.id === 1)
  assert.equal(root.assignedCount, 1)
  assert.equal(root.limit, 4)
})

test('指派前就能看出已知的拒絕原因，而且上限不是寫死的 3', () => {
  assert.equal(assignBlockReason({ isActive: false, assignedCount: 0, limit: 4, already: false }), '部門不存在或已停用')
  assert.equal(assignBlockReason({ isActive: true, assignedCount: 0, limit: 4, already: true }), '已是該單位的管理員')
  assert.equal(assignBlockReason({ isActive: true, assignedCount: 4, limit: 4, already: false }), '每單位最多 4 名單位管理員')
  assert.equal(assignBlockReason({ isActive: true, assignedCount: 1, limit: null, already: false }), '')
  assert.equal(alreadyDirect(ASSIGNMENTS, 2, 100), true)
  assert.equal(alreadyDirect(ASSIGNMENTS, 4, 100), false)
  assert.equal(canRevoke(ASSIGNMENTS[0]), true)
  assert.equal(canRevoke(ASSIGNMENTS[1]), false)
  assert.equal(canRevoke({ ...ASSIGNMENTS[0], inherited: true }), false)
})

test('查一個人會列出每個單位與原因；兩種身分用文字分開', () => {
  const units = unitsForPerson(101, DEPARTMENTS, ASSIGNMENTS)
  assert.equal(units.length, 2)
  assert.equal(units[0].kind, 'hr')
  assert.match(units[0].why, /人資帶入/)
  assert.match(units[0].why, /職稱：副組長、組長|職稱：組長、副組長/)
  assert.match(whyText(ASSIGNMENTS[0]), /^指派/)
  assert.equal(kindLabel('assigned'), '指派')
  assert.equal(kindLabel('hr'), '人資帶入')
  assert.equal(kindLabel('inherited'), '繼承')
  assert.equal(originKind(ASSIGNMENTS[1]), 'hr')
  assert.equal(originKind(ASSIGNMENTS[0]), 'assigned')
  assert.equal(titlesText({ hr_titles: [] }), '人資沒有留下職稱')
  assert.match(HR_MANAGED_NOTE, /由人資管理/)
  assert.match(HR_MANAGED_NOTE, /主管自動成為單位管理員/)
  assert.match(INHERITED_NOTE, /不計入這個單位的數字/)
  assert.equal(unitsForPerson(103, DEPARTMENTS, ASSIGNMENTS).length, 0)
})

test('錯誤留下原因與狀態，方便回報', () => {
  assert.equal(formatReportableError('已是該單位的管理員', 400), '已是該單位的管理員（狀態 400）')
  assert.equal(formatReportableError('讀不到', undefined, 'ERR_NETWORK'), '讀不到（ERR_NETWORK）')
  assert.equal(formatReportableError('', null), '操作沒有完成')
})

test('父節點指到自己時不會一直找下去', () => {
  const loop = [{ id: 9, name: '環', parent_id: 9, is_active: true }]
  assert.deepEqual(inheritedAssignments(9, loop, [{ id: 1, user_id: 1, department_id: 9, source: 'manual' }]), [])
})

test('頁面、路由、側欄、頁籤接在管理員的人員與單位', () => {
  const view = read('../src/views/UnitAdminsView.vue')
  const template = view.slice(view.indexOf('<template>'), view.indexOf('<script'))
  const router = read('../src/router/index.js')
  const sidebar = read('../src/components/layout/AppSidebar.vue')
  const header = read('../src/components/layout/AppHeader.vue')
  const api = read('../src/api/unitAdmins.js')

  assert.match(view, /departmentAdminRows/)
  assert.match(view, /unitsForPerson/)
  assert.match(view, /filterDepartmentRows/)
  assert.match(view, /summarize/)
  assert.match(view, /assignBlockReason/)
  assert.match(view, /formatReportableError/)
  assert.match(view, /unitAdminCounts/)
  assert.doesNotMatch(view, /／3/)
  assert.doesNotMatch(view, /最多 3/)
  assert.doesNotMatch(view, /limit\s*[:=]\s*3/)
  assert.doesNotMatch(template, /後端|endpoint|API|院內 AI 平台/)
  assert.doesNotMatch(template, /\bsource\b/)

  const hrStart = template.indexOf('data-kind="hr"')
  const inheritedStart = template.indexOf('data-kind="inherited"')
  assert.ok(hrStart > 0 && inheritedStart > hrStart)
  const hr = template.slice(hrStart, inheritedStart)
  assert.match(hr, /HR_MANAGED_NOTE/)
  assert.doesNotMatch(hr, /<button/)
  assert.match(template, /撤銷指派/)
  assert.match(template, /只看沒有管理員的單位/)
  assert.match(template, /人資主管/)

  const routeStart = router.indexOf("path: 'unit-admins'")
  const routeEnd = router.indexOf('path:', routeStart + 10)
  const route = router.slice(routeStart, routeEnd)
  assert.match(route, /UnitAdminsView/)
  assert.match(route, /requiresAdmin:\s*true/)

  const adminStart = sidebar.indexOf('if (authStore.isAdmin)')
  assert.match(sidebar.slice(adminStart), /path:\s*'\/unit-admins',\s*label:\s*'單位管理員'/)
  const unitOnly = sidebar.slice(
    sidebar.indexOf('isUnitAdmin && !authStore.isAdmin'),
    sidebar.indexOf('if (authStore.isAdmin)'),
  )
  assert.doesNotMatch(unitOnly, /unit-admins/)
  const deputy = sidebar.slice(
    sidebar.indexOf('isDeputy && !authStore.isAdmin'),
    sidebar.indexOf('const groups'),
  )
  assert.doesNotMatch(deputy, /unit-admins/)
  assert.match(header, /'\/unit-admins':\s*'單位管理員'/)

  assert.match(api, /from '\.\/client'/)
  assert.match(api, /client\.get\('\/api\/unit-admins\/counts'\)/)
  assert.match(api, /client\.post\('\/api\/unit-admins'/)
  assert.match(api, /client\.delete\(`\/api\/unit-admins\/\$\{id\}`\)/)
  assert.doesNotMatch(api, /fetch\(/)
})
