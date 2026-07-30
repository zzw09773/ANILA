/**
 * Agent 審批狀態的中央對照表（OE-1 三態）。
 *
 * SYSTEM-MAP：註冊 → admin 指派 → 可用。無連線／軌跡／安全審查三關。
 * 此模組是純函式（無 Vue／DOM 依賴）。
 *
 *   registered   已註冊     info     藍
 *   approved     已核准     ok       綠
 *   disabled     已停用     muted    暗灰
 *
 * 舊七值／三值相容：gate 殘餘與 pending 視為待核准（registered 語意）。
 */

export const APPROVAL_STATUSES = [
  'registered',
  'approved',
  'disabled',
]

const LABELS = {
  registered: '已註冊',
  approved: '已核准',
  disabled: '已停用',
  // 舊七值／三值相容（migration 前或快取殘值）
  draft: '已註冊',
  pending: '已註冊',
  pending_connection_test: '已註冊',
  pending_trace_test: '已註冊',
  pending_security_review: '已註冊',
  rejected: '已停用',
}

const VARIANTS = {
  registered: 'info',
  approved: 'ok',
  disabled: 'muted',
  draft: 'info',
  pending: 'info',
  pending_connection_test: 'info',
  pending_trace_test: 'info',
  pending_security_review: 'info',
  rejected: 'muted',
}

// 可顯示核准／停用控制的狀態（含舊值）。
const PENDING_REVIEW_STATUSES = new Set([
  'registered',
  'draft',
  'pending',
  'pending_connection_test',
  'pending_trace_test',
  'pending_security_review',
])

/**
 * 狀態 → 繁中標籤。未知／缺值回退為 '—'。
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
 * 此狀態是否處於「待核准」（決定是否顯示 approve／reject 控制）。
 * @param {string|null|undefined} status
 * @returns {boolean}
 */
export function isPendingReview(status) {
  return PENDING_REVIEW_STATUSES.has(status)
}

/**
 * 是否可核准 = 處於待核准（或已停用可重啟）。OE-1：不再要求軌跡測試。
 * @param {string|null|undefined} status
 * @param {string|null|undefined} [_traceTestPassedAt]  保留參數相容；忽略
 * @returns {boolean}
 */
export function isApprovable(status, _traceTestPassedAt) {
  if (status === 'disabled' || status === 'rejected') return true
  return isPendingReview(status)
}
