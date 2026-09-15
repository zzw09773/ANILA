// 思考等級探測結果的顯示與寫回欄位。
//
// 後端契約（CSP ModelResponse）：
//   thinking_levels_supported: string[] | null  // null／[]＝未探測
//   thinking_user_selectable: boolean           // 預設 true
// 探測由後端在新增／整批帶入／改端點時自動跑；前端只顯示，並提供維運重探。

export const THINKING_LEVEL_MAX_CHIPS = 4

export const THINKING_EFFORT_OPTIONS = [
  { value: 'none', label: 'NONE' },
  { value: 'low', label: 'low' },
  { value: 'medium', label: 'medium' },
  { value: 'high', label: 'high' },
  { value: 'xhigh', label: 'xhigh' },
  { value: 'max', label: 'max' },
]

const EFFORT_LABEL = Object.fromEntries(
  THINKING_EFFORT_OPTIONS.map((o) => [o.value, o.label]),
)

/** 列表／表單 chip：none 顯示為「關」。 */
export function formatThinkingLevel(level) {
  if (level === 'none' || level === 'off') return '關'
  return String(level ?? '')
}

/**
 * 後端永不回 []（可達必含 none，全部不可達存 NULL）。
 * 此處與 anila-shell `runtime/thinkingTier.js` 同義：null 與 [] 都當未探測。
 * @param {string[]|null|undefined} supported
 */
export function isThinkingLevelsUnprobed(supported) {
  return supported == null || (Array.isArray(supported) && supported.length === 0)
}

/**
 * @param {string[]|null|undefined} supported
 * @returns {{ status: 'unprobed'|'probed', visible: string[], overflow: number }}
 */
export function thinkingLevelsView(supported) {
  if (isThinkingLevelsUnprobed(supported)) {
    return { status: 'unprobed', visible: [], overflow: 0 }
  }
  const list = Array.isArray(supported) ? supported : []
  const visible = list.slice(0, THINKING_LEVEL_MAX_CHIPS)
  const overflow = Math.max(0, list.length - THINKING_LEVEL_MAX_CHIPS)
  return { status: 'probed', visible, overflow }
}

/** 下拉標籤：已探測但不在支援集的選項標「（端點不接受）」，仍可選。 */
export function thinkingEffortOptionLabel(value, supported) {
  const base = EFFORT_LABEL[value] || value
  if (isThinkingLevelsUnprobed(supported)) return base
  const accepted = new Set(Array.isArray(supported) ? supported : [])
  const key = value === 'off' ? 'none' : value
  if (!accepted.has(key) && !(key === 'none' && accepted.has('off'))) {
    return `${base}（端點不接受）`
  }
  return base
}

/** 舊後端或漏欄位時視為開啟（契約預設 TRUE）。 */
export function thinkingUserSelectableFromModel(model) {
  return model?.thinking_user_selectable !== false
}

/**
 * 寫入 payload：帶開關、不回送唯讀探測結果。不改 thinking_effort。
 * @param {Record<string, unknown>} payload
 * @param {{ thinking_user_selectable?: boolean }} form
 */
export function withThinkingWriteFields(payload, form) {
  const next = { ...payload }
  next.thinking_user_selectable = form?.thinking_user_selectable !== false
  delete next.thinking_levels_supported
  return next
}
