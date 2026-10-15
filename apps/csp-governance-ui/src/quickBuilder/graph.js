import { DOCUMENT_DEFAULTS } from './documentSettings.js'
import { RAG_DEFAULTS } from './ragSettings.js'
// 設定只作為資料使用；不執行運算式、程式碼或自訂元件。
export const modules = [
  { kind: 'input', name: '對話輸入', description: '接收同仁這次傳入的問題。', defaults: { placeholder: '請輸入問題' } },
  { kind: 'output', name: '對話輸出', description: '將結果回傳對話，保留可追查的來源。', defaults: { include_sources: true } },
  { kind: 'model', name: '模型', description: '透過平台模型代理產生回答；模型在助手設定選擇。', defaults: { temperature: 0.2 } },
  { kind: 'prompt', name: '提示詞與角色', description: '用白話告訴助手要完成的工作與回答方式。', defaults: { role: 'assistant', instruction: '請以台灣繁體中文清楚回答，資料不足時請明說。' } },
  { kind: 'rag', name: '文件檢索', description: '搜尋有權限的文件集，回答時附來源。', defaults: RAG_DEFAULTS },
  { kind: 'mcp', name: '工具查詢', description: '以提問者身分呼叫已核准的唯讀工具。', defaults: { tool_id: null, arguments: {} } },
  { kind: 'condition', name: '條件判斷', description: '比較欄位內容，依成立或不成立選擇下一步。', defaults: { field: 'text', operator: 'contains', value: '' } },
  { kind: 'extract', name: '擷取欄位', description: '從對話整理需要的欄位，缺少的資料標示未提供。', defaults: { fields: ['主旨', '承辦人'] } },
  { kind: 'format', name: '文字整理', description: '將內容整理成容易閱讀的固定格式。', defaults: { style: 'paragraph' } },
  { kind: 'document', name: '文件草稿', description: '依純文字範本或既有 Studio 範本產出草稿，請人工確認後使用。', defaults: DOCUMENT_DEFAULTS },
  { kind: 'agent', name: '多步驟代理', description: '依序規劃、查詢工具、檢查結果，必要時重試。', defaults: { tool_ids: [], max_steps: 5, max_retries: 1 } },
]
export const clone = (value) => JSON.parse(JSON.stringify(value))
export function makeNode(kind, index = 0, position) {
  const module = modules.find((m) => m.kind === kind)
  if (!module) throw new Error('不支援此模組（MODULE_KIND）')
  return { id: `node_${globalThis.crypto?.randomUUID?.() || `${Date.now()}_${index}`}`, kind, label: module.name, position: position || { x: 40 + (index % 3) * 220, y: 40 + Math.floor(index / 3) * 150 }, config: clone(module.defaults) }
}
// Node dimensions are outer border-box sizes, including padding and borders.
// These are presentation values only and never become persisted graph fields.
export const GRAPH_GEOMETRY = Object.freeze({
  nodeWidth: 200, nodeHeight: 152, columnGap: 72, rankGap: 56, padding: 24,
})

const compareId = (a, b) => a < b ? -1 : a > b ? 1 : 0
function geometry(options = {}) {
  const result = { ...GRAPH_GEOMETRY, ...options }
  for (const key of Object.keys(GRAPH_GEOMETRY)) {
    if (!Number.isFinite(result[key]) || result[key] < 0 || (key.startsWith('node') && result[key] === 0)) {
      throw new Error('畫布尺寸無效（GRAPH_GEOMETRY）')
    }
  }
  return result
}
function nodeSize(node, options) {
  const measured = options.nodeSizes?.[node.id]
  return {
    width: Number.isFinite(measured?.width) && measured.width > 0 ? measured.width : options.nodeWidth,
    height: Number.isFinite(measured?.height) && measured.height > 0 ? measured.height : options.nodeHeight,
  }
}
const branchOrder = edge => edge.branch === 'true' ? 0 : edge.branch === 'false' ? 1 : 2

/** New/template/explicit arrange only: saved layouts must remain as authored. */
export function layoutGraph(graph, options = {}) {
  const settings = geometry(options)
  const result = clone(graph)
  const nodes = result.nodes || []
  const edges = result.edges || []
  const byId = new Map(nodes.map(node => [node.id, node]))
  if (byId.size !== nodes.length || edges.some(edge => !byId.has(edge.source) || !byId.has(edge.target))) {
    throw new Error('流程節點或連線無效，無法整理排列（GRAPH_LAYOUT）')
  }
  const orderedIds = [...byId.keys()].sort(compareId)
  const outgoing = new Map(orderedIds.map(id => [id, []]))
  const incoming = new Map(orderedIds.map(id => [id, 0]))
  const sortedEdges = [...edges].sort((a, b) => compareId(a.source, b.source) || branchOrder(a) - branchOrder(b) || compareId(a.target, b.target))
  for (const edge of sortedEdges) {
    outgoing.get(edge.source).push(edge)
    incoming.set(edge.target, incoming.get(edge.target) + 1)
  }
  const roots = orderedIds.filter(id => incoming.get(id) === 0)
  const queue = [...roots]
  const rank = new Map(orderedIds.map(id => [id, 0]))
  const topological = []
  while (queue.length) {
    const id = queue.shift()
    topological.push(id)
    for (const edge of outgoing.get(id)) {
      rank.set(edge.target, Math.max(rank.get(edge.target), rank.get(id) + 1))
      incoming.set(edge.target, incoming.get(edge.target) - 1)
      if (incoming.get(edge.target) === 0) {
        queue.push(edge.target)
        queue.sort(compareId)
      }
    }
  }
  if (topological.length !== nodes.length) throw new Error('流程含有循環，無法整理排列（GRAPH_CYCLE）')
  if (!nodes.length) return result

  // Stable true-first traversal establishes branch lanes before crossing reduction.
  const priority = new Map()
  const visit = id => {
    if (priority.has(id)) return
    priority.set(id, priority.size)
    outgoing.get(id).forEach(edge => visit(edge.target))
  }
  roots.forEach(visit)
  const vertices = new Map(orderedIds.map(id => [id, { id, rank: rank.get(id), priority: priority.get(id), before: [], after: [] }]))
  const layers = Array.from({ length: Math.max(...rank.values()) + 1 }, () => [])
  for (const vertex of vertices.values()) layers[vertex.rank].push(vertex)
  sortedEdges.forEach((edge, index) => {
    let previous = vertices.get(edge.source)
    for (let level = previous.rank + 1; level < rank.get(edge.target); level++) {
      // Temporary routing slots reserve space across intermediate ranks.
      const dummy = { id: `\0edge_${index}_${level}`, rank: level, priority: priority.get(edge.target), before: [], after: [], edge }
      layers[level].push(dummy)
      previous.after.push(dummy)
      dummy.before.push(previous)
      previous = dummy
    }
    const target = vertices.get(edge.target)
    previous.after.push(target)
    target.before.push(previous)
  })
  layers.forEach(layer => layer.sort((a, b) => a.priority - b.priority || compareId(a.id, b.id)))
  const descendants = start => {
    const found = new Set(), pending = [start]
    while (pending.length) {
      const id = pending.pop()
      if (found.has(id)) continue
      found.add(id)
      outgoing.get(id).forEach(edge => pending.push(edge.target))
    }
    return found
  }
  const branches = orderedIds.flatMap(id => {
    const yes = outgoing.get(id).find(edge => edge.branch === 'true')
    const no = outgoing.get(id).find(edge => edge.branch === 'false')
    if (!yes || !no) return []
    const left = descendants(yes.target), right = descendants(no.target)
    const common = [...left].filter(target => right.has(target))
    common.forEach(target => { left.delete(target); right.delete(target) })
    return [{ left, right }]
  })
  const belongs = (set, vertex) => set.has(vertex.id) || (vertex.edge && (set.has(vertex.edge.source) || set.has(vertex.edge.target)))
  function reorder(layer, direction, positions) {
    const score = vertex => {
      const neighbours = vertex[direction]
      return neighbours.length ? neighbours.reduce((sum, other) => sum + positions.get(other.id), 0) / neighbours.length : positions.get(vertex.id)
    }
    const preferred = [...layer].sort((a, b) => score(a) - score(b) || a.priority - b.priority || compareId(a.id, b.id))
    const predecessors = new Map(layer.map(vertex => [vertex.id, new Set()]))
    for (const { left, right } of branches) {
      for (const a of layer.filter(vertex => belongs(left, vertex))) {
        for (const b of layer.filter(vertex => belongs(right, vertex))) {
          if (a !== b) predecessors.get(b.id).add(a.id)
        }
      }
    }
    const ordered = [], remaining = new Set(layer.map(vertex => vertex.id))
    while (remaining.size) {
      const next = preferred.find(vertex => remaining.has(vertex.id) && [...predecessors.get(vertex.id)].every(id => !remaining.has(id)))
      // A non-planar graph can have contradictory lane constraints; retain stable order.
      if (!next) return preferred
      ordered.push(next)
      remaining.delete(next.id)
    }
    return ordered
  }
  for (let sweep = 0; sweep < 6; sweep++) {
    const direction = sweep % 2 ? 'after' : 'before'
    const levels = direction === 'before' ? layers.keys() : [...layers.keys()].reverse()
    for (const level of levels) {
      const positions = new Map(layers.flatMap(layer => layer.map((vertex, index) => [vertex.id, index])))
      layers[level] = reorder(layers[level], direction, positions)
    }
  }
  const widths = layers.map(layer => layer.reduce((sum, vertex) => sum + (byId.has(vertex.id) ? nodeSize(byId.get(vertex.id), settings).width : settings.nodeWidth), 0) + Math.max(0, layer.length - 1) * settings.columnGap)
  const maximumWidth = Math.max(...widths)
  let y = settings.padding
  layers.forEach((layer, level) => {
    let x = settings.padding + (maximumWidth - widths[level]) / 2
    let height = settings.nodeHeight
    for (const vertex of layer) {
      const node = byId.get(vertex.id)
      const size = node ? nodeSize(node, settings) : { width: settings.nodeWidth, height: settings.nodeHeight }
      if (node) node.position = { x, y }
      x += size.width + settings.columnGap
      height = Math.max(height, size.height)
    }
    y += height + settings.rankGap
  })
  return result
}

export function graphBounds(graph, options = {}, routes) {
  const settings = geometry(options)
  const boxes = (graph.nodes || []).map(node => ({ ...node.position, ...nodeSize(node, settings) }))
  for (const [index, edge] of (graph.edges || []).entries()) {
    const route = routes === undefined ? edgeGeometry(graph, edge, settings) : routes[index]
    if (!route) continue
    boxes.push(...route.points.map(point => ({ ...point, width: 0, height: 0 })))
    if (edge.branch) boxes.push(labelBox(route.label, edge))
  }
  if (!boxes.length) return { x: 0, y: 0, width: settings.padding * 2, height: settings.padding * 2 }
  const minimumX = Math.min(...boxes.map(box => box.x - settings.padding))
  const minimumY = Math.min(...boxes.map(box => box.y - settings.padding))
  const maximumX = Math.max(...boxes.map(box => box.x + box.width + settings.padding))
  const maximumY = Math.max(...boxes.map(box => box.y + box.height + settings.padding))
  const includeOrigin = settings.includeOrigin !== false
  const x = includeOrigin ? Math.min(0, minimumX) : minimumX
  const y = includeOrigin ? Math.min(0, minimumY) : minimumY
  return {
    x, y,
    width: (includeOrigin ? Math.max(settings.padding * 2, maximumX) : maximumX) - x,
    height: (includeOrigin ? Math.max(settings.padding * 2, maximumY) : maximumY) - y,
  }
}

function segmentClear(a, b, boxes) {
  return !boxes.some(box => a.x === b.x
    ? a.x > box.left && a.x < box.right && Math.max(a.y, b.y) > box.top && Math.min(a.y, b.y) < box.bottom
    : a.y > box.top && a.y < box.bottom && Math.max(a.x, b.x) > box.left && Math.min(a.x, b.x) < box.right)
}
function compactPoints(points) {
  const unique = points.filter((point, index) => !index || point.x !== points[index - 1].x || point.y !== points[index - 1].y)
  return unique.filter((point, index) => {
    const previous = unique[index - 1], next = unique[index + 1]
    return !(previous && next && ((previous.x === point.x && point.x === next.x) || (previous.y === point.y && point.y === next.y)))
  })
}


// Matches the existing solid SVG label background: 48/64 by 26 pixels.
function labelBox(label, edge) {
  const width = edge.branch === 'false' ? 64 : 48
  return { x: label.x - width / 2, y: label.y - 13, width, height: 26 }
}
function placeLabel(start, points, edge, boxes) {
  const preferred = { x: start.x + (edge.branch === 'true' ? -20 : edge.branch === 'false' ? 20 : 12), y: start.y + 24 }
  if (!edge.branch) return preferred
  const available = point => {
    const label = labelBox(point, edge), margin = 3
    return !boxes.some(box => label.x < box.right + margin && label.x + label.width > box.left - margin &&
      label.y < box.bottom + margin && label.y + label.height > box.top - margin)
  }
  if (available(preferred)) return preferred
  // A 32px gap fits a 26px label with 3px margins; fixed +24 does not.
  const closeOutlet = { x: preferred.x, y: start.y + 16 }
  if (available(closeOutlet)) return closeOutlet
  const candidates = []
  const halfWidth = edge.branch === 'false' ? 32 : 24
  for (let index = 1; index < points.length; index++) {
    const a = points[index - 1], b = points[index]
    const near = { x: Math.max(Math.min(a.x, b.x), Math.min(start.x, Math.max(a.x, b.x))), y: Math.max(Math.min(a.y, b.y), Math.min(start.y + 16, Math.max(a.y, b.y))) }
    for (const point of [near, { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }]) {
      candidates.push(point, { x: point.x, y: point.y + 16 }, { x: point.x, y: point.y - 16 },
        { x: point.x - halfWidth - 4, y: point.y }, { x: point.x + halfWidth + 4, y: point.y })
    }
  }
  // Always retain a safe choice even in very dense manually positioned graphs.
  candidates.push(
    { x: Math.min(...boxes.map(box => box.left)) - halfWidth - 4, y: preferred.y },
    { x: Math.max(...boxes.map(box => box.right)) + halfWidth + 4, y: preferred.y },
  )
  const distance = point => Math.abs(point.x - start.x) + Math.abs(point.y - start.y)
  return candidates.filter(available).sort((a, b) => distance(a) - distance(b) || a.y - b.y || a.x - b.x)[0]
}
function routingClearance(boxes) {
  let clearance = 12
  for (let a = 0; a < boxes.length; a++) for (let b = a + 1; b < boxes.length; b++) {
    const xGap = Math.max(0, boxes[a].left - boxes[b].right, boxes[b].left - boxes[a].right)
    const yGap = Math.max(0, boxes[a].top - boxes[b].bottom, boxes[b].top - boxes[a].bottom)
    const gap = Math.max(xGap, yGap)
    if (gap > 0) clearance = Math.min(clearance, gap / 3)
  }
  return clearance
}

/** Orthogonal SVG routing around node rectangles, including manually moved nodes. */
export function edgeGeometry(graph, edge, options = {}) {
  const settings = geometry(options)
  const source = graph.nodes.find(node => node.id === edge.source)
  const target = graph.nodes.find(node => node.id === edge.target)
  if (!source || !target) return null
  const sourceSize = nodeSize(source, settings), targetSize = nodeSize(target, settings)
  if (source.position.x < target.position.x + targetSize.width && source.position.x + sourceSize.width > target.position.x &&
      source.position.y < target.position.y + targetSize.height && source.position.y + sourceSize.height > target.position.y) return null
  const incoming = graph.edges.filter(item => item.target === edge.target).sort((a, b) => {
    const aNode = graph.nodes.find(node => node.id === a.source)
    const bNode = graph.nodes.find(node => node.id === b.source)
    return (aNode.position.x - bNode.position.x) || compareId(a.source, b.source) || branchOrder(a) - branchOrder(b)
  })
  const incomingIndex = incoming.findIndex(item => item.source === edge.source && item.branch === edge.branch)
  const start = { x: source.position.x + sourceSize.width * (edge.branch === 'true' ? 0.3 : edge.branch === 'false' ? 0.7 : 0.5), y: source.position.y + sourceSize.height }
  const end = { x: target.position.x + targetSize.width * (incomingIndex + 1) / (incoming.length + 1), y: target.position.y }
  const nodeBoxes = graph.nodes.map(node => {
    const size = nodeSize(node, settings)
    return { left: node.position.x, right: node.position.x + size.width, top: node.position.y, bottom: node.position.y + size.height }
  })
  const clearance = routingClearance(nodeBoxes)
  const exit = { x: start.x, y: start.y + clearance }
  const entry = { x: end.x, y: end.y - clearance }
  const boxes = nodeBoxes.map(box => ({ left: box.left - clearance, right: box.right + clearance, top: box.top - clearance, bottom: box.bottom + clearance }))
  const finish = route => {
    const points = compactPoints([start, ...route, end])
    return {
      start, end, points,
      path: points.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.y}`).join(' '),
      label: placeLabel(start, points, edge, nodeBoxes),
    }
  }
  // Common forward and diagonal links need only two obstacle checks, not a grid search.
  for (const route of [[exit, { x: entry.x, y: exit.y }, entry], [exit, { x: exit.x, y: entry.y }, entry]]) {
    if (route.slice(1).every((point, index) => segmentClear(route[index], point, boxes))) return finish(route)
  }
  // The expanded rectangle boundaries are free routing lanes. Its interior is blocked.
  const xs = [...new Set([exit.x, entry.x, ...boxes.flatMap(box => [box.left, box.right])])].sort((a, b) => a - b)
  const ys = [...new Set([exit.y, entry.y, ...boxes.flatMap(box => [box.top, box.bottom])])].sort((a, b) => a - b)
  const key = (x, y) => y * xs.length + x
  const from = key(xs.indexOf(exit.x), ys.indexOf(exit.y)), to = key(xs.indexOf(entry.x), ys.indexOf(entry.y))
  const estimate = id => Math.abs(xs[id % xs.length] - entry.x) + Math.abs(ys[Math.floor(id / xs.length)] - entry.y)
  const distances = new Map([[from, 0]]), previous = new Map(), pending = [{ id: from, distance: 0, score: estimate(from) }]
  // Binary heap keeps larger authored graphs responsive without any routing dependency.
  const push = value => {
    pending.push(value)
    let index = pending.length - 1
    while (index > 0) {
      const parent = (index - 1) >> 1
      if (pending[parent].score <= value.score) break
      pending[index] = pending[parent]; index = parent
    }
    pending[index] = value
  }
  const pop = () => {
    const first = pending[0], last = pending.pop()
    if (pending.length) {
      let index = 0
      while (index * 2 + 1 < pending.length) {
        let child = index * 2 + 1
        if (child + 1 < pending.length && pending[child + 1].score < pending[child].score) child++
        if (pending[child].score >= last.score) break
        pending[index] = pending[child]; index = child
      }
      pending[index] = last
    }
    return first
  }
  while (pending.length) {
    const current = pop()
    if (current.distance !== distances.get(current.id)) continue
    if (current.id === to) break
    const x = current.id % xs.length, y = Math.floor(current.id / xs.length)
    for (const [nx, ny] of [[x, y + 1], [x - 1, y], [x + 1, y], [x, y - 1]]) {
      if (nx < 0 || ny < 0 || nx >= xs.length || ny >= ys.length) continue
      const a = { x: xs[x], y: ys[y] }, b = { x: xs[nx], y: ys[ny] }
      if (!segmentClear(a, b, boxes)) continue
      const id = key(nx, ny), distance = current.distance + Math.abs(a.x - b.x) + Math.abs(a.y - b.y)
      if (distance >= (distances.get(id) ?? Infinity)) continue
      distances.set(id, distance); previous.set(id, current.id); push({ id, distance, score: distance + estimate(id) })
    }
  }
  if (!distances.has(to)) return null // Overlapping manually placed nodes have no safe route.
  const route = []
  for (let id = to; id !== undefined; id = previous.get(id)) route.push({ x: xs[id % xs.length], y: ys[Math.floor(id / xs.length)] })
  return finish(route.reverse())
}

function validZoom(zoom) {
  if (!Number.isFinite(zoom) || zoom <= 0) throw new Error('畫布縮放比例無效（CANVAS_ZOOM）')
  return zoom
}
/** left/top are the scroll viewport's client rect; scroll offsets are CSS pixels. */
export function canvasPoint({ clientX, clientY }, { left, top, scrollLeft = 0, scrollTop = 0, zoom = 1 }) {
  validZoom(zoom)
  return { x: (clientX - left + scrollLeft) / zoom, y: (clientY - top + scrollTop) / zoom }
}
export function canvasDelta({ x, y }, zoom = 1) {
  validZoom(zoom)
  return { x: x / zoom, y: y / zoom }
}

export function initialGraph() {
  const nodes = ['input', 'prompt', 'model', 'output'].map((kind, i) => makeNode(kind, i))
  return layoutGraph({ nodes, edges: nodes.slice(1).map((n, i) => ({ source: nodes[i].id, target: n.id })) })
}
export function connect(graph, source, target, branch) {
  const from = graph.nodes.find((n) => n.id === source)
  const to = graph.nodes.find((n) => n.id === target)
  if (!from || !to || source === target || from.kind === 'output' || to.kind === 'input') throw new Error('請選擇不同且可連接的起點與終點（EDGE_TARGET）')
  if (from.kind === 'condition' && !['true', 'false'].includes(branch)) throw new Error('條件連線需選擇成立或不成立（EDGE_BRANCH）')
  const edge = { source, target, ...(from.kind === 'condition' ? { branch } : {}) }
  if (graph.edges.some((e) => e.source === source && (from.kind !== 'condition' || e.branch === branch))) throw new Error('這個出口已有連線，請先移除原連線（EDGE_EXISTS）')
  const reaches = (id, visited = new Set()) => id === source || (!visited.has(id) && (visited.add(id), graph.edges.filter((e) => e.source === id).some((e) => reaches(e.target, visited))))
  if (reaches(target)) throw new Error('這條連線會形成循環，請改接後續步驟（GRAPH_CYCLE）')
  graph.edges.push(edge)
}
export function removeNode(graph, id) {
  graph.nodes = graph.nodes.filter((n) => n.id !== id)
  graph.edges = graph.edges.filter((e) => e.source !== id && e.target !== id)
}
const sensitiveName = /token|password|passwd|secret|authorization|credential|cookie|api[\s_.-]?key|apikey|密碼|憑證|金鑰/i
const sensitiveAssignment = /((?:["']?)(?:api[\s_.-]?key|apikey|credentials?|cookies?|authorization|password|passwd|secrets?|tokens?|密碼|憑證|金鑰)(?:["']?)\s*[:=：]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n,;，；}\]]+)/gi
const cookieHeader = /((?:set-cookie|cookie)\s*[:=：]\s*)[^\r\n]+/gi
export function maskTrace(value, depth = 0) {
  if (depth > 12) return '［內容過深，已省略］'
  if (Array.isArray(value)) return value.map(item => maskTrace(item, depth + 1))
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sensitiveName.test(key) ? '［已遮罩］' : maskTrace(item, depth + 1)]))
  if (typeof value === 'string') {
    if (value.length < 200000 && /^[\s]*[\[{]/.test(value)) {
      try { return JSON.stringify(maskTrace(JSON.parse(value), depth + 1)) } catch { /* 非 JSON 文字仍檢查固定的敏感欄位標記。 */ }
    }
    return value
      .replace(/Bearer\s+[^\s,;，；]+/gi, 'Bearer ［已遮罩］')
      .replace(cookieHeader, '$1［已遮罩］')
      .replace(sensitiveAssignment, '$1［已遮罩］')
      .replace(/\b[A-Z][12]\d{8}\b/g, '［身分證字號已遮罩］')
      .replace(/\b09\d{8}\b/g, '［手機已遮罩］')
  }
  return value
}

export const statusLabel = (status) => ({ draft: '草稿', pending: '待主管審核', submitted: '待主管審核', approved: '主管已核准', rejected: '已退回', registered: '已註冊，待平台核准', published: '已發布', disabled: '已停用' }[status] || status || '草稿')
