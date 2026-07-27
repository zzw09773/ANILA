/**
 * Fixed-epoch: API timestamps with an explicit UTC offset render as Taipei wall clock.
 *
 * Mirrors the CSP serialization contract — offsetless ISO would be parsed as
 * local time by ECMAScript and show eight hours early in Asia/Taipei.
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
  const apiUtc = '2026-07-27T11:19:42.679819+00:00'

  it('renders the measured collection created_at as Taipei 19:19:42', () => {
    const rendered = formatTaipeiWallClock(apiUtc)
    // zh-TW locale may use "/" or "-" separators; assert the time-of-day.
    expect(rendered).toMatch(/19:19:42/)
    expect(rendered).toMatch(/2026/)
    expect(rendered).toMatch(/07/)
    expect(rendered).toMatch(/27/)
  })

  it('Z suffix is equivalent to +00:00', () => {
    expect(formatTaipeiWallClock('2026-07-27T11:19:42.679819Z')).toMatch(
      /19:19:42/,
    )
  })
})
