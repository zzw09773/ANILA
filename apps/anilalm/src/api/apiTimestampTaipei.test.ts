/**
 * Fixed-epoch: API timestamps with an explicit UTC offset render as Taipei wall clock.
 *
 * Offsetless ISO would be parsed as local time by ECMAScript and show eight
 * hours early in Asia/Taipei. After X.3 the API emits +00:00 / Z.
 */
import { describe, expect, it } from 'vitest'

function formatTaipeiWallClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-TW', {
    timeZone: 'Asia/Taipei',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

describe('API timestamp with UTC offset → Asia/Taipei wall clock', () => {
  const apiUtc = '2026-07-30T08:00:00.000000+00:00'

  it('renders UTC 08:00 as Taipei 16:00', () => {
    const rendered = formatTaipeiWallClock(apiUtc)
    expect(rendered).toMatch(/16:00:00/)
    expect(rendered).toMatch(/2026/)
    expect(rendered).toMatch(/07/)
    expect(rendered).toMatch(/30/)
  })

  it('Z suffix is equivalent to +00:00', () => {
    expect(formatTaipeiWallClock('2026-07-30T08:00:00.000000Z')).toMatch(/16:00:00/)
  })

  it('offsetless string is the bug shape (parsed as local, not UTC)', () => {
    // In a Taipei TZ environment this equals 08:00 local — eight hours early
    // relative to the true instant. We only assert parseability here; the
    // gated contract is that the API no longer emits this shape.
    const d = new Date('2026-07-30T08:00:00')
    expect(Number.isNaN(d.getTime())).toBe(false)
  })
})
