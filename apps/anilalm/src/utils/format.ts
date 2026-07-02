// Tiny formatting helpers used in cards/lists. Pure functions, no deps.

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = bytes
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

const ZH_RTF = new Intl.RelativeTimeFormat('zh-TW', { numeric: 'auto' })
const RANGES: { unit: Intl.RelativeTimeFormatUnit; ms: number }[] = [
  { unit: 'year', ms: 365 * 24 * 60 * 60 * 1000 },
  { unit: 'month', ms: 30 * 24 * 60 * 60 * 1000 },
  { unit: 'week', ms: 7 * 24 * 60 * 60 * 1000 },
  { unit: 'day', ms: 24 * 60 * 60 * 1000 },
  { unit: 'hour', ms: 60 * 60 * 1000 },
  { unit: 'minute', ms: 60 * 1000 },
]

/** Returns "剛剛" / "5 分鐘前" / "昨天" — relative to now. */
export function timeAgo(iso: string | Date): string {
  const date = typeof iso === 'string' ? new Date(iso) : iso
  const diff = date.getTime() - Date.now()
  for (const { unit, ms } of RANGES) {
    if (Math.abs(diff) >= ms) {
      return ZH_RTF.format(Math.round(diff / ms), unit)
    }
  }
  return '剛剛'
}

const ACCENT_PALETTE = ['#7C7BFF', '#3DD68C', '#F4B740', '#FF8FAB', '#5BC0EB', '#C792EA']

/** Stable per-collection accent so the same project always gets the same colour. */
export function accentForId(id: number | string): string {
  const s = String(id)
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return ACCENT_PALETTE[h % ACCENT_PALETTE.length]
}

export function shortName(name: string | null | undefined, max = 24): string {
  if (!name) return ''
  if (name.length <= max) return name
  return name.slice(0, max - 1) + '…'
}
