const DISK_LABELS = {
  ingestion: '知識庫',
  attachments: '附件',
  root: '系統',
}

function oneDecimal(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return null
  return n.toFixed(1)
}

/** 磁碟掛載的顯示名。與儀表板同一套；路徑不顯示。 */
export function diskSourceLabel(raw) {
  if (typeof raw !== 'string' || raw === '') return ''
  if (raw.includes('/') || raw.includes('\\')) return '未命名'
  return DISK_LABELS[raw] || raw
}

/** 掛載列。路徑（含斜線）不顯示。 */
export function diskMountRows(mounts) {
  if (!Array.isArray(mounts)) return []
  return mounts.map((row) => {
    const raw = typeof row?.label === 'string' ? row.label : ''
    const label = diskSourceLabel(raw) || '未命名'
    const used = oneDecimal(row?.used_pct)
    const free = oneDecimal(row?.free_gib)
    return {
      label,
      usedLabel: used == null ? '—' : `${used}%`,
      freeLabel: free == null ? '—' : `${free} GiB`,
    }
  })
}

/** 憑證卡。連不上 nginx 時不是到期，畫面寫無法讀取。 */
export function summarizeCertificate(raw) {
  if (raw == null) {
    return { date: null, dateLabel: '—', tone: 'default' }
  }
  if (raw.status !== 'ok' || !raw.not_after) {
    return { date: null, dateLabel: '無法讀取', tone: 'default' }
  }
  const tone = raw.severity === 'critical'
    ? 'danger'
    : raw.severity === 'high'
      ? 'warn'
      : 'default'
  return { date: raw.not_after, dateLabel: null, tone }
}
