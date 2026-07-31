/**
 * 部門樹的顯示輔助（院部 → 研究所／中心 → 組／科）。
 *
 * 為什麼需要祖先路徑：編制到三層之後，光看「企劃組」分不出是哪個所底下的，
 * 而兩個所各有一個「企劃組」是院內常態（後端的名稱唯一性只到同一個母單位
 * 為止，見 r1_0026）。所以凡是把部門排成一列選單的地方，都要把路徑攤開。
 *
 * 所有走訪都帶 visited 集合：資料庫層擋不住人工改壞的環狀 parent_id，
 * 前端撞到環要停下來，不是把分頁掛掉。
 */

export const PATH_SEPARATOR = ' / '

/** 扁平清單 → Map(id → department)。 */
export function indexById(departments) {
  const byId = new Map()
  for (const d of departments || []) byId.set(d.id, d)
  return byId
}

/** 從根到 id 的名稱陣列，例如 ['院部', '航空研究所', '氣動組']。 */
export function ancestorNames(id, byId) {
  const names = []
  const seen = new Set()
  let cur = byId.get(id)
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    names.unshift(cur.name)
    cur = cur.parent_id == null ? null : byId.get(cur.parent_id)
  }
  return names
}

/** 完整路徑字串，例如 '院部 / 航空研究所 / 氣動組'。 */
export function departmentPath(id, byId) {
  return ancestorNames(id, byId).join(PATH_SEPARATOR)
}

/** 深度，根節點 = 1。找不到回 0。 */
export function departmentDepth(id, byId) {
  return ancestorNames(id, byId).length
}

/** id 是不是 ancestorId 本身或它的子孫（用來擋「把部門掛到自己底下」）。 */
export function isSelfOrDescendant(id, ancestorId, byId) {
  if (ancestorId == null) return false
  const seen = new Set()
  let cur = byId.get(id)
  while (cur && !seen.has(cur.id)) {
    if (cur.id === ancestorId) return true
    seen.add(cur.id)
    cur = cur.parent_id == null ? null : byId.get(cur.parent_id)
  }
  return false
}

/**
 * GET /api/departments/tree 的巢狀結果 → [{ id, depth, hasChildren }]，
 * depth 由 1 起算，順序就是畫面上要由上往下排的順序。
 */
export function flattenTree(nodes, depth = 1, out = []) {
  for (const node of nodes || []) {
    const children = node.children || []
    out.push({ id: node.id, depth, hasChildren: children.length > 0 })
    flattenTree(children, depth + 1, out)
  }
  return out
}

/**
 * 給下拉選單用的部門選項，依完整路徑排序（父的路徑是子的前綴，排出來自然
 * 就是樹狀順序），每個選項都帶完整祖先路徑。
 *
 * @param departments        扁平部門清單（要有 id / name / parent_id / is_active）
 * @param excludeSubtreeOf   排除這個部門與它的所有子孫（編輯自己時用）
 * @param activeOnly         只列使用中的部門
 */
export function departmentOptions(
  departments,
  { excludeSubtreeOf = null, activeOnly = true } = {},
) {
  const byId = indexById(departments)
  return (departments || [])
    .filter((d) => (activeOnly ? d.is_active : true))
    .filter((d) => !isSelfOrDescendant(d.id, excludeSubtreeOf, byId))
    .map((d) => ({
      id: d.id,
      name: d.name,
      depth: departmentDepth(d.id, byId),
      label: departmentPath(d.id, byId),
    }))
    .sort((a, b) => a.label.localeCompare(b.label, 'zh-Hant'))
}
