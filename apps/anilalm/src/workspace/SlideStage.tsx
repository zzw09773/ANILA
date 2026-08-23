import type { CSSProperties } from 'react'
import type { ThemeTokens } from '../theme/tokens'
import type { SlidePreview } from '../types'
import { displayBullet, resolveSlideStage } from './slideStage'

export function SlideStage({
  slide,
  t,
}: {
  slide: SlidePreview
  t: ThemeTokens
}) {
  const kind = resolveSlideStage(slide)
  const stage: CSSProperties = {
    flex: 1,
    minHeight: 0,
    overflow: 'hidden',
    display: 'flex',
    flexDirection: 'column',
  }

  if (kind === 'section_break') {
    return (
      <div
        data-layout={kind}
        style={{
          ...stage,
          justifyContent: 'center',
          alignItems: 'center',
          textAlign: 'center',
          gap: 12,
          background: t.accentSoft,
          margin: -32,
          padding: 40,
        }}
      >
        <h3
          style={{
            margin: 0,
            fontSize: 28,
            fontWeight: 650,
            letterSpacing: -0.4,
            color: t.text,
            lineHeight: 1.25,
          }}
        >
          {slide.title}
        </h3>
        {slide.bullets[0] ? (
          <p style={{ margin: 0, fontSize: 16, color: t.textMuted, lineHeight: 1.5 }}>
            {displayBullet(slide.bullets[0])}
          </p>
        ) : null}
      </div>
    )
  }

  if (kind === 'stat_callout' && slide.stat) {
    const hasBaseline = Boolean(slide.stat.baseline?.trim())
    return (
      <div
        data-layout={kind}
        style={{
          ...stage,
          alignItems: 'center',
          justifyContent: 'center',
          textAlign: 'center',
          gap: 8,
        }}
      >
        {hasBaseline ? (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: '1fr auto 1.4fr',
              alignItems: 'center',
              gap: 12,
              width: '100%',
            }}
          >
            <div>
              <div style={{ fontSize: 32, fontWeight: 650, color: t.textMuted, lineHeight: 1 }}>
                {slide.stat.baseline}
              </div>
              <div style={{ fontSize: 12, color: t.textSubtle, marginTop: 6 }}>
                {slide.stat.baselineLabel || '基準'}
              </div>
            </div>
            <div style={{ fontSize: 28, fontWeight: 700, color: t.accent }}>→</div>
            <div>
              <div style={{ fontSize: 48, fontWeight: 700, letterSpacing: -1, color: t.accent, lineHeight: 1 }}>
                {slide.stat.value}
              </div>
              <div style={{ fontSize: 16, fontWeight: 600, color: t.text, marginTop: 6 }}>
                {slide.stat.label}
              </div>
            </div>
          </div>
        ) : (
          <>
            <div
              style={{
                fontSize: 56,
                fontWeight: 700,
                letterSpacing: -1.2,
                color: t.accent,
                lineHeight: 1,
              }}
            >
              {slide.stat.value}
            </div>
            <div style={{ fontSize: 18, fontWeight: 600, color: t.text }}>
              {slide.stat.label}
            </div>
          </>
        )}
        {slide.stat.supporting ? (
          <p
            style={{
              margin: '6px 0 0',
              fontSize: 13,
              color: t.textMuted,
              fontStyle: 'italic',
              maxWidth: '36em',
              lineHeight: 1.5,
            }}
          >
            {slide.stat.supporting}
          </p>
        ) : null}
      </div>
    )
  }

  if (kind === 'quote' && slide.quote?.text) {
    return (
      <div
        data-layout={kind}
        style={{
          ...stage,
          justifyContent: 'center',
          gap: 16,
          paddingTop: 8,
        }}
      >
        <blockquote
          style={{
            margin: 0,
            fontSize: 22,
            fontStyle: 'italic',
            color: t.text,
            lineHeight: 1.45,
          }}
        >
          「{slide.quote.text}」
        </blockquote>
        {slide.quote.attribution ? (
          <cite style={{ fontStyle: 'normal', fontSize: 13, color: t.textMuted, alignSelf: 'flex-end' }}>
            — {slide.quote.attribution}
          </cite>
        ) : null}
      </div>
    )
  }

  if (kind === 'two_column' && slide.columns && slide.columns.length >= 2) {
    const cols = slide.columns.slice(0, 2)
    return (
      <div data-layout={kind} style={{ ...stage, gap: 14 }}>
        <h3 style={titleStyle(t)}>{slide.title}</h3>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: 20,
            minHeight: 0,
            flex: 1,
          }}
        >
          {cols.map((col, i) => (
            <div key={i} style={{ minWidth: 0 }}>
              <div
                style={{
                  fontSize: 14,
                  fontWeight: 650,
                  color: t.accent,
                  marginBottom: 8,
                  paddingBottom: 6,
                  borderBottom: `2px solid ${t.accent}`,
                }}
              >
                {col.heading}
              </div>
              <ul style={listStyle()}>
                {(col.bullets ?? []).map((b, j) => (
                  <li key={j} style={bulletStyle(t)}>
                    {displayBullet(b)}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (kind === 'icon_rows' && slide.iconRows?.length) {
    return (
      <div data-layout={kind} style={{ ...stage, gap: 12 }}>
        <h3 style={titleStyle(t)}>{slide.title}</h3>
        <ul style={{ ...listStyle(), listStyle: 'none', paddingLeft: 0, gap: 10 }}>
          {slide.iconRows.map((row, i) => (
            <li key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <strong style={{ fontSize: 15, color: t.text, fontWeight: 650 }}>
                {row.heading}
              </strong>
              {row.description ? (
                <span style={{ fontSize: 13, color: t.textMuted, lineHeight: 1.5 }}>
                  {row.description}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      </div>
    )
  }

  return (
    <div data-layout="standard" style={{ ...stage, gap: 14 }}>
      <h3 style={titleStyle(t)}>{slide.title}</h3>
      <ul style={listStyle()}>
        {slide.bullets.map((b, i) => (
          <li key={i} style={bulletStyle(t)}>
            {displayBullet(b)}
          </li>
        ))}
      </ul>
    </div>
  )
}

function titleStyle(t: ThemeTokens): CSSProperties {
  return {
    margin: 0,
    fontSize: 22,
    fontWeight: 600,
    letterSpacing: -0.3,
    color: t.text,
    lineHeight: 1.3,
  }
}

function listStyle(): CSSProperties {
  return {
    paddingLeft: 22,
    margin: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    minHeight: 0,
  }
}

function bulletStyle(t: ThemeTokens): CSSProperties {
  return { fontSize: 15, lineHeight: 1.55, color: t.text }
}
