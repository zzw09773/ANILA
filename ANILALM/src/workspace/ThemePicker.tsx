import { useTheme } from '../theme/ThemeContext'
import { THEMES, type ThemeId } from '../studio/themes'

interface Props {
  selected: ThemeId
  onSelect: (id: ThemeId) => void
}

/**
 * Grid of theme cards. Each card shows:
 * - A 4-row color swatch strip (bar/accent/ink/bg)
 * - Name + 1-line description
 * - "Suitable for" tags
 *
 * The spec (A.3) describes this with CSS classes (.theme-grid /
 * .theme-card / var(--card-bg) …). ANILALM has no global stylesheet or
 * CSS modules — every component themes itself with inline styles driven
 * by useTheme() tokens (see CommandModal / WSStudio). To stay consistent
 * with that pattern we map the spec's CSS onto inline styles using the
 * existing design tokens; the swatch SVG markup is kept verbatim.
 */
export function ThemePicker({ selected, onSelect }: Props) {
  const { t } = useTheme()
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
        gap: 10,
      }}
    >
      {THEMES.map((theme) => {
        const isSelected = selected === theme.id
        return (
          <button
            key={theme.id}
            type="button"
            onClick={() => onSelect(theme.id)}
            aria-pressed={isSelected}
            style={{
              background: isSelected ? t.accentSoft : t.surface2,
              border: `2px solid ${isSelected ? t.accentBorder : t.border}`,
              borderRadius: 10,
              padding: 9,
              cursor: 'pointer',
              textAlign: 'left',
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
              fontFamily: 'inherit',
            }}
          >
            <ThemeSwatch theme={theme} />
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: t.text }}>
                {theme.name}
              </div>
              <div
                style={{
                  fontSize: 11,
                  color: t.textMuted,
                  lineHeight: 1.4,
                }}
              >
                {theme.description}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 2 }}>
                {theme.suitableFor.map((tag) => (
                  <span
                    key={tag}
                    style={{
                      fontSize: 10,
                      padding: '2px 6px',
                      background: t.chipBg,
                      color: t.textMuted,
                      borderRadius: 4,
                    }}
                  >
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          </button>
        )
      })}
    </div>
  )
}

function ThemeSwatch({ theme }: { theme: (typeof THEMES)[number] }) {
  // 80×56 SVG mockup: title bar, accent strip, body lines
  const { swatches } = theme
  return (
    <svg
      viewBox="0 0 80 56"
      style={{ width: '100%', aspectRatio: '80 / 56', display: 'block' }}
      aria-hidden
    >
      {/* Background */}
      <rect width="80" height="56" fill={swatches.bg} stroke="#e0e0e0" strokeWidth="0.5" />
      {/* Title bar — variant by theme.iconStyle / chrome */}
      {(theme.id === 'corporate_navy' || theme.id === 'warm_journal' || theme.id === 'startup_pitch') && (
        <rect x="0" y="0" width="80" height="10" fill={swatches.bar} />
      )}
      {theme.id === 'academic_paper' && (
        <line x1="6" y1="11" x2="74" y2="11" stroke="#999" strokeWidth="0.5" />
      )}
      {/* Accent — left strip for corporate/warm, none for academic/exec */}
      {(theme.id === 'corporate_navy' || theme.id === 'warm_journal') && (
        <rect x="0" y="10" width="2" height="46" fill={swatches.accent} />
      )}
      {/* Body lines — sans/serif treatment differs */}
      <rect x={theme.id === 'academic_paper' ? 8 : 6} y="18" width="50" height="2.5" fill={swatches.ink} opacity="0.85" />
      <rect x={theme.id === 'academic_paper' ? 8 : 6} y="24" width="60" height="2" fill={swatches.ink} opacity="0.55" />
      <rect x={theme.id === 'academic_paper' ? 8 : 6} y="29" width="55" height="2" fill={swatches.ink} opacity="0.55" />
      {/* Icon hint — small circle for circle styles, dot for others */}
      {theme.iconStyle === 'outlined-circle' && (
        <circle cx="68" cy="40" r="4" stroke={swatches.accent} strokeWidth="1" fill="none" />
      )}
      {theme.iconStyle === 'soft-filled' && (
        <circle cx="68" cy="40" r="3.5" fill={swatches.accent} />
      )}
      {theme.iconStyle === 'filled-pill' && (
        <circle cx="68" cy="40" r="5" fill={swatches.accent} />
      )}
      {(theme.iconStyle === 'monochrome-dot' || theme.iconStyle === 'minimal-dot') && (
        <circle cx="68" cy="40" r="1.2" fill={swatches.ink} opacity="0.5" />
      )}
    </svg>
  )
}
