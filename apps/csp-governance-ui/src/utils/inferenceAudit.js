// Inference audit filter serialization + display helpers.
// Pure functions for the 稽核查詢 view and node --test coverage.

export const INFERENCE_ACTIONS = [
  { value: 'inference.chat', label: '對話' },
  { value: 'inference.agent', label: 'Agent' },
  { value: 'inference.rag_query', label: 'RAG 查詢' },
  { value: 'inference.studio', label: 'Studio' },
  { value: 'inference.image', label: '影像' },
]

export const INFERENCE_ACTION_FILTER_OPTIONS = [
  { value: '', label: '全部' },
  ...INFERENCE_ACTIONS,
]

const ACTION_LABELS = Object.fromEntries(
  INFERENCE_ACTIONS.map((a) => [a.value, a.label]),
)

export function actionLabel(action) {
  return ACTION_LABELS[action] || action || '—'
}

export function statusLabel(status) {
  if (status === 'success') return '成功'
  if (status === 'denied') return '拒絕'
  if (status === 'error') return '錯誤'
  return status || '—'
}

/** TermBadge variant: denied/error must be visually distinct from success. */
export function statusVariant(status) {
  if (status === 'success') return 'ok'
  if (status === 'denied') return 'warn'
  if (status === 'error') return 'danger'
  return 'info'
}

/**
 * Convert ``datetime-local`` value (no tz) to tz-aware ISO8601 (UTC ``Z``).
 * Empty / invalid → ``undefined`` (omitted from query).
 */
export function localInputToIso(value) {
  if (value == null) return undefined
  const raw = String(value).trim()
  if (!raw) return undefined
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return undefined
  return d.toISOString()
}

/**
 * Build axios/query params for GET /api/admin/audit/inference[ /export ].
 * Empty strings are omitted. Export callers pass ``includePagination: false``.
 */
export function buildInferenceAuditParams(filters = {}, options = {}) {
  const { includePagination = true } = options
  const params = {}

  const username = typeof filters.username === 'string' ? filters.username.trim() : ''
  if (username) params.username = username

  const ip = typeof filters.ip === 'string' ? filters.ip.trim() : ''
  if (ip) params.ip = ip

  if (filters.action) params.action = filters.action

  const q = typeof filters.q === 'string' ? filters.q.trim() : ''
  if (q) params.q = q

  const fromIso = localInputToIso(filters.from)
  if (fromIso) params.from = fromIso

  const toIso = localInputToIso(filters.to)
  if (toIso) params.to = toIso

  if (includePagination) {
    const limit = Number(filters.limit)
    params.limit = Number.isFinite(limit) && limit > 0 ? Math.min(limit, 500) : 50
    const offset = Number(filters.offset)
    params.offset = Number.isFinite(offset) && offset >= 0 ? offset : 0
  }

  return params
}

/** One-line preview; full text stays available for expand. */
export function truncateDetail(detail, maxLen = 80) {
  if (detail == null || detail === '') return '—'
  const text = String(detail).replace(/\s+/g, ' ').trim()
  if (text.length <= maxLen) return text
  return `${text.slice(0, maxLen)}…`
}

/** Pretty-print ``metadata_json`` string; return null if empty/unparseable. */
export function formatMetadata(metadataJson) {
  if (metadataJson == null || metadataJson === '') return null
  if (typeof metadataJson === 'object') {
    try {
      return JSON.stringify(metadataJson, null, 2)
    } catch {
      return null
    }
  }
  try {
    return JSON.stringify(JSON.parse(String(metadataJson)), null, 2)
  } catch {
    return String(metadataJson)
  }
}
