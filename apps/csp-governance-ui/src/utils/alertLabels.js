/** 警報清單的顯示文字。嚴重度與頁首橫幅、篩選選項同一套。 */

export const SEVERITY_LABEL = {
  critical: '嚴重',
  high: '高',
  medium: '中',
  low: '低',
}

export const CATEGORY_LABEL = {
  certificate: '憑證',
  disk: '磁碟',
  health: '健康',
  database: '資料庫',
  platform: '平台',
  backup: '備份',
  agent: '助手',
  gateway: '閘道',
  model: '模型',
}

export function categoryLabel(code) {
  if (typeof code !== 'string' || code === '') return ''
  return CATEGORY_LABEL[code] || code
}

export function severityLabel(code) {
  if (typeof code !== 'string' || code === '') return ''
  return SEVERITY_LABEL[code] || code
}
