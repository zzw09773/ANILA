/**
 * pptx-renderer — HTTP wrapper around pptxgenjs + LibreOffice headless.
 *
 * Endpoints:
 *   POST /render         { spec }      → .pptx binary (octet-stream)
 *   POST /screenshots    { pptxBase64 } → { images: [{ index, mime, base64 }] }
 *   GET  /health         → "ok"
 *
 * Why a separate Node service instead of subprocess.run() from CSP:
 *   - CSP container stays Python-only; this container owns Node + LibreOffice.
 *   - The vendored node_modules (~129 MB react-icons + sharp + pptxgenjs)
 *     live with this image — CSP image stays tiny.
 *   - Reusable: any future caller (n8n workflow node, CLI, slack bot) hits
 *     the same /render endpoint.
 *
 * Inputs are deliberately schema-light. Backend (CSP) does Pydantic
 * validation BEFORE calling here — by the time a spec lands on this
 * server it's already structurally valid; we just have to render it.
 *
 * Inputs that ARE checked: payload size, slide count cap, file paths
 * (no traversal in /screenshots).
 */

const express = require('express')
const PptxGenJS = require('pptxgenjs')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const { renderIconPng } = require('./icons.js')

const PORT = Number(process.env.PORT || 7100)
const TMP_ROOT = process.env.PPTX_TMP_DIR || '/tmp/pptx-out'
const MAX_PAYLOAD = '10mb'
const MAX_SLIDES = 60

// ── Phase 3: palette + layout dispatch ────────────────────────────────
//
// PALETTES is the single source of truth on what each named palette
// resolves to. The schema accepts a string and normalises unknown names
// to "navy_amber"; the renderer accepts whatever lands in spec.palette
// and falls back to "navy_amber" if it doesn't recognise it.
//
// Each palette declares ROLE-keyed colour slots so layout renderers can
// say "give me palette.bar" instead of hardcoding hex. Adding a new
// palette only touches this object plus the prompt's whitelist.
const PALETTES = {
  // Default — direct successor of Phase 2 visual.
  navy_amber: {
    bar: '1E2761',
    accent: 'F4B740',
    titleText: '1E2761', // cover-slide title and section headings
    barText: 'FFFFFF',
    ink: '1A1A1A',
    muted: '5C6470',
    bg: 'FFFFFF',
  },
  forest_moss: {
    bar: '2C5F2D',
    accent: '97BC62',
    titleText: '2C5F2D',
    barText: 'FFFFFF',
    ink: '1A1A1A',
    muted: '5C6470',
    bg: 'FFFFFF',
  },
  charcoal_minimal: {
    bar: '36454F',
    accent: 'F4B740',
    titleText: '36454F',
    barText: 'FFFFFF',
    ink: '212121',
    muted: '70757A',
    bg: 'FFFFFF',
  },
  // Coral as accent (full-bleed coral bar reads as too loud); the
  // bar uses a deep navy from the SKILL.md "Coral Energy" tertiary
  // colour and the accent strip+highlights use the actual coral.
  coral_energy: {
    bar: '2F3C7E',
    accent: 'F96167',
    titleText: '2F3C7E',
    barText: 'FFFFFF',
    ink: '1A1A1A',
    muted: '5C6470',
    bg: 'FFFFFF',
  },
}

// CJK font that ships in the renderer image (`fonts-noto-cjk` apt
// package). Forced explicitly because LibreOffice's Calibri fallback
// can resolve to a Simplified-Chinese variant for 繁體 strings.
const FONT_FACE = 'Noto Sans CJK TC'

fs.mkdirSync(TMP_ROOT, { recursive: true })

const app = express()
app.use(express.json({ limit: MAX_PAYLOAD }))

app.get('/health', (_req, res) => res.type('text/plain').send('ok'))

/**
 * Render a SlidesSpec into a .pptx file.
 *
 * The returned binary is the standard application/vnd.openxmlformats-...
 * MIME so a downstream HTTP client (CSP) can stream it straight back to
 * the browser without re-encoding.
 *
 * Job IDs let /screenshots later refer to the same temp file without us
 * round-tripping the .pptx bytes through CSP's memory.
 */
// ── Layout renderers ─────────────────────────────────────────────────
//
// Every renderer takes (pres, slide, palette) and returns nothing —
// they mutate `pres` by adding a slide. Calling order:
//   dispatcher (renderSlideByKind) →  per-kind renderer
// Per-kind renderers fall back to renderStandard when their layout-
// specific payload is missing or malformed, which keeps the pipeline
// resilient to LLM hallucinations on the optional fields.

/**
 * Standard layout — title bar + dynamic-sized bullets. Phase-2 logic.
 * Bullet font size scales with bullet count so a 3-bullet slide isn't
 * tiny floating text and a 7-bullet slide doesn't overflow.
 */
function renderStandard(pres, s, p) {
  const slide = pres.addSlide({ masterName: 'ANILA_BASE' })
  slide.addText(String(s.title || 'Untitled'), {
    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
    fontSize: 26, bold: true, color: p.barText,
    align: 'left', valign: 'middle',
    fontFace: FONT_FACE, margin: 0,
  })
  const bullets = Array.isArray(s.bullets) ? s.bullets : []
  const n = bullets.length
  let bodyFontSize, bodyValign, bodyY, bodyH, paraSpaceAfter
  if (n <= 3) {
    bodyFontSize = 28; bodyValign = 'middle'; bodyY = 1.2; bodyH = 5.7; paraSpaceAfter = 22
  } else if (n <= 5) {
    bodyFontSize = 24; bodyValign = 'top'; bodyY = 1.2; bodyH = 5.7; paraSpaceAfter = 16
  } else {
    bodyFontSize = 20; bodyValign = 'top'; bodyY = 1.1; bodyH = 5.8; paraSpaceAfter = 10
  }
  slide.addText(
    bullets.map((b) => ({
      text: String(b),
      options: {
        // pptxgenjs treats `{type, code}` as mutually exclusive — passing
        // both results in neither being rendered (silent fail). `code`
        // alone with the unicode for BLACK CIRCLE gives us a clean dot
        // bullet that LibreOffice and PowerPoint both honour.
        bullet: { code: '25CF' },
        color: p.ink,
      },
    })),
    {
      x: 0.9, y: bodyY, w: 11.5, h: bodyH,
      fontSize: bodyFontSize, color: p.ink, fontFace: FONT_FACE,
      paraSpaceAfter, valign: bodyValign, indentLevel: 0,
    },
  )
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Section break — full-bleed coloured background with centred large
 * title. Skips the master so there's no header bar (the entire slide
 * BECOMES the bar). Uses bullets[0] as a subtitle if provided.
 */
function renderSectionBreak(pres, s, p) {
  // No master — full-bleed colour fill.
  const slide = pres.addSlide()
  slide.background = { color: p.bar }
  // Decorative amber strip on the left edge — single visual motif
  // shared with the cover slide.
  slide.addShape('rect', {
    x: 0.6, y: 1.6, w: 0.14, h: 4.2,
    fill: { color: p.accent },
    line: { type: 'none' },
  })
  slide.addText(String(s.title || ''), {
    x: 1.1, y: 2.4, w: 11.5, h: 1.8,
    fontSize: 56, bold: true, color: 'FFFFFF',
    align: 'left', valign: 'middle', fontFace: FONT_FACE,
  })
  // Subtitle from bullets[0] if the LLM provided one — keeps the slide
  // useful even when it's clearly just a transition.
  const bullets = Array.isArray(s.bullets) ? s.bullets : []
  if (bullets[0]) {
    slide.addText(String(bullets[0]), {
      x: 1.1, y: 4.4, w: 11.5, h: 0.6,
      fontSize: 20, color: p.accent,
      align: 'left', italic: true, fontFace: FONT_FACE,
    })
  }
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Stat callout — title bar + huge number + label below.
 * Uses palette.accent for the number to make it pop. Falls back to
 * standard if `slide.stat.value` is missing.
 */
function renderStatCallout(pres, s, p) {
  if (!s.stat || !s.stat.value || !s.stat.label) {
    return renderStandard(pres, s, p)
  }
  const slide = pres.addSlide({ masterName: 'ANILA_BASE' })
  slide.addText(String(s.title || ''), {
    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
    fontSize: 26, bold: true, color: p.barText,
    align: 'left', valign: 'middle',
    fontFace: FONT_FACE, margin: 0,
  })
  // Big number — accent colour, 96pt, bold, centred. The "headline number"
  // is the entire visual focus of this layout, hence the deliberate size.
  slide.addText(String(s.stat.value), {
    x: 0.5, y: 1.6, w: 12.3, h: 2.6,
    fontSize: 96, bold: true, color: p.accent,
    align: 'center', valign: 'middle', fontFace: FONT_FACE,
  })
  // Label — what the number means. 28pt is large enough to read from
  // the back of a room without overshadowing the value above.
  slide.addText(String(s.stat.label), {
    x: 1.0, y: 4.5, w: 11.3, h: 0.8,
    fontSize: 28, color: p.ink,
    align: 'center', valign: 'middle', fontFace: FONT_FACE,
  })
  if (s.stat.supporting) {
    slide.addText(String(s.stat.supporting), {
      x: 1.0, y: 5.5, w: 11.3, h: 0.6,
      fontSize: 16, color: p.muted, italic: true,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
  }
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Pull-quote — large italic body with optional attribution. Falls back
 * to standard if `slide.quote.text` is missing.
 */
function renderQuote(pres, s, p) {
  if (!s.quote || !s.quote.text) {
    return renderStandard(pres, s, p)
  }
  const slide = pres.addSlide({ masterName: 'ANILA_BASE' })
  slide.addText(String(s.title || ''), {
    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
    fontSize: 26, bold: true, color: p.barText,
    align: 'left', valign: 'middle',
    fontFace: FONT_FACE, margin: 0,
  })
  // Oversized opening quote mark — accent colour, anchored top-left of
  // the body. The corresponding closing mark is omitted by design;
  // single-mark openers read more cleanly than balanced quotes when
  // the quote already fills the slide.
  slide.addText('“', {
    x: 0.6, y: 1.0, w: 1.2, h: 1.6,
    fontSize: 110, bold: true, color: p.accent,
    align: 'left', valign: 'top', fontFace: FONT_FACE,
  })
  slide.addText(String(s.quote.text), {
    x: 1.6, y: 1.6, w: 11.0, h: 4.2,
    fontSize: 30, italic: true, color: p.ink,
    align: 'left', valign: 'top', fontFace: FONT_FACE,
  })
  if (s.quote.attribution) {
    slide.addText(`— ${String(s.quote.attribution)}`, {
      x: 1.6, y: 6.0, w: 11.0, h: 0.5,
      fontSize: 16, color: p.muted,
      align: 'right', valign: 'top', fontFace: FONT_FACE,
    })
  }
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Two columns — heading + bullets per side. Strict 2-column layout:
 * if the LLM gave us 1 column we render the bullets across both sides
 * as standard; if it gave us 3 we use the first 2.
 * Falls back to standard if `columns` is empty or malformed.
 */
function renderTwoColumn(pres, s, p) {
  if (!Array.isArray(s.columns) || s.columns.length < 2) {
    return renderStandard(pres, s, p)
  }
  const cols = s.columns.slice(0, 2)
  const slide = pres.addSlide({ masterName: 'ANILA_BASE' })
  slide.addText(String(s.title || ''), {
    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
    fontSize: 26, bold: true, color: p.barText,
    align: 'left', valign: 'middle',
    fontFace: FONT_FACE, margin: 0,
  })
  // Layout maths: slide width 13.33in; we use 0.5 left margin, 0.5
  // right margin, 0.4 between columns. Each column gets
  // (13.33 - 0.5*2 - 0.4) / 2 = 5.965in ≈ 5.95in.
  const COL_WIDTH = 5.95
  const leftX = 0.5
  const rightX = leftX + COL_WIDTH + 0.4

  cols.forEach((col, i) => {
    const x = i === 0 ? leftX : rightX
    // Column heading — accent colour bold, sits above bullets.
    slide.addText(String(col.heading || ''), {
      x, y: 1.1, w: COL_WIDTH, h: 0.6,
      fontSize: 22, bold: true, color: p.accent,
      align: 'left', valign: 'middle', fontFace: FONT_FACE,
    })
    // Underline between heading and bullets — uses palette.accent so
    // both columns visually share the same divider style.
    slide.addShape('rect', {
      x, y: 1.7, w: COL_WIDTH, h: 0.03,
      fill: { color: p.accent }, line: { type: 'none' },
    })
    const bullets = Array.isArray(col.bullets) ? col.bullets : []
    slide.addText(
      bullets.map((b) => ({
        text: String(b),
        options: { bullet: { code: '25CF' }, color: p.ink },
      })),
      {
        x: x + 0.1, y: 2.0, w: COL_WIDTH - 0.1, h: 4.8,
        fontSize: 18, color: p.ink, fontFace: FONT_FACE,
        paraSpaceAfter: 12, valign: 'top',
      },
    )
  })
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Icon rows — 3-5 rows of [icon | heading | description].
 * Async because each icon is rendered SVG → PNG via sharp.
 *
 * If the icon for a given concept can't be resolved (concept not in
 * CONCEPT_MAP), the row still renders without the icon — heading and
 * description take the full row width. We don't drop the row, since
 * silently dropping LLM content is worse than a row missing a glyph.
 */
async function renderIconRows(pres, s, p) {
  if (!Array.isArray(s.icon_rows) || s.icon_rows.length === 0) {
    return renderStandard(pres, s, p)
  }
  const rows = s.icon_rows.slice(0, 5)
  const slide = pres.addSlide({ masterName: 'ANILA_BASE' })
  slide.addText(String(s.title || ''), {
    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
    fontSize: 26, bold: true, color: p.barText,
    align: 'left', valign: 'middle',
    fontFace: FONT_FACE, margin: 0,
  })

  // Row layout: distribute available vertical space (1.05 → 6.9 = 5.85in)
  // among the rows with a small gap between rows. Icon size scales
  // inverse to row count so 3 rows feel deliberate and 5 rows still fit.
  const TOP = 1.1
  const BOTTOM = 6.9
  const GAP = 0.2
  const rowH = (BOTTOM - TOP - GAP * (rows.length - 1)) / rows.length
  // Icon goes in a square box at the row's left; description text uses
  // the rest of the row width.
  const iconBoxSize = Math.min(rowH - 0.1, 1.0)
  const ICON_X = 0.6
  const TEXT_X = ICON_X + iconBoxSize + 0.3

  // Render every icon's PNG concurrently — they're independent and
  // sharp + the SVG path are CPU-light, so ~5 parallel awaits cost
  // ~the same wall-clock as one. Promise.all preserves array order.
  const iconPngs = await Promise.all(
    rows.map((r) => renderIconPng(r.concept, { color: `#${p.accent}`, size: 256 })),
  )

  rows.forEach((r, i) => {
    const y = TOP + i * (rowH + GAP)

    // White-fill circle with an accent ring. Originally tried a tinted
    // fill via `${p.accent}22` (8-char hex with alpha), but pptxgenjs's
    // shape `fill.color` doesn't honour an alpha channel — the value
    // gets silently dropped or mis-parsed, which left rows without an
    // icon image showing a black ellipse interior. White fill + 2pt
    // accent ring is robust across pptxgenjs / LibreOffice / PowerPoint
    // versions, and gives the same "icon in a coloured circle" motif
    // SKILL.md recommends for contrast.
    slide.addShape('ellipse', {
      x: ICON_X, y: y + (rowH - iconBoxSize) / 2,
      w: iconBoxSize, h: iconBoxSize,
      fill: { color: 'FFFFFF' },
      line: { color: p.accent, width: 2 },
    })
    if (iconPngs[i]) {
      // Inset 12% of the box so the glyph doesn't kiss the circle edge.
      const inset = iconBoxSize * 0.18
      slide.addImage({
        data: `data:image/png;base64,${iconPngs[i].toString('base64')}`,
        x: ICON_X + inset,
        y: y + (rowH - iconBoxSize) / 2 + inset,
        w: iconBoxSize - inset * 2,
        h: iconBoxSize - inset * 2,
      })
    }
    // Heading — top half of the row text area.
    slide.addText(String(r.heading || ''), {
      x: TEXT_X, y, w: 12.0 - TEXT_X, h: rowH * 0.45,
      fontSize: 18, bold: true, color: p.ink,
      align: 'left', valign: 'bottom', fontFace: FONT_FACE,
    })
    // Description — bottom half.
    slide.addText(String(r.description || ''), {
      x: TEXT_X, y: y + rowH * 0.45, w: 12.0 - TEXT_X, h: rowH * 0.55,
      fontSize: 14, color: p.muted,
      align: 'left', valign: 'top', fontFace: FONT_FACE,
    })
  })
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Image-focus layout (Phase 5) — embeds a real image extracted from the
 * source document on the left half, with bullets on the right.
 *
 * The CSP backend hydrates `slide.image_data` (a `data:image/...;base64,...`
 * URL) from the LLM's `image_ref` before sending the spec here. If
 * `image_data` is missing we fall back to standard layout so a stale or
 * malformed ref doesn't crash the deck.
 *
 * Aspect-ratio handling: pptxgenjs's `addImage({ sizing })` would scale
 * to fit but we want letterboxing (preserve aspect, centre in box). We
 * pass the box bounds and rely on pptxgenjs's `sizing.type='contain'`
 * for that behaviour.
 */
function renderImageFocus(pres, s, p) {
  if (!s.image_data || typeof s.image_data !== 'string') {
    return renderStandard(pres, s, p)
  }
  const slide = pres.addSlide({ masterName: 'ANILA_BASE' })

  // Title bar — same as other layouts.
  slide.addText(String(s.title || ''), {
    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
    fontSize: 26, bold: true, color: p.barText,
    align: 'left', valign: 'middle',
    fontFace: FONT_FACE, margin: 0,
  })

  // Image box — left 50%. Slide width 13.33"; minus 0.5" left + 0.4"
  // gap → image gets 5.95" wide (mirrors two_column geometry so
  // visual rhythm stays consistent across the deck).
  const IMG_X = 0.5
  const IMG_Y = 1.1
  const IMG_W = 5.95
  const IMG_H = 5.7
  slide.addImage({
    data: s.image_data,
    x: IMG_X, y: IMG_Y, w: IMG_W, h: IMG_H,
    // 'contain' keeps the source's aspect ratio inside the box,
    // letterboxing rather than stretching — important for a chart
    // whose axis labels would distort under a forced-stretch.
    sizing: { type: 'contain', w: IMG_W, h: IMG_H },
  })

  // Bullets — right 50%.
  const TEXT_X = IMG_X + IMG_W + 0.4
  const TEXT_W = 13.33 - TEXT_X - 0.5
  const bullets = Array.isArray(s.bullets) ? s.bullets : []
  if (bullets.length > 0) {
    // Same dynamic font sizing as standard — fewer bullets → bigger.
    const n = bullets.length
    const fontSize = n <= 3 ? 22 : (n <= 5 ? 20 : 18)
    const paraSpaceAfter = n <= 3 ? 18 : (n <= 5 ? 14 : 10)
    slide.addText(
      bullets.map((b) => ({
        text: String(b),
        options: { bullet: { code: '25CF' }, color: p.ink },
      })),
      {
        x: TEXT_X, y: IMG_Y, w: TEXT_W, h: IMG_H,
        fontSize, color: p.ink, fontFace: FONT_FACE,
        paraSpaceAfter, valign: 'top', indentLevel: 0,
      },
    )
  }
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Closing statement (Phase 6) — final-slide aesthetic. Full-bleed accent
 * background with centred italic title (the question / call-to-action)
 * and an optional small tagline from bullets[0]. Distinct from
 * section_break which uses palette.bar (navy) — closing uses palette.
 * accent (amber/coral) so it visually closes the deck rather than
 * starting a new section.
 *
 * Skips ANILA_BASE master entirely so the standard navy header bar
 * doesn't appear above the closing statement (it would read as "still
 * in the deck" rather than "we're done").
 */
function renderClosingStatement(pres, s, p) {
  const slide = pres.addSlide()
  // Solid accent background — same trick section_break uses with the bar
  // colour. We pick `accent` here (the amber / coral / sage) because it's
  // the brightest tone in each palette and visually says "the end".
  slide.background = { color: p.accent }

  // Subtle vignette at the bottom via a second rect at low opacity. Skip
  // it — pptxgenjs's fill alpha is unreliable across PowerPoint /
  // LibreOffice (same bug we hit in icon_rows). Solid colour + good
  // typography is enough.

  // Title — large, italic, centred. Lower vertical centre than
  // section_break so the slide reads as "wrap-up" rather than "intro".
  slide.addText(String(s.title || ''), {
    x: 0.5, y: 2.4, w: 12.3, h: 2.2,
    fontSize: 52, bold: true, italic: true, color: 'FFFFFF',
    align: 'center', valign: 'middle', fontFace: FONT_FACE,
  })

  // Optional tagline from bullets[0]. We use the SAME field the LLM
  // already populates for section_break sub-titles, so the prompt
  // instruction "bullets[0] = 副標" is consistent across both layouts.
  const bullets = Array.isArray(s.bullets) ? s.bullets : []
  if (bullets[0]) {
    slide.addText(String(bullets[0]), {
      x: 1.0, y: 4.6, w: 11.3, h: 0.7,
      fontSize: 22, color: 'FFFFFF', italic: false,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
  }
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Before/After (Phase 6) — comparison framing. Reuses `slide.columns`
 * (must be exactly 2). Adds two visual elements two_column doesn't have:
 *   1. Pill-shaped labels above each column ("Before" / "After" by
 *      default; LLM can put alternative labels in column.heading).
 *   2. A right-pointing arrow shape between the two columns to make the
 *      directionality (left → right) explicit.
 * Falls back to renderTwoColumn if columns are missing or only 1 — at
 * that point the "before/after" framing has no meaning anyway.
 */
function renderBeforeAfter(pres, s, p) {
  if (!Array.isArray(s.columns) || s.columns.length < 2) {
    return renderTwoColumn(pres, s, p)
  }
  const cols = s.columns.slice(0, 2)
  const slide = pres.addSlide({ masterName: 'ANILA_BASE' })
  slide.addText(String(s.title || ''), {
    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
    fontSize: 26, bold: true, color: p.barText,
    align: 'left', valign: 'middle',
    fontFace: FONT_FACE, margin: 0,
  })

  // Same column maths as two_column for visual rhythm consistency.
  // Slightly narrower columns (5.6 vs 5.95) so the centre arrow has
  // breathing room.
  const COL_WIDTH = 5.6
  const leftX = 0.4
  const rightX = leftX + COL_WIDTH + 1.1
  // Arrow occupies the gap between columns.
  const ARROW_X = leftX + COL_WIDTH + 0.05
  const ARROW_W = 1.0

  cols.forEach((col, i) => {
    const x = i === 0 ? leftX : rightX
    // Default heading text — LLM can override via column.heading. We use
    // the literal column heading if non-empty; otherwise fall back to
    // "Before" / "After".
    const fallback = i === 0 ? 'Before' : 'After'
    const headingText = String(col.heading || fallback)

    // Pill background for the heading. palette.bar for "Before" (subdued),
    // palette.accent for "After" (highlighted) — visually communicates
    // direction-of-improvement without an explicit colour key.
    const pillFill = i === 0 ? p.muted : p.accent
    slide.addShape('roundRect', {
      x, y: 1.05, w: COL_WIDTH, h: 0.55,
      fill: { color: pillFill },
      line: { type: 'none' },
      rectRadius: 0.27, // half the height = full pill
    })
    slide.addText(headingText, {
      x, y: 1.05, w: COL_WIDTH, h: 0.55,
      fontSize: 18, bold: true, color: 'FFFFFF',
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })

    const bullets = Array.isArray(col.bullets) ? col.bullets : []
    slide.addText(
      bullets.map((b) => ({
        text: String(b),
        options: { bullet: { code: '25CF' }, color: p.ink },
      })),
      {
        x: x + 0.1, y: 1.85, w: COL_WIDTH - 0.1, h: 4.9,
        fontSize: 18, color: p.ink, fontFace: FONT_FACE,
        paraSpaceAfter: 12, valign: 'top',
      },
    )
  })

  // Centre arrow — chevron-right shape from the AutoShape catalogue
  // pptxgenjs ships. `rightArrow` reads as "left becomes right" without
  // needing extra labels.
  slide.addShape('rightArrow', {
    x: ARROW_X, y: 3.2, w: ARROW_W, h: 1.2,
    fill: { color: p.accent },
    line: { color: p.accent, width: 0 },
  })
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Image grid (Phase 6) — 2-4 source figures arranged in a grid below
 * the title bar. Reads `slide.image_data_list` (a parallel array of
 * data URLs) populated by CSP's `_hydrate_image_refs`. Captions, when
 * present, render as small italic text under each cell.
 *
 * Layout strategy:
 *   2 cells → side-by-side, each ~6 in wide
 *   3 cells → 1 large left + 2 stacked right (asymmetric Hero look)
 *   4 cells → 2x2 grid
 * We pick layouts that *use* the available space rather than always
 * forcing a 2x2 even for 2 cells (which leaves dead bottom half).
 *
 * Falls back to renderStandard if image_data_list is missing or has
 * fewer than 2 cells (CSP-side hydration already guarantees ≥2 when
 * the field exists, so this is just defence-in-depth).
 */
function renderImageGrid(pres, s, p) {
  const cells = Array.isArray(s.image_data_list) ? s.image_data_list : []
  if (cells.length < 2) {
    return renderStandard(pres, s, p)
  }
  const captions = (s.image_grid && Array.isArray(s.image_grid.captions))
    ? s.image_grid.captions
    : []
  const n = Math.min(cells.length, 4)

  const slide = pres.addSlide({ masterName: 'ANILA_BASE' })
  slide.addText(String(s.title || ''), {
    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
    fontSize: 26, bold: true, color: p.barText,
    align: 'left', valign: 'middle',
    fontFace: FONT_FACE, margin: 0,
  })

  // Vertical area available for the grid: 1.0 (top) → 6.9 (bottom).
  // Captions eat 0.35in below each row; account for that in cell height.
  const GRID_TOP = 1.05
  const GRID_BOTTOM = 6.85
  const CAP_H = 0.35
  const GAP = 0.2

  // Geometry per layout.
  const placements = []
  if (n === 2) {
    // 1 row × 2 cols, full vertical bleed below the bar.
    const cellW = (13.33 - 0.5 * 2 - GAP) / 2
    const cellH = GRID_BOTTOM - GRID_TOP - CAP_H
    placements.push({ x: 0.5,                   y: GRID_TOP, w: cellW, h: cellH, capY: GRID_TOP + cellH })
    placements.push({ x: 0.5 + cellW + GAP,     y: GRID_TOP, w: cellW, h: cellH, capY: GRID_TOP + cellH })
  } else if (n === 3) {
    // 1 large left (full height), 2 stacked right (half height each).
    const leftW = 7.5
    const rightW = 13.33 - 0.5 * 2 - leftW - GAP
    const fullH = GRID_BOTTOM - GRID_TOP - CAP_H
    const halfH = (GRID_BOTTOM - GRID_TOP - GAP - CAP_H * 2) / 2
    placements.push({ x: 0.5,                       y: GRID_TOP, w: leftW,  h: fullH, capY: GRID_TOP + fullH })
    placements.push({ x: 0.5 + leftW + GAP,         y: GRID_TOP, w: rightW, h: halfH, capY: GRID_TOP + halfH })
    placements.push({
      x: 0.5 + leftW + GAP,
      y: GRID_TOP + halfH + CAP_H + GAP,
      w: rightW, h: halfH,
      capY: GRID_TOP + halfH + CAP_H + GAP + halfH,
    })
  } else {
    // 2x2 grid, equal cells.
    const cellW = (13.33 - 0.5 * 2 - GAP) / 2
    const cellH = (GRID_BOTTOM - GRID_TOP - GAP - CAP_H * 2) / 2
    for (let row = 0; row < 2; row++) {
      for (let col = 0; col < 2; col++) {
        const x = 0.5 + col * (cellW + GAP)
        const y = GRID_TOP + row * (cellH + CAP_H + GAP)
        placements.push({ x, y, w: cellW, h: cellH, capY: y + cellH })
      }
    }
  }

  for (let i = 0; i < n; i++) {
    const place = placements[i]
    slide.addImage({
      data: cells[i],
      x: place.x, y: place.y, w: place.w, h: place.h,
      // contain — same letterboxing strategy as image_focus, important
      // because source figures rarely match our box aspect ratio.
      sizing: { type: 'contain', w: place.w, h: place.h },
    })
    const cap = captions[i] && String(captions[i]).trim()
    if (cap) {
      slide.addText(cap, {
        x: place.x, y: place.capY, w: place.w, h: CAP_H,
        fontSize: 11, italic: true, color: p.muted,
        align: 'center', valign: 'top', fontFace: FONT_FACE,
      })
    }
  }
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Dispatcher — picks the renderer based on slide.layout_kind. Unknown
 * kinds fall back to `renderStandard`. Async so callers can `await` it
 * uniformly even though only icon_rows is actually async.
 */
async function renderSlideByKind(pres, s, p) {
  const kind = String(s.layout_kind || 'standard')
  switch (kind) {
    case 'section_break':     return renderSectionBreak(pres, s, p)
    case 'stat_callout':      return renderStatCallout(pres, s, p)
    case 'quote':             return renderQuote(pres, s, p)
    case 'two_column':        return renderTwoColumn(pres, s, p)
    case 'icon_rows':         return await renderIconRows(pres, s, p)
    case 'image_focus':       return renderImageFocus(pres, s, p)
    case 'closing_statement': return renderClosingStatement(pres, s, p)
    case 'before_after':      return renderBeforeAfter(pres, s, p)
    case 'image_grid':        return renderImageGrid(pres, s, p)
    default:                  return renderStandard(pres, s, p)
  }
}

app.post('/render', async (req, res) => {
  try {
    const spec = req.body?.spec
    if (!spec || typeof spec !== 'object') {
      return res.status(400).json({ error: 'missing spec' })
    }
    if (!Array.isArray(spec.slides) || spec.slides.length === 0) {
      return res.status(400).json({ error: 'spec.slides must be non-empty array' })
    }
    if (spec.slides.length > MAX_SLIDES) {
      return res.status(413).json({
        error: `spec.slides exceeds cap of ${MAX_SLIDES}`,
      })
    }

    // Resolve palette — schema already normalises but defence in depth.
    const paletteName = PALETTES[spec.palette] ? spec.palette : 'navy_amber'
    const p = PALETTES[paletteName]

    const pres = new PptxGenJS()
    pres.layout = 'LAYOUT_WIDE' // 13.33 × 7.5 inch (16:9)
    pres.author = 'ANILA LM'
    pres.title = String(spec.title || 'Untitled')

    // Master shared by all non-section_break layouts. Section breaks
    // skip the master and draw their own full-bleed background, so the
    // bar isn't inherited there.
    pres.defineSlideMaster({
      title: 'ANILA_BASE',
      background: { color: p.bg },
      objects: [
        // Top header bar (height 0.8) — large enough for a 26-pt title
        // to sit centred vertically without crowding descenders.
        {
          rect: {
            x: 0, y: 0, w: '100%', h: 0.8,
            fill: { color: p.bar },
          },
        },
        // Thin accent strip immediately below the bar — single visual
        // motif we repeat across the deck (also appears as the column
        // dividers in two_column and the icon backplate ring).
        {
          rect: {
            x: 0, y: 0.8, w: '100%', h: 0.04,
            fill: { color: p.accent },
          },
        },
      ],
      slideNumber: { x: 12.5, y: 7.1, fontSize: 10, color: p.muted, fontFace: FONT_FACE },
    })

    // ── Cover slide (always first, distinct from section_break) ──
    //
    // We always prepend a cover. If spec.slides[0].layout_kind ===
    // 'section_break' the LLM is signalling "I want my first slide to
    // be the cover" — in that case we let renderSectionBreak handle it
    // and skip the auto-generated cover, otherwise we'd have two
    // section-break-looking slides back to back.
    const firstIsCover = spec.slides[0]?.layout_kind === 'section_break'
    if (!firstIsCover) {
      const titleSlide = pres.addSlide({ masterName: 'ANILA_BASE' })
      titleSlide.addShape('rect', {
        x: 0.6, y: 2.0, w: 0.14, h: 3.5,
        fill: { color: p.accent },
        line: { type: 'none' },
      })
      titleSlide.addText(String(spec.title), {
        x: 1.0, y: 2.1, w: 11.7, h: 1.8,
        fontSize: 50, bold: true, color: p.titleText,
        align: 'left', valign: 'middle', fontFace: FONT_FACE,
      })
      if (spec.slides.length > 1) {
        titleSlide.addText(`共 ${spec.slides.length} 張投影片`, {
          x: 1.0, y: 4.0, w: 11.7, h: 0.5,
          fontSize: 16, color: p.muted,
          align: 'left', fontFace: FONT_FACE,
        })
      }
      titleSlide.addText('ANILA LM · 自動生成', {
        x: 1.0, y: 4.7, w: 11.7, h: 0.4,
        fontSize: 12, color: p.muted, italic: true,
        align: 'left', fontFace: FONT_FACE,
      })
    }

    // Body slides via the layout dispatcher. Sequential await keeps
    // pptxgenjs's internal slide ordering deterministic (Promise.all
    // would race on shared internal state).
    for (const s of spec.slides) {
      await renderSlideByKind(pres, s, p)
    }

    const jobId = `${Date.now().toString(36)}-${Math.random()
      .toString(36)
      .slice(2, 8)}`
    const outPath = path.join(TMP_ROOT, `${jobId}.pptx`)
    await pres.writeFile({ fileName: outPath })

    const buf = fs.readFileSync(outPath)
    res
      .status(200)
      .set({
        'Content-Type':
          'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'X-Pptx-Job-Id': jobId,
        'X-Pptx-Path': outPath,
      })
      .send(buf)
  } catch (err) {
    console.error('[render] error:', err)
    res.status(500).json({ error: String(err && err.message) || 'render failed' })
  }
})

/**
 * Take screenshots of every slide for vision QA (step 8 in the flow).
 *
 * Inputs accept either a server-side path (from a previous /render's
 * X-Pptx-Path header — fast path, no payload bloat) OR the .pptx as
 * base64 (fallback for clients that didn't keep the path).
 *
 * Pipeline: soffice headless → PDF → pdftoppm → PNG per slide.
 *
 * Returns base64 PNGs in slide order. Caller (CSP) streams each one to
 * a vision LLM (gemma4) for defect detection.
 */
app.post('/screenshots', async (req, res) => {
  try {
    const { pptxPath, pptxBase64 } = req.body || {}
    let workPath = null
    let cleanupPath = null

    if (pptxPath) {
      // Path-traversal guard: only accept files under our TMP_ROOT.
      const resolved = path.resolve(pptxPath)
      if (!resolved.startsWith(path.resolve(TMP_ROOT) + path.sep)) {
        return res.status(400).json({ error: 'pptxPath outside TMP_ROOT' })
      }
      if (!fs.existsSync(resolved)) {
        return res.status(404).json({ error: 'pptx file not found' })
      }
      workPath = resolved
    } else if (typeof pptxBase64 === 'string' && pptxBase64.length > 0) {
      const tmp = path.join(
        TMP_ROOT,
        `inline-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.pptx`,
      )
      fs.writeFileSync(tmp, Buffer.from(pptxBase64, 'base64'))
      workPath = tmp
      cleanupPath = tmp
    } else {
      return res
        .status(400)
        .json({ error: 'must supply pptxPath or pptxBase64' })
    }

    const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pptx-shots-'))

    try {
      // soffice converts to PDF in $workDir; output filename mirrors input.
      execFileSync(
        'soffice',
        [
          '--headless',
          '--convert-to', 'pdf',
          '--outdir', workDir,
          workPath,
        ],
        { stdio: ['ignore', 'pipe', 'pipe'], timeout: 90_000 },
      )
      const baseName = path.basename(workPath, '.pptx')
      const pdfPath = path.join(workDir, `${baseName}.pdf`)
      if (!fs.existsSync(pdfPath)) {
        throw new Error('soffice produced no PDF')
      }

      // -r 96 keeps file size bounded (~120 KB / slide); good enough for
      // vision QA which mainly looks at layout, not fine text rendering.
      execFileSync(
        'pdftoppm',
        ['-png', '-r', '96', pdfPath, path.join(workDir, 'slide')],
        { stdio: ['ignore', 'pipe', 'pipe'], timeout: 60_000 },
      )

      const files = fs
        .readdirSync(workDir)
        .filter((f) => f.startsWith('slide-') && f.endsWith('.png'))
        .sort() // pdftoppm zero-pads filenames so lexicographic sort matches slide order
      const images = files.map((f, idx) => ({
        index: idx,
        mime: 'image/png',
        base64: fs.readFileSync(path.join(workDir, f)).toString('base64'),
      }))

      res.json({ images })
    } finally {
      // Always clean up the soffice work dir; only delete the inline
      // upload if we created it (don't delete the caller's /render output).
      try {
        fs.rmSync(workDir, { recursive: true, force: true })
      } catch (e) {
        console.warn('[screenshots] workDir cleanup failed:', e.message)
      }
      if (cleanupPath) {
        try {
          fs.unlinkSync(cleanupPath)
        } catch {
          // best-effort
        }
      }
    }
  } catch (err) {
    console.error('[screenshots] error:', err)
    res
      .status(500)
      .json({ error: String((err && err.message) || 'screenshots failed') })
  }
})

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[pptx-renderer] listening on :${PORT}`)
})
