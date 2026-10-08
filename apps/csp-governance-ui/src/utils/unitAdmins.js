/**
 * 單位管理員頁的純邏輯：計數、分組、繼承、沒有管理員的單位、查一個人。
 *
 * 畫面上的上限用伺服器回的 limit，不在這裡寫死。
 * 一個單位自己的數字只算直接掛在該單位的人；上層的人不算進來。
 */

import { departmentPath, flattenTree, indexById } from './departmentTree.js'

export const HR_MANAGED_NOTE =
  '由人資管理。人資不再列這個職稱，或是把人資資料庫的「主管自動成為單位管理員」關掉之後，這個人下次登入就會移除。'

export const INHERITED_NOTE =
  '這些人掛在上層，不計入這個單位的數字。要調整指派，請打開上層單位。人資帶入的主管仍由人資管理。'

export function kindLabel(kind) {
  if (kind === 'hr') return '人資帶入'
  if (kind === 'inherited') return '繼承'
  return '指派'
}

export function originKind(row) {
  return row?.source === 'hr' ? 'hr' : 'assigned'
}

export function formatAssignedFraction(assigned, limit) {
  const count = Number.isFinite(Number(assigned)) ? Number(assigned) : 0
  if (limit == null || limit === '') return String(count)
  return `${count}／${limit}`
}

export function formatReportableError(detail, status, code) {
  const text = typeof detail === 'string' && detail.trim() ? detail.trim() : '操作沒有完成'
  if (status !== undefined && status !== null && status !== '') {
    return `${text}（狀態 ${status}）`
  }
  if (code) return `${text}（${code}）`
  return text
}

export function personLabel(row) {
  const name = String(row?.display_name || '').trim()
  const employeeNo = String(row?.employee_no || '').trim()
  return {
    primary: name || '（沒有姓名）',
    employeeNo: employeeNo || '—',
  }
}

export function titlesText(row) {
  const titles = Array.isArray(row?.hr_titles) ? row.hr_titles.filter(Boolean) : []
  if (!titles.length) return '人資沒有留下職稱'
  return `職稱：${titles.join('、')}`
}

export function whyText(row) {
  if (row?.source === 'hr') {
    return `人資帶入。${titlesText(row)}。這個單位與它的下層都在範圍裡。`
  }
  return '指派。這個單位與它的下層都在範圍裡。'
}

export function inheritedWhy(row) {
  const from = row?.inheritedFromName || '上層'
  if (row?.source === 'hr') {
    return `人資帶入，繼承自「${from}」。${titlesText(row)}。不計入這個單位的數字。`
  }
  return `指派，繼承自「${from}」。不計入這個單位的數字。`
}

export function canRevoke(row) {
  return !!row && !row.inherited && row.source !== 'hr' && !row.revoked_at
}

function isActiveRow(row) {
  return !!row && !row.revoked_at
}

function isAssignedSource(source) {
  return source === 'manual' || source == null || source === ''
}

export function countDirect(assignments, departmentId) {
  let assigned = 0
  let hr = 0
  for (const row of assignments || []) {
    if (!isActiveRow(row) || row.department_id !== departmentId) continue
    if (row.source === 'hr') hr += 1
    else if (isAssignedSource(row.source)) assigned += 1
  }
  return { assigned, hr }
}

function splitDirect(assignments, departmentId) {
  const assigned = []
  const hr = []
  for (const row of assignments || []) {
    if (!isActiveRow(row) || row.department_id !== departmentId) continue
    if (row.source === 'hr') hr.push(row)
    else if (isAssignedSource(row.source)) assigned.push(row)
  }
  return { assigned, hr }
}

export function inheritedAssignments(departmentId, departments, assignments) {
  const byId = indexById(departments)
  const out = []
  const seen = new Set()
  if (departmentId != null) seen.add(departmentId)
  let parentId = byId.get(departmentId)?.parent_id ?? null
  while (parentId != null && !seen.has(parentId)) {
    seen.add(parentId)
    const ancestor = byId.get(parentId)
    if (!ancestor) break
    for (const row of assignments || []) {
      if (!isActiveRow(row) || row.department_id !== parentId) continue
      out.push({
        ...row,
        inherited: true,
        inheritedFromId: parentId,
        inheritedFromName: ancestor.name || '',
      })
    }
    parentId = ancestor.parent_id ?? null
  }
  return out
}

function readLimit(counts) {
  if (!counts || counts.limit == null || counts.limit === '') return null
  const limit = Number(counts.limit)
  return Number.isFinite(limit) ? limit : null
}

function serverCount(counts, departmentId) {
  const rows = counts?.departments
  if (!Array.isArray(rows)) return null
  return rows.find((row) => row.department_id === departmentId) || null
}

function asCount(value, fallback) {
  const count = Number(value)
  return Number.isFinite(count) ? count : fallback
}

export function departmentAdminRows(departments, assignments, counts) {
  const limit = readLimit(counts)
  const byId = indexById(departments)
  return (departments || []).map((dept) => {
    const local = countDirect(assignments, dept.id)
    const remote = serverCount(counts, dept.id)
    const assignedCount = remote
      ? asCount(remote.assigned_count, local.assigned)
      : local.assigned
    const hrCount = remote ? asCount(remote.hr_count, local.hr) : local.hr
    const direct = splitDirect(assignments, dept.id)
    const inherited = inheritedAssignments(dept.id, departments, assignments)
    const covered = assignedCount + hrCount > 0 || local.assigned + local.hr > 0 || inherited.length > 0
    return {
      id: dept.id,
      name: dept.name,
      path: departmentPath(dept.id, byId),
      parentId: dept.parent_id ?? null,
      isActive: dept.is_active !== false,
      assignedCount,
      hrCount,
      limit,
      fraction: formatAssignedFraction(assignedCount, limit),
      covered,
      assigned: direct.assigned,
      hr: direct.hr,
      inherited,
    }
  })
}

export function orderDepartmentRows(rows, treeNodes) {
  const flat = flattenTree(treeNodes)
  const byId = new Map((rows || []).map((row) => [row.id, row]))
  if (!flat.length) {
    return [...(rows || [])]
      .sort((a, b) => (a.path || '').localeCompare(b.path || '', 'zh-Hant'))
      .map((row) => ({ ...row, depth: row.depth || 1, hasChildren: false }))
  }
  const ordered = []
  const seen = new Set()
  for (const node of flat) {
    const row = byId.get(node.id)
    if (!row) continue
    ordered.push({ ...row, depth: node.depth, hasChildren: node.hasChildren })
    seen.add(node.id)
  }
  for (const row of rows || []) {
    if (!seen.has(row.id)) ordered.push({ ...row, depth: 1, hasChildren: false })
  }
  return ordered
}

export function filterDepartmentRows(rows, { query = '', uncoveredOnly = false } = {}) {
  const needle = String(query || '').trim().toLowerCase()
  return (rows || []).filter((row) => {
    if (uncoveredOnly && row.covered) return false
    if (!needle) return true
    const hay = `${row.name || ''} ${row.path || ''}`.toLowerCase()
    return hay.includes(needle)
  })
}

export function summarize(rows, assignments) {
  const hrPeople = new Set()
  for (const row of assignments || []) {
    if (!isActiveRow(row) || row.source !== 'hr') continue
    hrPeople.add(row.user_id)
  }
  let uncovered = 0
  let full = 0
  for (const row of rows || []) {
    if (!row.covered) uncovered += 1
    if (row.limit != null && row.assignedCount >= row.limit) full += 1
  }
  return { uncovered, full, hrPeople: hrPeople.size }
}

export function alreadyDirect(assignments, departmentId, userId) {
  return (assignments || []).some(
    (row) => isActiveRow(row) && row.department_id === departmentId && row.user_id === userId,
  )
}

export function assignBlockReason({ isActive, assignedCount, limit, already }) {
  if (isActive === false) return '部門不存在或已停用'
  if (already) return '已是該單位的管理員'
  if (limit != null && limit !== '' && Number(assignedCount) >= Number(limit)) {
    return `每單位最多 ${limit} 名單位管理員`
  }
  return ''
}

export function unitsForPerson(userId, departments, assignments) {
  const byId = indexById(departments)
  return (assignments || [])
    .filter((row) => isActiveRow(row) && row.user_id === userId)
    .map((row) => ({
      id: row.id,
      departmentId: row.department_id,
      path: departmentPath(row.department_id, byId) || row.department_name || '',
      displayName: row.display_name || '',
      employeeNo: row.employee_no || '',
      kind: row.source === 'hr' ? 'hr' : 'assigned',
      why: whyText(row),
    }))
    .sort((a, b) => a.path.localeCompare(b.path, 'zh-Hant'))
}
