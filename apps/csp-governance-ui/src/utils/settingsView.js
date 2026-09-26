// 平台設定總覽的純函式。後端只回 C 類設定（目前十九顆）：每顆可編輯，
// 儲存後下一個請求直接讀到新值；這裡不再推導 boot、restart 或唯讀區。

export const UNKNOWN_SECTION_ID = 'unknown-class'

export const SECTION_DEFS = [
  {
    id: 'models',
    classes: ['C'],
    title: '模型與檢索',
    hint: '模型逾時、檢索門檻與 Router 說明。每項的生效時機寫在該列。',
    editable: true,
  },
  {
    id: 'account',
    classes: ['C'],
    title: '帳號',
    hint: '登入權杖與部門層級。權杖類設定在下次簽發時才套用。',
    editable: true,
  },
  {
    id: 'conversation',
    classes: ['C'],
    title: '對話行為',
    hint: '附件預算與動作頻率等對話期間限制。',
    editable: true,
  },
]

const UNKNOWN_SECTION = {
  id: UNKNOWN_SECTION_ID,
  classes: [],
  title: '未識別的設定類別',
  hint: '後端回傳了尚未核准顯示或編輯方式的類別。',
  editable: false,
}

export function sectionIdFor(item) {
  if (!item || item.class !== 'C') return UNKNOWN_SECTION_ID
  const k = item.key || ''
  if (k.startsWith('auth.') || k === 'limits.department_max_depth') return 'account'
  if (k.startsWith('limits.')) return 'conversation'
  return 'models'
}

export function groupIntoSections(items) {
  const rows = Array.isArray(items) ? items : []
  const buckets = new Map(SECTION_DEFS.map((section) => [section.id, []]))
  const overflow = []
  for (const item of rows) {
    const id = sectionIdFor(item)
    if (buckets.has(id)) buckets.get(id).push(item)
    else overflow.push(item)
  }
  const sections = SECTION_DEFS.map((section) => ({
    ...section,
    items: buckets.get(section.id),
  }))
  return overflow.length ? [...sections, { ...UNKNOWN_SECTION, items: overflow }] : sections
}

export function canEdit(item) {
  return item?.editable === true && sectionIdFor(item) !== UNKNOWN_SECTION_ID
}

export function formatSettingValue(value) {
  if (value === null || value === undefined) return '—'
  if (value === '') return '（空字串）'
  if (typeof value === 'string' || typeof value === 'boolean' || typeof value === 'number') {
    return String(value)
  }
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const SOURCE_LABELS = {
  db: 'DB',
  env: 'env／compose',
  default: '程式預設',
}

export function sourceLabel(source) {
  if (source === null || source === undefined || source === '') return '—'
  return SOURCE_LABELS[source] ?? String(source)
}

export function valueCells(item) {
  if (!item) return []
  return [
    {
      field: 'effective',
      label: '現在生效',
      text: isTextSetting(item) ? textPreview(item.effective) : formatSettingValue(item.effective),
      className: 'setting-cell--effective',
    },
    {
      field: 'stored',
      label: 'DB 存值',
      text: isTextSetting(item) ? textPreview(item.stored) : formatSettingValue(item.stored),
      className: item.stored_usable === false ? 'setting-cell--unusable' : 'setting-cell--stored',
    },
    {
      field: 'source',
      label: '來源',
      text: sourceLabel(item.source),
      className: 'setting-cell--source',
    },
  ]
}

export function draftValue(item) {
  const value = item?.effective
  return value === null || value === undefined ? '' : String(value)
}

/** 多行文字設定（Router 的 system prompt）：用 textarea 編輯、可一鍵重設為出貨預設。 */
export function isTextSetting(item) {
  return item?.value_type === 'text'
}

/** 長文字在表格裡只給前幾行預覽＋字數；全文在編輯框。 */
export function textPreview(value, maxChars = 120) {
  if (typeof value !== 'string') return formatSettingValue(value)
  const oneLine = value.replace(/\s+/g, ' ').trim()
  const head = oneLine.length > maxChars ? `${oneLine.slice(0, maxChars)}…` : oneLine
  return `${head}（共 ${value.length} 字）`
}

/** 目前生效值是不是就是出貨預設（決定「重設為出貨預設」要不要能按）。 */
export function isAtDefault(item) {
  return typeof item?.default === 'string' && item.effective === item.default
}

export function countMismatchWarning(overview) {
  const total = overview?.total
  if (typeof total !== 'number') return null
  const received = Array.isArray(overview.items) ? overview.items.length : 0
  return received === total
    ? null
    : `後端說有 ${total} 顆設定，這一頁只收到 ${received} 顆 —— 下面不是全部。`
}

export function overviewState({ loaded, error, items }) {
  if (!loaded) return 'loading'
  if (error) return 'failed'
  if (!Array.isArray(items) || items.length === 0) return 'empty'
  return 'ready'
}

export function overviewStateMessage(state) {
  return {
    loading: '正在讀取設定…',
    failed: '後端沒有回傳可用的設定總覽。',
    empty: '目前沒有可顯示的設定。',
  }[state] ?? ''
}

export function extractDetail(error, fallback) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) return detail.map((entry) => entry?.msg || String(entry)).join('；')
  return fallback
}

export function replaceRow(rows, replacement) {
  return rows.map((row) => (row.key === replacement.key ? replacement : row))
}

export function saveNotice() {
  return { tone: 'ok', message: '已儲存，下一個請求就生效' }
}
export function isBoolSetting(item) {
  return item?.value_type === 'bool' || item?.value_type === 'boolean' || typeof item?.effective === 'boolean'
}

export function settingUnit(item) {
  const k = item?.key || ''
  if (k.endsWith('_timeout')) return '秒'
  if (k.includes('expire_minutes')) return '分鐘'
  if (k.includes('expire_days')) return '天'
  if (k.includes('top_k')) return '筆'
  if (k.includes('max_depth')) return '層'
  if (k.includes('per_min')) return '次／分'
  if (k.includes('ratio')) return '比例'
  return ''
}

export function applyWhenLabel(item) {
  const k = item?.key || ''
  if (k.startsWith('router.prompt.')) return '儲存後 Router 約 30 秒內套用'
  if (k.startsWith('auth.')) return '下次簽發權杖時生效'
  return '儲存後下一個請求生效'
}
