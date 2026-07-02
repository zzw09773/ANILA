/**
 * Agent 審批狀態的中央對照表（Slice 5b）。
 *
 * approval_status 由三值擴為七值（doc 05 §3 state machine）：能連上、trace 過、
 * 安全審查分關把守。此模組是純函式（無 Vue／DOM 依賴），故可獨立做單元測試——
 * 本套件目前無測試框架（package.json 無 test script），保持純淨以便日後接上
 * vitest 時零改動即可測。
 *
 * 對照關係（狀態 → 繁中標籤 / TermBadge variant / 顏色語意）：
 *
 *   draft                     草稿        ''(default) 中性灰
 *   pending_connection_test   待連線測試   info        藍
 *   pending_trace_test        待軌跡測試   warn        琥珀
 *   pending_security_review   待安全審查   accent      紫
 *   approved                  已核准       ok          綠
 *   rejected                  已駁回       danger      紅
 *   disabled                  已停用       muted       暗灰
 *
 * 舊資料相容：三值時代的 `pending` 視為待審查（warn），未知值回退為 '—' / 中性。
 */

// 七值的正規順序（供篩選下拉、狀態機顯示依序列出）。
export const APPROVAL_STATUSES = [
  'draft',
  'pending_connection_test',
  'pending_trace_test',
  'pending_security_review',
  'approved',
  'rejected',
  'disabled',
]

const LABELS = {
  draft: '草稿',
  pending_connection_test: '待連線測試',
  pending_trace_test: '待軌跡測試',
  pending_security_review: '待安全審查',
  approved: '已核准',
  rejected: '已駁回',
  disabled: '已停用',
  // 舊三值資料相容
  pending: '待審查',
}

// TermBadge variant（沿用既有 badge 慣例：ok/warn/danger/info/accent/muted）。
const VARIANTS = {
  draft: '',
  pending_connection_test: 'info',
  pending_trace_test: 'warn',
  pending_security_review: 'accent',
  approved: 'ok',
  rejected: 'danger',
  disabled: 'muted',
  pending: 'warn',
}

// 「待審查中」的狀態集合——顯示 approve／reject 控制、計入 pending KPI。
// 含舊值 `pending`，讓既有資料仍走同一治理流程。
const PENDING_REVIEW_STATUSES = new Set([
  'pending_connection_test',
  'pending_trace_test',
  'pending_security_review',
  'pending',
])

/**
 * 狀態 → 繁中標籤。未知／缺值（舊資料）回退為 '—'。
 * @param {string|null|undefined} status
 * @returns {string}
 */
export function approvalLabel(status) {
  if (!status) return '—'
  return LABELS[status] || status
}

/**
 * 狀態 → TermBadge variant 字串。未知值回退為 ''(中性 default)。
 * @param {string|null|undefined} status
 * @returns {string}
 */
export function approvalVariant(status) {
  if (!status) return ''
  return VARIANTS[status] ?? ''
}

/**
 * 此狀態是否處於「待審查」關卡（決定是否顯示 approve／reject 控制）。
 * @param {string|null|undefined} status
 * @returns {boolean}
 */
export function isPendingReview(status) {
  return PENDING_REVIEW_STATUSES.has(status)
}

/**
 * 是否可核准 = 處於待審查關卡「且」已通過軌跡測試。
 * 軌跡測試未過（!traceTestPassedAt）→ 不可核准（後端亦會回 409）。
 * @param {string|null|undefined} status
 * @param {string|null|undefined} traceTestPassedAt  ISO 時間字串；未過為 null
 * @returns {boolean}
 */
export function isApprovable(status, traceTestPassedAt) {
  if (!traceTestPassedAt) return false
  return isPendingReview(status)
}
