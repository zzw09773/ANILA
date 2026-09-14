export const STATUS_LABELS = {
  open: '待處理',
  acknowledged: '已確認',
  resolved: '已解決',
  active: '使用中',
  inactive: '已停用',
  archived: '已封存',
  indexed: '已索引',
  success: '成功',
  failed: '失敗',
  pending: '待審核',
}

export function statusLabel(value) {
  if (value == null || value === '') return '—'
  return STATUS_LABELS[value] ?? String(value)
}
