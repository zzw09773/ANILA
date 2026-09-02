// 平台設定總覽的純函式。後端只回 C 類設定（目前十五顆）：每顆可編輯，
// 儲存後下一個請求直接讀到新值；這裡不再推導 boot、restart 或唯讀區。

export const UNKNOWN_SECTION_ID = 'unknown-class'

export const SECTION_DEFS = [
  {
    id: 'apply-now',
    classes: ['C'],
    title: '改完立刻生效',
    hint: '每一次請求都重新讀 DB，按下儲存後下一個請求就是新值。',
    editable: true,
  },
]

const SECTION_BY_CLASS = new Map(
  SECTION_DEFS.flatMap((section) => section.classes.map((cls) => [cls, section.id])),
)

const UNKNOWN_SECTION = {
  id: UNKNOWN_SECTION_ID,
  classes: [],
  title: '未識別的設定類別',
  hint: '後端回傳了尚未核准顯示或編輯方式的類別。',
  editable: false,
}

export function sectionIdFor(item) {
  return SECTION_BY_CLASS.get(item?.class) ?? UNKNOWN_SECTION_ID
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
