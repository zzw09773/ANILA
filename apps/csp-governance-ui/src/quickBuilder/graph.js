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
export function initialGraph() {
  const nodes = ['input', 'prompt', 'model', 'output'].map((kind, i) => makeNode(kind, i))
  return { nodes, edges: nodes.slice(1).map((n, i) => ({ source: nodes[i].id, target: n.id })) }
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
