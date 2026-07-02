/**
 * 模型健康狀態的中央對照表（Slice 6b · Model Gateway hardening）。
 *
 * doc 04 §2/§9：model 端點 health_status 由現況三值（online/connecting/offline）
 * 收斂為五態。此模組是純函式（無 Vue／DOM 依賴），故可獨立做單元測試——
 * 風格對齊 src/utils/approvalStatus.js（Slice 5b）。
 *
 * 對照關係（狀態 → 繁中標籤 / TermBadge variant / 顏色語意）：
 *
 *   unknown    未知    ''(default) 中性灰   還沒探測過
 *   healthy    健康    ok          綠       探測通過
 *   degraded   降級    warn        琥珀     部分能力受損／延遲偏高
 *   unhealthy  異常    danger      紅       探測失敗
 *   disabled   已停用  muted       暗灰     被管理者停用，不做探測
 *
 * 舊資料相容（doc 04 §9 現況字彙）：health loop 舊寫入 online/connecting/offline，
 * 正規化為五態；未知／缺值一律回退 unknown（不臆測為健康）。
 */

// 五態的正規順序（供篩選下拉、KPI、狀態機依序列出）。
export const HEALTH_STATUSES = [
  'unknown',
  'healthy',
  'degraded',
  'unhealthy',
  'disabled',
]

// 舊三值 → 五態的正規化別名。connecting（探測中/尚未確認）視為 degraded，
// 保留其原本的琥珀色語意；offline → unhealthy；online → healthy。
const LEGACY_ALIASES = {
  online: 'healthy',
  connecting: 'degraded',
  offline: 'unhealthy',
}

const LABELS = {
  unknown: '未知',
  healthy: '健康',
  degraded: '降級',
  unhealthy: '異常',
  disabled: '已停用',
}

// TermBadge variant（沿用既有 badge 慣例：ok/warn/danger/muted/'')。
const VARIANTS = {
  unknown: '',
  healthy: 'ok',
  degraded: 'warn',
  unhealthy: 'danger',
  disabled: 'muted',
}

/**
 * 把任意（含舊值／缺值／未知值）健康字彙正規化為五態之一。
 * 防禦性：後端欄位尚未落地（parallel worker 6a）或回傳空值時，一律 unknown。
 * @param {string|null|undefined} status
 * @returns {'unknown'|'healthy'|'degraded'|'unhealthy'|'disabled'}
 */
export function normalizeHealth(status) {
  if (!status) return 'unknown'
  if (LEGACY_ALIASES[status]) return LEGACY_ALIASES[status]
  return HEALTH_STATUSES.includes(status) ? status : 'unknown'
}

/**
 * 狀態 → 繁中標籤。舊值先正規化再對照；未知／缺值回退「未知」。
 * @param {string|null|undefined} status
 * @returns {string}
 */
export function healthLabel(status) {
  return LABELS[normalizeHealth(status)]
}

/**
 * 狀態 → TermBadge variant 字串。unknown 回退 ''(中性 default)。
 * @param {string|null|undefined} status
 * @returns {string}
 */
export function healthVariant(status) {
  return VARIANTS[normalizeHealth(status)]
}

/**
 * 便利判定：此狀態是否為健康（探測通過）。供 KPI／toast 語氣使用。
 * @param {string|null|undefined} status
 * @returns {boolean}
 */
export function isHealthy(status) {
  return normalizeHealth(status) === 'healthy'
}
