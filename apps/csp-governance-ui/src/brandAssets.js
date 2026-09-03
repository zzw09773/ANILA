function brandUrl(file) {
  const base = import.meta.env.BASE_URL || '/'
  const prefix = base.endsWith('/') ? base : `${base}/`
  return `${prefix}brand/${file}`
}

export const ANILA_LOGO_PNG = brandUrl('anila-logo.png')
export const ANILA_MARK_PNG = brandUrl('anila-mark.png')
export const ANILA_LOGO_MP4 = brandUrl('anila-logo.mp4')
