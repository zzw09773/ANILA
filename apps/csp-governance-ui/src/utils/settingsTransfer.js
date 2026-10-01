const ENTITY_LABEL = {
  trusted_host: '信任主機',
  model: '模型',
  model_role: '模型角色',
  router_grant: '全院授權',
  external_service: '外部服務',
  department: '部門',
  banner: '公告',
}

const ACTION_LABEL = {
  create: '新增',
  update: '更新',
  skip: '略過',
  'needs-credential': '需另外填入金鑰',
}

export function formatPlanItem(item) {
  const entity = ENTITY_LABEL[item?.entity] || '項目'
  const name = item?.label || item?.key || ''
  const action = ACTION_LABEL[item?.action] || item?.action || ''
  const message = item?.message && item.message !== action ? `（${item.message}）` : ''
  return `${entity} ${name}：${action}${message}`
}

export function downloadSettingsFile(document) {
  const blob = new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = window.document.createElement('a')
  link.href = url
  link.download = 'anila-settings.json'
  link.click()
  URL.revokeObjectURL(url)
}
