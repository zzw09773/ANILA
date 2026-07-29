// Service Registry 純邏輯：badge / label 映射與欄位鎖定判斷。
// 抽成獨立模組，讓 PlatformLinksView 保持宣告式，並保留給未來單元測試。
//
// doc 07（registered-gui-service-platform）契約摘要：
//   - registered_services 是 platform_links 的超集（additive upgrade）。
//   - config_source = 'env_seeded'（部署 env 播種）| 'db'（UI 為唯一事實來源）。
//   - env_seeded 服務多數欄位唯讀鎖定，僅 db_editable_fields 白名單可由 admin 覆寫。
//   - launch_mode = 'new_tab'（新分頁）| 'iframe'（內嵌）。
//   - classification_ceiling 對齊後端四級 ClassificationLevel。

export const LAUNCH_MODES = [
  { value: 'new_tab', label: '新分頁' },
  { value: 'iframe', label: '內嵌 iframe' },
]

// 四級分類（對齊後端 ClassificationLevel 契約宣告順序）。
export const CLASSIFICATION_LEVELS = [
  '無機密',
  '營業秘密',
  '密',
  '機密',
]

// env_seeded 服務未宣告 db_editable_fields 時的安全預設：只有停用可覆寫。
const DEFAULT_STICKY_FIELDS = ['is_active']

export function configSourceForRow(service) {
  return service?.config_source === 'env_seeded' ? 'env_seeded' : 'db'
}

export function launchModeLabel(mode) {
  const found = LAUNCH_MODES.find((m) => m.value === mode)
  return found ? found.label : '新分頁'
}

// config_source 顯示 badge：環境種子（鎖定）vs 資料庫（可編輯）。
export function configSourceBadge(service) {
  if (configSourceForRow(service) === 'env_seeded') {
    return { label: '環境種子', variant: 'warn', locked: true }
  }
  return { label: '資料庫', variant: 'info', locked: false }
}

// env_seeded 服務中仍可由 admin 覆寫（admin-sticky）的欄位白名單。
export function stickyEditableFields(service) {
  const fields = service?.db_editable_fields
  if (Array.isArray(fields) && fields.length > 0) return fields
  return DEFAULT_STICKY_FIELDS
}

// 某欄位在此服務上是否唯讀鎖定：只有 env_seeded 且不在白名單時才鎖。
export function isFieldLocked(service, field) {
  if (configSourceForRow(service) !== 'env_seeded') return false
  return !stickyEditableFields(service).includes(field)
}

export function classificationLabel(level) {
  return level && CLASSIFICATION_LEVELS.includes(level) ? level : '未設定'
}

// 服務列正規化：registered_services 缺欄位時退回 platform_links 既有值，
// 確保 7a 後端尚未上線（僅 legacy /api/platform-links）時 UI 也能運作。
export function normalizeService(row) {
  const source = row || {}
  return {
    ...source,
    launch_mode: source.launch_mode === 'iframe' ? 'iframe' : 'new_tab',
    config_source: configSourceForRow(source),
    service_admin_user_ids: Array.isArray(source.service_admin_user_ids)
      ? source.service_admin_user_ids
      : [],
    classification_ceiling: source.classification_ceiling || '',
    healthcheck_url: source.healthcheck_url || '',
    required_roles: Array.isArray(source.required_roles) ? source.required_roles : [],
  }
}
