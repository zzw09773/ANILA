import type { SlideLayoutKind, SlidePreview } from '../types'

/** Layouts the preview actually paints — same fallback as the PPTX renderer. */
export type PaintedSlideKind =
  | 'section_break'
  | 'stat_callout'
  | 'quote'
  | 'two_column'
  | 'icon_rows'
  | 'standard'

const MARKER = /^[●◦▪•]\s+/u

export function displayBullet(text: string): string {
  return text.replace(MARKER, '').trim()
}

export function resolveSlideStage(slide: SlidePreview): PaintedSlideKind {
  const kind = (slide.layoutKind || 'standard') as SlideLayoutKind
  if (kind === 'section_break') return 'section_break'
  if (kind === 'stat_callout' && slide.stat?.value && slide.stat?.label) {
    return 'stat_callout'
  }
  if (kind === 'quote' && slide.quote?.text) return 'quote'
  if (kind === 'two_column' && (slide.columns?.length ?? 0) >= 2) {
    return 'two_column'
  }
  if (kind === 'icon_rows' && (slide.iconRows?.length ?? 0) >= 1) {
    return 'icon_rows'
  }
  return 'standard'
}
