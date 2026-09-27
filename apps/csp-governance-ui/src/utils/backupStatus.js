// 儀表板「最後一次備份」的顯示。時間本身交給 formatDate（台北）。

const UNITS = ['KiB', 'MiB', 'GiB', 'TiB']

/** @param {number|null|undefined} bytes */
export function formatBackupSize(bytes) {
  if (bytes == null || bytes === '') return '—'
  const n = Number(bytes)
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 1024) return `${Math.round(n)} B`
  let value = n / 1024
  let index = 0
  while (value >= 1024 && index < UNITS.length - 1) {
    value /= 1024
    index += 1
  }
  const text = value >= 10 ? value.toFixed(0) : value.toFixed(1)
  return `${text} ${UNITS[index]}`
}

/**
 * @param {object|null|undefined} payload  GET /api/admin/backup-status
 * @returns {{
 *   time: string|null,
 *   resultLabel: string,
 *   sizeLabel: string,
 *   tone: 'ok'|'warn',
 *   hint: string,
 * }}
 */
export function summarizeBackupStatus(payload) {
  if (!payload || payload.reason === 'missing' || payload.reason === 'unreadable' || payload.last_result === 'none') {
    return {
      time: null,
      resultLabel: '尚無備份',
      sizeLabel: '—',
      tone: 'warn',
      hint: payload?.reason === 'unreadable' ? '狀態檔讀不到' : '還沒有讀到成功的備份',
    }
  }
  const failed = payload.last_result === 'failure' || payload.reason === 'failed'
  const stale = Boolean(payload.stale) || payload.reason === 'stale'
  if (failed) {
    return {
      time: payload.last_run_at || null,
      resultLabel: '失敗',
      sizeLabel: '—',
      tone: 'warn',
      hint: '最近一次沒有成功',
    }
  }
  if (stale) {
    return {
      time: payload.last_success_at || payload.last_run_at || null,
      resultLabel: '過期',
      sizeLabel: formatBackupSize(payload.last_size_bytes),
      tone: 'warn',
      hint: '距離上次成功已超過 36 小時',
    }
  }
  return {
    time: payload.last_run_at || null,
    resultLabel: '成功',
    sizeLabel: formatBackupSize(payload.last_size_bytes),
    tone: 'ok',
    hint: '每天一輪',
  }
}
