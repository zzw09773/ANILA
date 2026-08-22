import type { SlidePreview } from '../types'
import { slidesFromJobSpec, type JobStatus } from '../api/studio'

/** Visible mark testers (and users) can see that this page was rewritten. */
export const REDONE_SUFFIX = '（重做）'

export function markRedoneTitle(title: string): string {
  const base = title.replace(/（重做）+$/u, '').trim() || title
  return `${base}${REDONE_SUFFIX}`
}

/**
 * Keep the current deck. Replace only `index`.
 *
 * A regenerate response must not become a new 12-page job, and an empty
 * / malformed spec must not blank the preview (that reads as about:blank).
 */
export function replaceSlideInDeck(
  current: SlidePreview[],
  incoming: SlidePreview[] | undefined,
  index: number,
): SlidePreview[] {
  if (!Array.isArray(current) || current.length === 0) {
    return incoming?.length ? incoming : current
  }
  if (index < 0 || index >= current.length) return current
  const next = incoming?.[index] ?? incoming?.[0]
  if (!next) return current
  const patched = { ...next, title: markRedoneTitle(next.title) }
  return current.map((slide, i) => (i === index ? patched : slide))
}

export function slidesAfterRegenerate(
  current: SlidePreview[],
  status: Pick<JobStatus, 'spec'> | null | undefined,
  index: number,
): SlidePreview[] {
  return replaceSlideInDeck(current, slidesFromJobSpec(status?.spec), index)
}
