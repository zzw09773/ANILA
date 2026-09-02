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
const JSZip = require('jszip')
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

// Layered title-band scrim for full-bleed hero/section images. pptxgenjs 3.x
// has no native gradient fill, so we stack rects of decreasing transparency to
// fake a soft vertical darkening centred on the title band — GUARANTEES the
// white title stays legible no matter how bright the FLUX imagery is.
// Shared by the cover title slide and renderSectionBreak.
const GRADIENT_BANDS = [
  { y: 1.7, h: 3.8, transparency: 52 },
  { y: 2.2, h: 2.8, transparency: 42 },
  { y: 2.6, h: 2.0, transparency: 34 },
]

// ── Hierarchical bullet markers ──────────────────────────────────────
// Wire format stays list[str]; level is encoded as a leading Unicode
// marker that the renderer parses + strips so the visible text doesn't
// double up with pptxgenjs's own bullet rendering.
//
//   level 0 → ● U+25CF (default; no prefix needed for back-compat)
//   level 1 → ◦ U+25E6 (sub-point, indent 1)
//   level 2 → ▪ U+25AA (sub-sub-point, indent 2)
//
// Accept a couple of common visual synonyms for each level so an LLM
// that emits ○ instead of ◦ still gets the right hierarchy.
const BULLET_LEVEL_MARKERS = [
  { prefix: ['●', '・'],        code: '25CF', indent: 0 },
  { prefix: ['◦', '○', '◯'],    code: '25E6', indent: 1 },
  { prefix: ['▪', '■', '▫'],    code: '25AA', indent: 2 },
]

function parseBulletHierarchy(raw) {
  const text = String(raw)
  for (const m of BULLET_LEVEL_MARKERS) {
    for (const p of m.prefix) {
      // Accept `<marker> ` or `<marker>` followed by whitespace.
      if (text.startsWith(p + ' ') || text.startsWith(p + ' ') ||
          text.startsWith(p + '\t')) {
        return { text: text.slice(p.length).trimStart(), code: m.code,
                 indent: m.indent }
      }
      if (text === p) {
        return { text: '', code: m.code, indent: m.indent }
      }
    }
  }
  // No marker → default level 0 with ● (back-compat with legacy specs).
  return { text, code: '25CF', indent: 0 }
}

// ── Round 3 Patch M: theme bundles ────────────────────────────────────
//
// Themes are complete visual identity bundles. Each bundles palette +
// typography + chrome + iconTreatment + density. Renderers dispatch on
// theme.chrome.titleBar etc. to decide what to draw.
//
// corporate_navy MUST be visually equivalent to legacy navy_amber palette
// — schema-layer _PALETTE_TO_THEME maps navy_amber → corporate_navy and
// existing decks rendered against navy_amber must still look identical.
//
// Patch M is SCAFFOLDING ONLY: chrome variants beyond `filled` (legacy)
// fall back to the legacy filled behaviour. Patches N.1-N.4 will wire
// up the actual visual differentiation for the other 4 themes.
const THEMES = {
  corporate_navy: {
    id: 'corporate_navy',
    palette: {
      bar: '1E2761', accent: 'F4B740',
      titleText: '1E2761', barText: 'FFFFFF',
      ink: '1A1A1A', muted: '5C6470', bg: 'FFFFFF',
    },
    fonts: {
      title: 'Noto Sans CJK TC',
      body: 'Noto Sans CJK TC',
      titleWeight: 'bold',
      titleSize: { content: 26, section: 56, cover: 56 },
      bodySize: { default: 18, dense: 16, spacious: 20 },
    },
    chrome: {
      titleBar: 'filled',
      sectionBreak: 'side_strip',
      accentMotif: 'yellow_underline',
    },
    iconTreatment: {
      style: 'outline_circle',
      circleSize: 0.8,
      iconSize: 0.4,
      strokeWidth: 2,
    },
    density: 'comfortable',
  },

  academic_paper: {
    id: 'academic_paper',
    palette: {
      bar: 'FFFFFF', accent: 'A0826D',
      titleText: '212121', barText: '212121',
      ink: '212121', muted: '70757A', bg: 'FFFFFF',
    },
    fonts: {
      title: 'Noto Serif CJK TC',
      body: 'Noto Serif CJK TC',
      titleWeight: 'normal',
      titleSize: { content: 24, section: 48, cover: 56 },
      bodySize: { default: 16, dense: 14, spacious: 18 },
    },
    chrome: {
      titleBar: 'underline_only',
      sectionBreak: 'centered_minimal',
      accentMotif: 'none',
    },
    iconTreatment: {
      style: 'monochrome_dot',
      circleSize: 0,
      iconSize: 0.15,
    },
    density: 'dense',
  },

  warm_journal: {
    id: 'warm_journal',
    palette: {
      bar: 'FFF8F0', accent: 'D2691E',
      titleText: '4A3429', barText: '4A3429',
      ink: '4A3429', muted: '7D6857', bg: 'FFFCF7',
    },
    fonts: {
      title: 'Noto Sans CJK TC',
      body: 'Noto Sans CJK TC',
      titleWeight: 'semibold',
      titleSize: { content: 26, section: 52, cover: 56 },
      bodySize: { default: 18, dense: 16, spacious: 20 },
    },
    chrome: {
      titleBar: 'left_marker',
      sectionBreak: 'soft_centered',
      accentMotif: 'soft_highlight',
    },
    iconTreatment: {
      style: 'soft_filled',
      circleSize: 0,
      iconSize: 0.6,
    },
    density: 'comfortable',
  },

  executive_brief: {
    id: 'executive_brief',
    palette: {
      bar: 'FFFFFF', accent: '1A1A1A',
      titleText: '1A1A1A', barText: '1A1A1A',
      ink: '1A1A1A', muted: '8E8E93', bg: 'FFFFFF',
    },
    fonts: {
      title: 'Noto Sans CJK TC',
      body: 'Noto Sans CJK TC',
      titleWeight: 'medium',
      titleSize: { content: 22, section: 44, cover: 48 },
      bodySize: { default: 18, dense: 16, spacious: 22 },
    },
    chrome: {
      titleBar: 'none',
      sectionBreak: 'numbered_minimal',
      accentMotif: 'none',
    },
    iconTreatment: {
      style: 'minimal_dot',
      circleSize: 0,
      iconSize: 0.1,
    },
    density: 'spacious',
  },

  startup_pitch: {
    id: 'startup_pitch',
    palette: {
      bar: '2F3C7E', accent: 'F96167',
      titleText: '2F3C7E', barText: 'FFFFFF',
      ink: '1A1A1A', muted: '5C6470', bg: 'FFFFFF',
    },
    fonts: {
      title: 'Noto Sans CJK TC',
      body: 'Noto Sans CJK TC',
      titleWeight: 'black',
      titleSize: { content: 32, section: 72, cover: 96 },
      bodySize: { default: 20, dense: 18, spacious: 24 },
    },
    chrome: {
      titleBar: 'oversized_display',
      sectionBreak: 'full_bleed_number',
      accentMotif: 'highlight_pill',
    },
    iconTreatment: {
      style: 'filled_pill',
      circleSize: 0.9,
      iconSize: 0.5,
    },
    density: 'comfortable',
  },
}

function getTheme(name) {
  return THEMES[name] || THEMES.corporate_navy
}

// Legacy palette name → theme name. Lets callers that still send
// `palette` (tests, ops scripts, decks pre-Patch L) resolve to the
// closest theme. Schema-layer _PALETTE_TO_THEME mirrors this map.
const LEGACY_PALETTE_TO_THEME = {
  navy_amber: 'corporate_navy',
  forest_moss: 'warm_journal',
  charcoal_minimal: 'academic_paper',
  coral_energy: 'startup_pitch',
}

// ── Chrome dispatch helpers ──────────────────────────────────────────
//
// createContentSlide / applyTitleBar work as a pair. Every per-kind
// content renderer (everything except section_break) calls:
//
//     const slide = createContentSlide(pres, theme)
//     applyTitleBar(slide, s.title, theme)
//
// createContentSlide decides whether to attach the `ANILA_BASE` master
// (legacy `filled` titleBar — the master paints the coloured bar) or to
// create a blank slide with the theme's bg colour (modern variants like
// `left_marker` that don't want the painted bar).
//
// applyTitleBar then draws the title text (and any accent ornament such
// as the left marker block) on top.
//
// IMPORTANT: corporate_navy MUST keep using the master so legacy decks
// look pixel-identical to pre-Patch-M output.
function createContentSlide(pres, theme) {
  if (theme.chrome.titleBar === 'filled') {
    // Legacy path — master paints filled bar + background.
    return pres.addSlide({ masterName: 'ANILA_BASE' })
  }
  // Modern variants (left_marker, and N.2-N.4 reserved styles) — no
  // painted bar; set the slide bg to the theme's bg colour so the deck
  // gets its full identity (米白 for warm_journal, etc.).
  const slide = pres.addSlide()
  slide.background = { color: theme.palette.bg }
  return slide
}

// applyTitleBar: dispatches on theme.chrome.titleBar.
//
// `filled` (corporate_navy): master already painted the bar — we only
// draw the title text on top. Keeps legacy output identical.
//
// `left_marker` (warm_journal): 0.15"-wide accent block + brown title
// text on cream bg (createContentSlide already set the bg).
//
// Variants reserved for Patches N.2-N.4 (underline_only, none,
// oversized_display) fall back to the `filled` legacy text style for
// now so jobs using those themes still render something visible.
function applyTitleBar(slide, title, theme) {
  const p = theme.palette
  const fonts = theme.fonts
  const titleStr = String(title || '')

  switch (theme.chrome.titleBar) {
    case 'left_marker': {
      // Warm-journal accent block — small cinnamon rectangle at the
      // title's left edge. Stops well short of the slide edge so the
      // cream bg can breathe.
      slide.addShape('rect', {
        x: 0.5, y: 0.3, w: 0.15, h: 0.5,
        fill: { color: p.accent },
        line: { type: 'none' },
      })
      slide.addText(titleStr, {
        x: 0.8, y: 0.3, w: 12.0, h: 0.5,
        fontSize: 22, bold: true,
        color: p.titleText,
        align: 'left', valign: 'middle',
        fontFace: fonts.title, margin: 0,
      })
      break
    }
    case 'underline_only': {
      // Academic-paper variant — flush-left title in the theme's serif
      // face, no painted bar, with a thin muted rule beneath. Reads as
      // a research-paper section heading rather than a corporate banner.
      slide.addText(titleStr, {
        x: 0.5, y: 0.3, w: 12.3, h: 0.5,
        fontSize: fonts.titleSize.content,
        color: p.titleText,
        fontFace: fonts.title,
        align: 'left', valign: 'middle', margin: 0,
      })
      slide.addShape('line', {
        x: 0.5, y: 0.95, w: 12.3, h: 0,
        line: { color: p.muted, width: 0.5 },
      })
      break
    }
    case 'none': {
      // Executive-brief variant — no bar, no rule. Title sits flush
      // at slide top in muted text, letting the slide body do the
      // heavy lifting. Reads as a clean memo header.
      slide.addText(titleStr, {
        x: 0.5, y: 0.4, w: 12.3, h: 0.4,
        fontSize: fonts.titleSize.content,
        color: p.muted,
        fontFace: fonts.title,
        align: 'left', valign: 'middle', margin: 0,
      })
      break
    }
    case 'oversized_display': {
      // Startup-pitch variant — massive content-slide title with a
      // thin accent underline. ~32pt × 1.0" height (vs 26pt × 0.6"
      // for filled) gives the deck pitch-deck flair.
      slide.addText(titleStr, {
        x: 0.5, y: 0.2, w: 12.3, h: 1.0,
        fontSize: fonts.titleSize.content, bold: true,
        color: p.titleText,
        fontFace: fonts.title,
        align: 'left', valign: 'middle', margin: 0,
      })
      slide.addShape('line', {
        x: 0.5, y: 1.3, w: 4.0, h: 0,
        line: { color: p.accent, width: 3 },
      })
      break
    }
    case 'filled':
    default:
      // Legacy filled-bar title. The master already paints the bar
      // rectangle; we only draw the text on top of it.
      slide.addText(titleStr, {
        x: 0.5, y: 0.1, w: 12.3, h: 0.6,
        fontSize: fonts.titleSize.content, bold: true,
        color: p.barText,
        align: 'left', valign: 'middle',
        fontFace: fonts.title, margin: 0,
      })
      break
  }
}

// Reserved for variants; Patches N.1-N.4 implement specifics. For now
// returns theme.iconTreatment for callers to consult.
function getIconTreatment(theme) {
  return theme.iconTreatment
}

fs.mkdirSync(TMP_ROOT, { recursive: true })

const app = express()
app.use(express.json({ limit: MAX_PAYLOAD }))

// Request access log — written to stdout so `docker logs` shows
// every /render / /screenshots call without needing nginx reverse-proxy logs.
app.use((req, res, next) => {
  const start = Date.now();
  res.on("finish", () => {
    const ms = Date.now() - start;
    console.log(`${new Date().toISOString()} ${req.method} ${req.originalUrl} → ${res.statusCode} ${ms}ms`);
  });
  next();
});

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
function renderStandard(pres, s, theme) {
  // Round 3 Patch M: legacy shim — keep p.bar / p.accent / etc. working
  // unchanged in the body of the function. Renderer signature now takes
  // the full theme bundle.
  const p = theme.palette
  const slide = createContentSlide(pres, theme)
  applyTitleBar(slide, s.title || 'Untitled', theme)
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
    bullets.map((b) => {
      const parsed = parseBulletHierarchy(b)
      return {
        text: parsed.text,
        options: {
          // pptxgenjs treats `{type, code}` as mutually exclusive — passing
          // both results in neither being rendered (silent fail). `code`
          // alone with the unicode for the level's marker gives us a clean
          // dot bullet that LibreOffice and PowerPoint both honour.
          bullet: { code: parsed.code },
          // pptxgenjs honours per-paragraph indentLevel (separate from the
          // outer addText indentLevel) and indents proportionally.
          indentLevel: parsed.indent,
          color: p.ink,
        },
      }
    }),
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
 * Pick a section-break title fontSize that fits 11.5" width without
 * producing orphan-line breaks. Calibrated for Noto Sans CJK TC bold.
 *
 * Empirical: at 56pt bold ~12 CJK chars/line, 44pt ~16, 36pt ~20, 28pt ~26.
 * Latin chars are narrower; we weight CJK as 1.0 and Latin/digit as 0.55.
 */
function pickSectionTitleFont(title) {
  if (!title) return 56
  let weighted = 0
  for (const ch of String(title)) {
    weighted += /[一-鿿　-〿]/.test(ch) ? 1.0 : 0.55
  }
  if (weighted <= 12) return 56
  if (weighted <= 16) return 44
  if (weighted <= 20) return 36
  return 28
}

/**
 * Section break — full-bleed coloured background with centred large
 * title. Skips the master so there's no header bar (the entire slide
 * BECOMES the bar). Uses bullets[0] as a subtitle if provided.
 */
function renderSectionBreak(pres, s, theme) {
  const p = theme.palette // Round 3 Patch M: legacy shim
  const bullets = Array.isArray(s.bullets) ? s.bullets : []
  const titleStr = String(s.title || '')
  const titleFont = pickSectionTitleFont(titleStr)

  // FLUX cover hero: when the backend hydrated a hero image into this
  // slide (the cover is commonly marked layout_kind="section_break", so
  // the cover-hero image_data lands here rather than on the prepended
  // titleSlide; also future Stage 4 section bands), render it full-bleed
  // with a dark scrim + white centred title. Independent early-return
  // path so the existing colour-slab chrome variants stay untouched.
  const heroData = s.image_data
  const hasHero =
    typeof heroData === 'string' && heroData.startsWith('data:image/')
  if (hasHero) {
    const heroSlide = pres.addSlide()
    heroSlide.addImage({
      data: heroData,
      x: 0, y: 0, w: 13.33, h: 7.5,
      sizing: { type: 'cover', w: 13.33, h: 7.5 },
    })
    // Full-bleed base scrim — unify tone, keep imagery visible.
    heroSlide.addShape('rect', {
      x: 0, y: 0, w: 13.33, h: 7.5,
      fill: { color: '000000', transparency: 58 },
      line: { type: 'none' },
    })
    // Title-band gradient scrim (shared GRADIENT_BANDS): outer→inner layered
    // rects so the centre behind the title is darkest and the edges feather
    // out, guaranteeing legibility over arbitrary FLUX imagery.
    for (const band of GRADIENT_BANDS) {
      heroSlide.addShape('rect', {
        x: 0, y: band.y, w: 13.33, h: band.h,
        fill: { color: '000000', transparency: band.transparency },
        line: { type: 'none' },
      })
    }
    heroSlide.addText(titleStr, {
      x: 1.0, y: 2.6, w: 11.3, h: 1.6,
      fontSize: titleFont, bold: true, color: 'FFFFFF',
      align: 'center', valign: 'middle',
      fontFace: theme.fonts.title, margin: 0,
    })
    if (bullets[0]) {
      heroSlide.addText(String(bullets[0]), {
        x: 1.0, y: 4.4, w: 11.3, h: 0.6,
        fontSize: 20, color: 'F0F0F0',
        align: 'center', italic: false,
        fontFace: theme.fonts.body, margin: 0,
      })
    }
    return
  }

  // No master — full-bleed colour fill regardless of variant.
  const slide = pres.addSlide()

  // Patch N.1/N.2: branch on theme.chrome.sectionBreak.
  //
  // `soft_centered` (warm_journal): cream bg, centered brown title,
  // muted brown subtitle. NO left accent strip — the slide reads as a
  // quiet pause rather than a coloured slab.
  //
  // `centered_minimal` (academic_paper): white bg, centered serif title,
  // a short tan accent rule below, optional muted subtitle. Reads as a
  // page break in a printed paper rather than a coloured banner.
  //
  // `side_strip` (corporate_navy + fallback): legacy filled bar bg
  // with white title and cinnamon accent strip on the left.
  if (theme.chrome.sectionBreak === 'soft_centered') {
    slide.background = { color: p.bg }
    slide.addText(titleStr, {
      x: 1.0, y: 2.6, w: 11.5, h: 1.4,
      fontSize: titleFont, bold: true,
      color: p.titleText,
      align: 'center', valign: 'middle',
      fontFace: theme.fonts.title, margin: 0,
    })
    if (bullets[0]) {
      slide.addText(String(bullets[0]), {
        x: 1.0, y: 4.4, w: 11.5, h: 0.6,
        fontSize: 20, color: p.muted,
        align: 'center', italic: false,
        fontFace: theme.fonts.body, margin: 0,
      })
    }
  } else if (theme.chrome.sectionBreak === 'centered_minimal') {
    slide.background = { color: theme.palette.bg }
    slide.addText(titleStr, {
      x: 1.0, y: 2.8, w: 11.5, h: 1.4,
      fontSize: titleFont,
      color: theme.palette.titleText,
      align: 'center', valign: 'middle',
      fontFace: theme.fonts.title,
      margin: 0,
    })
    // Thin rule beneath the title — short and centred, evoking the
    // separator lines in academic typesetting.
    slide.addShape('line', {
      x: 4.5, y: 4.4, w: 4.5, h: 0,
      line: { color: theme.palette.accent, width: 0.75 },
    })
    if (bullets[0]) {
      slide.addText(String(bullets[0]), {
        x: 1.0, y: 4.7, w: 11.5, h: 0.5,
        fontSize: 18, color: theme.palette.muted,
        align: 'center', italic: false,
        fontFace: theme.fonts.body, margin: 0,
      })
    }
  } else if (theme.chrome.sectionBreak === 'numbered_minimal') {
    // Executive-brief variant — white bg, huge light-weight accent
    // number on the left, thin section title on the right. Extracts
    // the chapter number from common patterns ("第N章", "Chapter N",
    // "Part N", "N.") via regex; falls back to a centred title-only
    // layout if no number is found.
    slide.background = { color: theme.palette.bg }
    const numMatch = titleStr.match(/第\s*([一二三四五六七八九十0-9]+)\s*章|Chapter\s+(\d+)|Part\s+(\d+)|^(\d+)[\.：:]/i)
    const numberStr = numMatch
      ? (numMatch[1] || numMatch[2] || numMatch[3] || numMatch[4] || '')
      : ''
    const restStr = titleStr.replace(/第\s*[一二三四五六七八九十0-9]+\s*章[:：]?\s*|Chapter\s+\d+[:：]?\s*|Part\s+\d+[:：]?\s*|^\d+[\.：:]\s*/i, '').trim() || titleStr
    if (numberStr) {
      slide.addText(numberStr, {
        x: 0.5, y: 1.8, w: 5.5, h: 4.0,
        fontSize: 200, bold: false,
        color: theme.palette.accent,
        align: 'right', valign: 'middle',
        fontFace: theme.fonts.title, margin: 0,
      })
      slide.addText(restStr, {
        x: 6.3, y: 2.6, w: 6.5, h: 2.0,
        fontSize: 36,
        color: theme.palette.titleText,
        align: 'left', valign: 'middle',
        fontFace: theme.fonts.title, margin: 0,
      })
      if (bullets[0]) {
        slide.addText(String(bullets[0]), {
          x: 6.3, y: 4.8, w: 6.5, h: 0.6,
          fontSize: 16, color: theme.palette.muted,
          align: 'left', italic: false,
          fontFace: theme.fonts.body, margin: 0,
        })
      }
    } else {
      // Fallback: no number pattern — just render the title centred
      // so the slide still reads as a minimalist break.
      slide.addText(restStr, {
        x: 1.0, y: 2.8, w: 11.5, h: 1.4,
        fontSize: titleFont,
        color: theme.palette.titleText,
        align: 'center', valign: 'middle',
        fontFace: theme.fonts.title, margin: 0,
      })
      if (bullets[0]) {
        slide.addText(String(bullets[0]), {
          x: 1.0, y: 4.4, w: 11.5, h: 0.6,
          fontSize: 18, color: theme.palette.muted,
          align: 'center', italic: false,
          fontFace: theme.fonts.body, margin: 0,
        })
      }
    }
  } else if (theme.chrome.sectionBreak === 'full_bleed_number') {
    // Startup-pitch variant — full-bleed navy bg, massive coral
    // chapter number filling the left half, white section title on
    // the right. High-impact pitch-deck aesthetic.
    slide.background = { color: theme.palette.bar }
    const numMatch = titleStr.match(/第\s*([一二三四五六七八九十0-9]+)\s*章|Chapter\s+(\d+)|Part\s+(\d+)|^(\d+)[\.：:]/i)
    const numberStr = numMatch
      ? (numMatch[1] || numMatch[2] || numMatch[3] || numMatch[4] || '')
      : ''
    const restStr = titleStr.replace(/第\s*[一二三四五六七八九十0-9]+\s*章[:：]?\s*|Chapter\s+\d+[:：]?\s*|Part\s+\d+[:：]?\s*|^\d+[\.：:]\s*/i, '').trim() || titleStr
    if (numberStr) {
      slide.addText(numberStr, {
        x: 0.5, y: 1.0, w: 6.0, h: 5.5,
        fontSize: 240, bold: true,
        color: theme.palette.accent,
        align: 'center', valign: 'middle',
        fontFace: theme.fonts.title, margin: 0,
      })
      slide.addText(restStr, {
        x: 6.8, y: 2.5, w: 6.0, h: 2.5,
        fontSize: 48, bold: true,
        color: 'FFFFFF',
        align: 'left', valign: 'middle',
        fontFace: theme.fonts.title, margin: 0,
      })
      if (bullets[0]) {
        slide.addText(String(bullets[0]), {
          x: 6.8, y: 5.2, w: 6.0, h: 0.6,
          fontSize: 20, color: 'FFFFFF', italic: false,
          fontFace: theme.fonts.body, margin: 0,
        })
      }
    } else {
      // Fallback: no number — centre the title in white over navy.
      slide.addText(restStr, {
        x: 1.0, y: 2.6, w: 11.5, h: 1.8,
        fontSize: titleFont, bold: true,
        color: 'FFFFFF',
        align: 'center', valign: 'middle',
        fontFace: theme.fonts.title, margin: 0,
      })
      if (bullets[0]) {
        slide.addText(String(bullets[0]), {
          x: 1.0, y: 4.6, w: 11.5, h: 0.6,
          fontSize: 20, color: theme.palette.accent,
          align: 'center', italic: false,
          fontFace: theme.fonts.body, margin: 0,
        })
      }
    }
  } else {
    // Legacy `side_strip` (corporate_navy + reserved variants).
    slide.background = { color: p.bar }
    // Decorative amber strip on the left edge — single visual motif
    // shared with the cover slide.
    slide.addShape('rect', {
      x: 0.6, y: 1.6, w: 0.14, h: 4.2,
      fill: { color: p.accent },
      line: { type: 'none' },
    })
    slide.addText(titleStr, {
      x: 1.1, y: 2.4, w: 11.5, h: 1.8,
      fontSize: titleFont, bold: true, color: 'FFFFFF',
      align: 'left', valign: 'middle', fontFace: FONT_FACE,
    })
    // Subtitle from bullets[0] if the LLM provided one — keeps the slide
    // useful even when it's clearly just a transition.
    if (bullets[0]) {
      slide.addText(String(bullets[0]), {
        x: 1.1, y: 4.4, w: 11.5, h: 0.6,
        fontSize: 20, color: p.accent,
        align: 'left', italic: false, fontFace: FONT_FACE,
      })
    }
  }
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Stat callout — title bar + huge number + label.
 * Studio Fix 3 (2026-05-18): two visual modes.
 *
 *  - Comparison mode (when `stat.baseline` present): split content area
 *    into left (small baseline + label) → arrow → right (big value + label).
 *    Makes "78% → 95%" gains immediately legible without the user having
 *    to parse a paragraph.
 *  - Solo mode (default): vertical-center the value+label block in the
 *    main content area, with `supporting` directly below the value.
 *    Avoids the old layout's bottom-heavy whitespace problem.
 *
 * In both modes, `bullets[0]` (if present) becomes a small muted-colour
 * footer "takeaway" near the slide bottom — typically a one-line so-what
 * sentence so the slide doesn't end with raw numbers and no narrative.
 *
 * Falls back to standard if `stat.value` / `stat.label` are missing.
 */
function renderStatCallout(pres, s, theme) {
  const p = theme.palette // Round 3 Patch M: legacy shim
  if (!s.stat || !s.stat.value || !s.stat.label) {
    return renderStandard(pres, s, theme)
  }
  const slide = createContentSlide(pres, theme)
  applyTitleBar(slide, s.title, theme)

  const hasBaseline = !!(s.stat.baseline && String(s.stat.baseline).trim())
  const bullets = Array.isArray(s.bullets) ? s.bullets : []

  if (hasBaseline) {
    // ── Comparison mode: baseline ← arrow → value ──
    // Left third = baseline (small), middle = arrow, right two-thirds =
    // value (large). Vertical extent 1.4 → 5.0 so 'supporting' still
    // has room below for the gain narrative.
    const baselineLabel = String(s.stat.baseline_label || '基準').trim()
    slide.addText(String(s.stat.baseline), {
      x: 0.5, y: 1.6, w: 4.0, h: 2.4,
      fontSize: 60, bold: true, color: p.muted,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
    slide.addText(baselineLabel, {
      x: 0.5, y: 3.9, w: 4.0, h: 0.5,
      fontSize: 16, color: p.muted,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
    // Arrow centre column — uses accent colour to direct the eye.
    slide.addText('→', {
      x: 4.5, y: 1.6, w: 1.3, h: 2.4,
      fontSize: 72, bold: true, color: p.accent,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
    slide.addText(String(s.stat.value), {
      x: 5.8, y: 1.4, w: 7.0, h: 2.8,
      fontSize: 88, bold: true, color: p.accent,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
    slide.addText(String(s.stat.label), {
      x: 5.8, y: 4.2, w: 7.0, h: 0.6,
      fontSize: 22, color: p.ink,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
    if (s.stat.supporting) {
      slide.addText(String(s.stat.supporting), {
        x: 1.0, y: 5.0, w: 11.3, h: 1.2,
        fontSize: 16, color: p.muted, italic: true,
        align: 'center', valign: 'top', fontFace: FONT_FACE,
      })
    }
  } else {
    // ── Solo mode: vertical-centre value+label+supporting as one block ──
    // Content area runs ~0.84 → 6.5 inches (above the takeaway footer).
    // Block heights: value 2.4 + label 0.7 + supporting 1.0 = 4.1 inches
    // plus 0.2 gaps = 4.5 inches total. Centre of (0.84, 6.5) is 3.67;
    // block starts at y = 3.67 - 4.5/2 = 1.42. We round to y=2.0 to push
    // visually slightly south of geometric centre (looks balanced after
    // accounting for the title bar's heavy top weight).
    slide.addText(String(s.stat.value), {
      x: 0.5, y: 2.0, w: 12.3, h: 2.4,
      fontSize: 96, bold: true, color: p.accent,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
    slide.addText(String(s.stat.label), {
      x: 1.0, y: 4.6, w: 11.3, h: 0.7,
      fontSize: 28, color: p.ink,
      align: 'center', valign: 'middle', fontFace: FONT_FACE,
    })
    if (s.stat.supporting) {
      slide.addText(String(s.stat.supporting), {
        x: 1.0, y: 5.4, w: 11.3, h: 1.0,
        fontSize: 16, color: p.muted, italic: true,
        align: 'center', valign: 'top', fontFace: FONT_FACE,
      })
    }
  }

  // Footer takeaway — bullets[0] (if any) as a small muted line near
  // the bottom. Common pattern: "因此 X 可以做 Y" so the slide ends
  // with a "so what" instead of raw figures.
  if (bullets[0]) {
    slide.addText(String(bullets[0]), {
      x: 0.5, y: 6.5, w: 12.3, h: 0.5,
      fontSize: 14, color: p.muted, italic: true,
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
function renderQuote(pres, s, theme) {
  const p = theme.palette // Round 3 Patch M: legacy shim
  if (!s.quote || !s.quote.text) {
    return renderStandard(pres, s, theme)
  }
  const slide = createContentSlide(pres, theme)
  applyTitleBar(slide, s.title, theme)
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
 *
 * Studio Fix 3 (2026-05-18): with the schema floor raising bullets to
 * min 3 per column, density is enforced upstream. As a polish step, if
 * a column is *short on text* (<= 3 bullets), we add a thin muted-colour
 * divider at the bottom edge so both columns visually anchor at the
 * same height even when content is sparse. Tall columns skip the
 * divider so it doesn't crowd the last bullet.
 */
function renderTwoColumn(pres, s, theme) {
  const p = theme.palette // Round 3 Patch M: legacy shim
  if (!Array.isArray(s.columns) || s.columns.length < 2) {
    return renderStandard(pres, s, theme)
  }
  const cols = s.columns.slice(0, 2)
  const slide = createContentSlide(pres, theme)
  applyTitleBar(slide, s.title, theme)
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
      bullets.map((b) => {
        const parsed = parseBulletHierarchy(b)
        return {
          text: parsed.text,
          options: { bullet: { code: parsed.code }, indentLevel: parsed.indent, color: p.ink },
        }
      }),
      {
        x: x + 0.1, y: 2.0, w: COL_WIDTH - 0.1, h: 4.8,
        fontSize: 18, color: p.ink, fontFace: FONT_FACE,
        paraSpaceAfter: 12, valign: 'top',
      },
    )
    // Bottom anchor divider for short columns. Heuristic: 3 bullets
    // roughly fills 2.5 inches at fontSize 18 with paraSpaceAfter 12,
    // so anything <= 3 bullets benefits from a visual bottom edge to
    // avoid the column looking like it floated up. Muted colour so it
    // reads as a soft baseline, not a strong divider.
    if (bullets.length <= 3) {
      slide.addShape('rect', {
        x, y: 6.85, w: COL_WIDTH, h: 0.015,
        fill: { color: p.muted }, line: { type: 'none' },
      })
    }
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
async function renderIconRows(pres, s, theme) {
  const p = theme.palette // Round 3 Patch M: legacy shim
  if (!Array.isArray(s.icon_rows) || s.icon_rows.length === 0) {
    return renderStandard(pres, s, theme)
  }
  const rows = s.icon_rows.slice(0, 5)
  const slide = createContentSlide(pres, theme)
  applyTitleBar(slide, s.title, theme)

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
  // Patch BB: filled_pill (startup_pitch) lays the glyph ON the
  // accent-filled circle — glyph and fill would be the same colour and
  // the icon disappears. Force white glyphs for that style. Every other
  // style tints the glyph in accent over a white / cream / transparent
  // background, where accent reads fine — keep those unchanged.
  const glyphColor =
    theme.iconTreatment.style === 'filled_pill' ? '#FFFFFF' : `#${p.accent}`
  const iconPngs = await Promise.all(
    rows.map((r) => renderIconPng(r.concept, { color: glyphColor, size: 256 })),
  )

  rows.forEach((r, i) => {
    const y = TOP + i * (rowH + GAP)

    if (iconPngs[i]) {
      // Patch N.1: branch on theme.iconTreatment.style.
      //
      // `soft_filled` (warm_journal): NO outline circle — just the
      // heroicon glyph, scaled larger (`iconTreatment.iconSize`) and
      // directly tinted in the accent colour. Reads as a warm,
      // hand-illustrated motif rather than a button.
      //
      // `outline_circle` (corporate_navy + fallback): white-fill circle
      // + accent ring + glyph. Originally tried a tinted fill via
      // `${p.accent}22` (8-char hex with alpha), but pptxgenjs's
      // shape `fill.color` doesn't honour an alpha channel — the value
      // gets silently dropped or mis-parsed, which left rows without an
      // icon image showing a black ellipse interior. White fill + 2pt
      // accent ring is robust across pptxgenjs / LibreOffice / PowerPoint
      // versions, and gives the same "icon in a coloured circle" motif
      // SKILL.md recommends for contrast.
      const iconStyle = theme.iconTreatment.style
      const iconX = ICON_X
      const iconY = y + (rowH - iconBoxSize) / 2
      if (iconStyle === 'soft_filled') {
        // Larger glyph, no chrome. Centre the requested icon size inside
        // iconBoxSize so the heading/description text columns stay aligned.
        const glyphSize = Math.min(theme.iconTreatment.iconSize || 0.6, iconBoxSize)
        const inset = (iconBoxSize - glyphSize) / 2
        slide.addImage({
          data: `data:image/png;base64,${iconPngs[i].toString('base64')}`,
          x: iconX + inset,
          y: iconY + inset,
          w: glyphSize,
          h: glyphSize,
        })
      } else if (iconStyle === 'monochrome_dot') {
        // Academic-paper variant — drop the heroicon entirely and place
        // a tiny muted dot where the legacy outline circle's centre would
        // sit. Lets the heading + description text carry the visual weight
        // (matches the "minimal chrome, serif typography" identity of the
        // theme). 0.15" dot centred in the legacy 0.8" box → offset 0.325".
        slide.addShape('ellipse', {
          x: iconX + 0.325, y: iconY + 0.325, w: 0.15, h: 0.15,
          fill: { color: theme.palette.muted },
          line: { type: 'none' },
        })
      } else if (iconStyle === 'minimal_dot') {
        // Executive-brief variant — even smaller dot than monochrome_dot
        // (0.10" vs 0.15"), no surrounding circle, no heroicon. Pure
        // typography emphasis; the dot is just a quiet bullet marker.
        slide.addShape('ellipse', {
          x: iconX + 0.35, y: iconY + 0.35, w: 0.1, h: 0.1,
          fill: { color: theme.palette.muted },
          line: { type: 'none' },
        })
      } else if (iconStyle === 'filled_pill') {
        // Startup-pitch variant — accent-filled circle with a WHITE
        // heroicon glyph on top. glyphColor is forced to #FFFFFF above for
        // this style (Patch BB), giving the white-on-coral "pill" the
        // pitch deck wants. High-impact, high-contrast.
        slide.addShape('ellipse', {
          x: iconX, y: iconY, w: 0.9, h: 0.9,
          fill: { color: theme.palette.accent },
          line: { type: 'none' },
        })
        if (iconPngs[i]) {
          slide.addImage({
            data: `data:image/png;base64,${iconPngs[i].toString('base64')}`,
            x: iconX + 0.225, y: iconY + 0.225, w: 0.45, h: 0.45,
          })
        }
      } else {
        // Legacy outline_circle treatment.
        slide.addShape('ellipse', {
          x: iconX, y: iconY,
          w: iconBoxSize, h: iconBoxSize,
          fill: { color: 'FFFFFF' },
          line: { color: p.accent, width: 2 },
        })
        // Inset 12% of the box so the glyph doesn't kiss the circle edge.
        const inset = iconBoxSize * 0.18
        slide.addImage({
          data: `data:image/png;base64,${iconPngs[i].toString('base64')}`,
          x: iconX + inset,
          y: iconY + inset,
          w: iconBoxSize - inset * 2,
          h: iconBoxSize - inset * 2,
        })
      }
    } else {
      // Unknown concept: small filled dot. Empty 0.8" outlined circles
      // read as "broken icon"; a small accent dot reads as intentional
      // minimalism. Log so future runs can mine unknowns for additions.
      //
      // Patch T (Round 4): pin to a fixed FALLBACK_DOT_SIZE rather than
      // anything derived from theme.iconTreatment.iconSize. warm_journal's
      // soft_filled style sets iconSize=0.6" (calibrated for an actual
      // heroicon glyph) — if the fallback ever scales with iconSize it
      // renders as a giant 0.6" orange disc that looks like a "broken
      // placeholder", defeating the point. Purpose of this dot is "small
      // unknown-concept marker", so the size shouldn't track the heroicon
      // calibration.
      const FALLBACK_DOT_SIZE = 0.12
      slide.addShape('ellipse', {
        x: ICON_X + (iconBoxSize - FALLBACK_DOT_SIZE) / 2,
        y: y + (rowH - iconBoxSize) / 2 + (iconBoxSize - FALLBACK_DOT_SIZE) / 2,
        w: FALLBACK_DOT_SIZE, h: FALLBACK_DOT_SIZE,
        fill: { color: p.accent },
        line: { type: 'none' },
      })
      console.warn(`[icon_rows] unknown concept "${r.concept}" — drew dot`)
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
function renderImageFocus(pres, s, theme) {
  const p = theme.palette // Round 3 Patch M: legacy shim
  if (!s.image_data || typeof s.image_data !== 'string') {
    return renderStandard(pres, s, theme)
  }
  const slide = createContentSlide(pres, theme)

  // Title bar — same as other layouts.
  applyTitleBar(slide, s.title, theme)

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
      bullets.map((b) => {
        const parsed = parseBulletHierarchy(b)
        return {
          text: parsed.text,
          options: { bullet: { code: parsed.code }, indentLevel: parsed.indent, color: p.ink },
        }
      }),
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
 * Process — 2-6 numbered steps left→right (one row up to 4, two rows for
 * 5-6). The deterministic answer to "流程圖" slides: no diagram engine, no
 * model-written DOT, always legible. Falls back to standard without steps.
 */
function renderProcess(pres, s, theme) {
  const p = theme.palette
  const steps = Array.isArray(s.steps) ? s.steps.filter((st) => st && (st.heading || st.description)).slice(0, 6) : []
  if (steps.length < 2) return renderStandard(pres, s, theme)
  const slide = createContentSlide(pres, theme)
  applyTitleBar(slide, s.title, theme)
  const perRow = steps.length <= 4 ? steps.length : Math.ceil(steps.length / 2)
  const rows = steps.length <= 4 ? 1 : 2
  const LEFT = 0.6, RIGHT = 12.73, TOP = 1.3, BOTTOM = 6.9
  const gap = 0.35
  const colW = (RIGHT - LEFT - gap * (perRow - 1)) / perRow
  const rowH = (BOTTOM - TOP - (rows - 1) * 0.3) / rows
  const circle = 0.7
  steps.forEach((st, i) => {
    const r = Math.floor(i / perRow)
    const c = i % perRow
    const x = LEFT + c * (colW + gap)
    const y = TOP + r * (rowH + 0.3)
    // connector to the next step in the same row
    if (c < perRow - 1 && i < steps.length - 1) {
      slide.addShape('line', {
        x: x + colW, y: y + circle / 2, w: gap, h: 0,
        line: { color: p.accent, width: 2, endArrowType: 'triangle' },
      })
    }
    slide.addShape('ellipse', {
      x, y, w: circle, h: circle,
      fill: { color: p.accent }, line: { type: 'none' },
    })
    slide.addText(String(i + 1), {
      x, y, w: circle, h: circle,
      fontSize: 22, bold: true, color: 'FFFFFF',
      align: 'center', valign: 'middle', fontFace: FONT_FACE, margin: 0,
    })
    slide.addText(String(st.heading || ''), {
      x, y: y + circle + 0.15, w: colW, h: 0.6,
      fontSize: perRow <= 3 ? 20 : 17, bold: true, color: p.ink,
      align: 'left', valign: 'top', fontFace: FONT_FACE, margin: 0,
    })
    slide.addText(String(st.description || ''), {
      x, y: y + circle + 0.8, w: colW, h: rowH - circle - 0.9,
      fontSize: perRow <= 3 ? 15 : 13, color: p.muted,
      align: 'left', valign: 'top', fontFace: FONT_FACE, margin: 0,
    })
  })
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Table — a native, editable pptx table. Header row in the theme bar
 * colour, body rows alternating. Up to 5 columns × 10 rows; font size
 * scales with row count. Falls back to standard without columns/rows.
 */
function renderTable(pres, s, theme) {
  const p = theme.palette
  const t = s.table || {}
  const columns = Array.isArray(t.columns) ? t.columns.slice(0, 5).map(String) : []
  const body = Array.isArray(t.rows) ? t.rows.filter(Array.isArray).slice(0, 10) : []
  if (columns.length < 2 || body.length < 1) return renderStandard(pres, s, theme)
  const slide = createContentSlide(pres, theme)
  applyTitleBar(slide, s.title, theme)
  const fontSize = body.length <= 5 ? 16 : body.length <= 8 ? 14 : 12
  const header = columns.map((c) => ({
    text: c,
    options: { bold: true, color: p.barText === 'FFFFFF' ? 'FFFFFF' : p.ink, fill: { color: p.bar }, align: 'left', valign: 'middle' },
  }))
  const rows = body.map((r, i) => columns.map((_, ci) => ({
    text: String(r[ci] ?? ''),
    options: { color: p.ink, fill: { color: i % 2 === 0 ? 'FFFFFF' : 'F3F4F6' }, align: 'left', valign: 'middle' },
  })))
  const W = 12.33
  const firstW = Math.min(3.2, W / columns.length)
  const restW = (W - firstW) / (columns.length - 1)
  // Rows stretch to use the body (up to 1" each) so a four-row table does
  // not sit as a strip under the title; h is passed explicitly because
  // pptxgenjs otherwise writes a 1" frame extent regardless of rows.
  const rowH = Math.min(1.0, 5.6 / (rows.length + 1))
  slide.addTable([header, ...rows], {
    x: 0.5, y: 1.2, w: W, h: rowH * (rows.length + 1),
    colW: [firstW, ...Array(columns.length - 1).fill(restW)],
    fontSize, fontFace: FONT_FACE, color: p.ink,
    border: { type: 'solid', pt: 0.75, color: 'D1D5DB' },
    rowH,
    margin: 0.08,
    autoPage: false,
  })
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Sources — closing slide listing the documents the deck drew on. Written
 * by the pipeline (not the model) from the retrieved chunks, so it is the
 * one slide whose facts are guaranteed. Two columns past six entries.
 */
function renderSources(pres, s, theme) {
  const p = theme.palette
  const items = Array.isArray(s.sources) ? s.sources.filter((it) => it && it.label).slice(0, 14) : []
  if (items.length === 0) return renderStandard(pres, s, theme)
  const slide = createContentSlide(pres, theme)
  applyTitleBar(slide, s.title || '資料來源', theme)
  const twoCols = items.length > 6
  const perCol = twoCols ? Math.ceil(items.length / 2) : items.length
  const colW = twoCols ? 5.9 : 12.3
  const lineH = Math.min(0.62, 5.4 / perCol)
  items.forEach((it, i) => {
    const col = twoCols ? Math.floor(i / perCol) : 0
    const row = twoCols ? i % perCol : i
    const x = 0.5 + col * (colW + 0.5)
    const y = 1.25 + row * lineH
    slide.addText([
      { text: String(it.label), options: { bold: true, color: p.ink, fontSize: 14 } },
      ...(it.note ? [{ text: `　${String(it.note)}`, options: { color: p.muted, fontSize: 12 } }] : []),
    ], { x, y, w: colW, h: lineH, fontFace: FONT_FACE, valign: 'middle', margin: 0 })
  })
  slide.addText('內容由 ANILA 依上列文件自動整理；條號與數字請以原文為準。', {
    x: 0.5, y: 6.75, w: 12.3, h: 0.4, fontSize: 11, color: p.muted, italic: true, fontFace: FONT_FACE, margin: 0,
  })
  if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
  return slide
}

/**
 * Dispatcher — picks the renderer based on slide.layout_kind. Unknown
 * kinds fall back to `renderStandard`. Async so callers can `await` it
 * uniformly even though only icon_rows is actually async.
 */
async function renderSlideByKind(pres, s, theme) {
  const slide = await renderSlideByKindInner(pres, s, theme)
  const kind = effectiveKind(s)
  if (slide && kind !== 'section_break' && kind !== 'sources') applySourceFooter(slide, s, theme)
  return slide
}

/**
 * Provenance footer — the pipeline fills Slide.source_line from the [N]
 * references on that slide. Small, muted, left of the page number.
 */
function applySourceFooter(slide, s, theme) {
  const line = typeof s.source_line === 'string' ? s.source_line.trim() : ''
  if (!line) return
  slide.addText(line, {
    x: 0.5, y: 7.05, w: 11.8, h: 0.3,
    fontSize: 10, color: theme.palette.muted, fontFace: FONT_FACE,
    align: 'left', valign: 'middle', margin: 0,
  })
}

async function renderSlideByKindInner(pres, s, theme) {
  const kind = String(s.layout_kind || 'standard')
  switch (kind) {
    case 'section_break': return renderSectionBreak(pres, s, theme)
    case 'stat_callout':  return renderStatCallout(pres, s, theme)
    case 'quote':         return renderQuote(pres, s, theme)
    case 'two_column':    return renderTwoColumn(pres, s, theme)
    case 'icon_rows':     return await renderIconRows(pres, s, theme)
    case 'image_focus':   return renderImageFocus(pres, s, theme)
    case 'process':       return renderProcess(pres, s, theme)
    case 'table':         return renderTable(pres, s, theme)
    case 'sources':       return renderSources(pres, s, theme)
    default:              return renderStandard(pres, s, theme)
  }
}

/**
 * Render a spec to a .pptx buffer (no HTTP). Returns
 *   { buffer, coverPrepended, kinds }
 * where `kinds` lists every rendered slide's layout kind in order — with a
 * synthetic 'cover' first when the renderer prepended its own title slide —
 * so QA callers can index defects back to the spec and judge each slide by
 * what it is (a cover is sparse by design; a bullet page is not).
 */
async function renderSpec(spec) {
  if (!spec || typeof spec !== 'object') throw new Error('missing spec')
  if (!Array.isArray(spec.slides) || spec.slides.length === 0) {
    throw new Error('spec.slides must be non-empty array')
  }
  if (spec.slides.length > MAX_SLIDES) {
    const err = new Error(`spec.slides exceeds cap of ${MAX_SLIDES}`)
    err.status = 413
    throw err
  }

  // Round 3 Patch M: theme is the new identity unit, palette is legacy.
  // Resolution priority: spec.theme → legacy spec.palette → default.
  let themeName = 'corporate_navy'
  if (spec.theme && THEMES[spec.theme]) {
    themeName = spec.theme
  } else if (spec.palette && LEGACY_PALETTE_TO_THEME[spec.palette]) {
    themeName = LEGACY_PALETTE_TO_THEME[spec.palette]
  }
  const theme = getTheme(themeName)
  const p = theme.palette

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
      { rect: { x: 0, y: 0, w: '100%', h: 0.8, fill: { color: p.bar } } },
      // Thin accent strip immediately below the bar — single visual
      // motif we repeat across the deck.
      { rect: { x: 0, y: 0.8, w: '100%', h: 0.04, fill: { color: p.accent } } },
    ],
    // Explicit w: pptxgenjs defaults to 0.875" which at x=12.5 overflows the
    // 13.333" slide and made every content slide a false 'critical overflow'.
    slideNumber: { x: 12.5, y: 7.1, w: 0.7, h: 0.3, fontSize: 10, color: p.muted, fontFace: FONT_FACE },
  })

  const kinds = []
  // ── Cover slide (always first, distinct from section_break) ──
  // If spec.slides[0].layout_kind === 'section_break' the LLM is signalling
  // "my first slide is the cover" — renderSectionBreak handles it and we
  // skip the auto-generated cover.
  const coverPrepended = spec.slides[0]?.layout_kind !== 'section_break'
  if (coverPrepended) {
    kinds.push('cover')
    const titleSlide = pres.addSlide({ masterName: 'ANILA_BASE' })
    const heroData = spec.slides[0]?.image_data
    const hasHero =
      typeof heroData === 'string' &&
      heroData.startsWith('data:image/') &&
      spec.slides[0]?.image_gen_meta?.use_case === 'cover_hero'
    if (hasHero) {
      titleSlide.addImage({
        data: heroData,
        x: 0, y: 0, w: 13.33, h: 7.5,
        sizing: { type: 'cover', w: 13.33, h: 7.5 },
      })
      titleSlide.addShape('rect', {
        x: 0, y: 0, w: 13.33, h: 7.5,
        fill: { color: '000000', transparency: 58 },
        line: { type: 'none' },
      })
      for (const band of GRADIENT_BANDS) {
        titleSlide.addShape('rect', {
          x: 0, y: band.y, w: 13.33, h: band.h,
          fill: { color: '000000', transparency: band.transparency },
          line: { type: 'none' },
        })
      }
    }
    const coverTitleColor = hasHero ? 'FFFFFF' : p.titleText
    const coverMutedColor = hasHero ? 'F0F0F0' : p.muted
    const coverFootColor = hasHero ? 'F0F0F0' : '1A1A1A'

    titleSlide.addShape('rect', {
      x: 0.6, y: 2.0, w: 0.14, h: 3.5,
      fill: { color: p.accent },
      line: { type: 'none' },
    })
    titleSlide.addText(String(spec.title), {
      x: 1.0, y: 2.1, w: 11.7, h: 1.8,
      fontSize: 50, bold: true, color: coverTitleColor,
      align: 'left', valign: 'middle', fontFace: FONT_FACE,
    })
    if (spec.slides.length > 1) {
      titleSlide.addText(`共 ${spec.slides.length} 張投影片`, {
        x: 1.0, y: 4.0, w: 11.7, h: 0.5,
        fontSize: 16, color: coverMutedColor,
        align: 'left', fontFace: FONT_FACE,
      })
    }
    titleSlide.addText('ANILA LM · 自動生成', {
      x: 1.0, y: 4.7, w: 11.7, h: 0.4,
      fontSize: 14, color: coverFootColor, italic: false,
      align: 'left', fontFace: FONT_FACE,
    })
  }

  // Body slides via the layout dispatcher. Sequential await keeps
  // pptxgenjs's internal slide ordering deterministic.
  for (const s of spec.slides) {
    kinds.push(effectiveKind(s))
    await renderSlideByKind(pres, s, theme)
  }

  const buffer = await pres.write({ outputType: 'nodebuffer' })
  return { buffer, coverPrepended, kinds }
}

/**
 * The kind a slide actually renders as — per-kind renderers fall back to
 * `standard` when their payload is missing, and QA must judge the slide by
 * what was drawn, not by what the LLM asked for.
 */
function effectiveKind(s) {
  const kind = String(s.layout_kind || 'standard')
  switch (kind) {
    case 'section_break': return 'section_break'
    case 'stat_callout': return (s.stat && s.stat.value && s.stat.label) ? 'stat_callout' : 'standard'
    case 'quote': return (s.quote && s.quote.text) ? 'quote' : 'standard'
    case 'two_column': return (Array.isArray(s.columns) && s.columns.length >= 2) ? 'two_column' : 'standard'
    case 'icon_rows': return (Array.isArray(s.icon_rows) && s.icon_rows.length > 0) ? 'icon_rows' : 'standard'
    case 'image_focus': return (s.image_data && typeof s.image_data === 'string') ? 'image_focus' : 'standard'
    case 'process': return (Array.isArray(s.steps) && s.steps.filter((st) => st && (st.heading || st.description)).length >= 2) ? 'process' : 'standard'
    case 'table': {
      const t = s.table || {}
      const ok = Array.isArray(t.columns) && t.columns.length >= 2 && Array.isArray(t.rows) && t.rows.filter(Array.isArray).length >= 1
      return ok ? 'table' : 'standard'
    }
    case 'sources': return (Array.isArray(s.sources) && s.sources.some((it) => it && it.label)) ? 'sources' : 'standard'
    default: return 'standard'
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
      return res.status(413).json({ error: `spec.slides exceeds cap of ${MAX_SLIDES}` })
    }

    const { buffer, coverPrepended, kinds } = await renderSpec(spec)

    const jobId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    const outPath = path.join(TMP_ROOT, `${jobId}.pptx`)
    fs.writeFileSync(outPath, buffer)

    res
      .status(200)
      .set({
        'Content-Type':
          'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'X-Pptx-Job-Id': jobId,
        'X-Pptx-Path': outPath,
        // QA alignment: whether slide 0 is a renderer-made cover, and the
        // rendered kind of every slide (cover first when prepended).
        'X-Pptx-Cover-Prepended': coverPrepended ? '1' : '0',
        'X-Pptx-Slide-Kinds': kinds.join(','),
      })
      .send(buffer)
  } catch (err) {
    console.error('[render] error:', err)
    res.status(err && err.status === 413 ? 413 : 500).json({ error: String(err && err.message) || 'render failed' })
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

/**
 * Deterministic geometric QA — Studio Fix 6.
 *
 * Parses each slide's XML and analyses shape coordinates so the pipeline
 * can flag layout problems vision QA tends to miss (e.g. a 3-inch band
 * of dead whitespace, or a shape extending past the slide edge). Cheap
 * compared to the gemma4 vision pass, and catches the symptom-free
 * issues that vision LLMs blandly approve.
 *
 * EMU primer: 914400 EMU = 1 inch. A 16:9 slide is 13.333" x 7.5", i.e.
 * 12192000 x 6858000 EMU. We work in inches because the thresholds
 * (overflow, density-per-sq-inch) are expressed in inches.
 */
const EMU_PER_INCH = 914400
const SLIDE_W_INCH = 13.333
const SLIDE_H_INCH = 7.5
const SLIDE_AREA_INCH = SLIDE_W_INCH * SLIDE_H_INCH
const WHITESPACE_RATIO_WARN = 0.55
const WHITESPACE_RATIO_CRIT = 0.7
const TEXT_DENSITY_WARN = 50
const OVERLAP_MIN_SQ_INCH = 0.1
const GRID_COLS = 6
const GRID_ROWS = 4
const MAX_EMPTY_CELLS_WARN = 8 // 8/24 = 33% contiguous void
const MAX_EMPTY_CELLS_CRIT = 12 // 12/24 = 50% contiguous void
const BODY_Y_START_INCH = 0.84

function extractShapesFromSlideXml(xml) {
  const shapes = []
  const spRegex = /<p:sp\b[\s\S]*?<\/p:sp>/g
  let m
  while ((m = spRegex.exec(xml)) !== null) {
    const block = m[0]
    const off = block.match(/<a:off\s+x="(-?\d+)"\s+y="(-?\d+)"\s*\/>/)
    const ext = block.match(/<a:ext\s+cx="(\d+)"\s+cy="(\d+)"\s*\/>/)
    if (!off || !ext) continue
    const xInch = Number(off[1]) / EMU_PER_INCH
    const yInch = Number(off[2]) / EMU_PER_INCH
    const wInch = Number(ext[1]) / EMU_PER_INCH
    const hInch = Number(ext[2]) / EMU_PER_INCH
    let textLen = 0
    const textRegex = /<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/g
    let t
    while ((t = textRegex.exec(block)) !== null) {
      textLen += t[1].length
    }
    const isSlideNum = /type="slidenum"/.test(block)
    shapes.push({ x: xInch, y: yInch, w: wInch, h: hInch, textLen, isSlideNum })
  }
  // Tables / charts live in graphicFrames, not p:sp — count their extent
  // as covered area or every table slide reads as empty.
  const frameRegex = /<p:graphicFrame\b[\s\S]*?<\/p:graphicFrame>/g
  while ((m = frameRegex.exec(xml)) !== null) {
    const block = m[0]
    const off = block.match(/<a:off\s+x="(-?\d+)"\s+y="(-?\d+)"\s*\/>/)
    const ext = block.match(/<a:ext\s+cx="(\d+)"\s+cy="(\d+)"\s*\/>/)
    if (!off || !ext) continue
    let textLen = 0
    const textRegex = /<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/g
    let t
    while ((t = textRegex.exec(block)) !== null) textLen += t[1].length
    shapes.push({
      x: Number(off[1]) / EMU_PER_INCH, y: Number(off[2]) / EMU_PER_INCH,
      w: Number(ext[1]) / EMU_PER_INCH, h: Number(ext[2]) / EMU_PER_INCH,
      textLen, isFrame: true,
    })
  }
  const picRegex = /<p:pic\b[\s\S]*?<\/p:pic>/g
  while ((m = picRegex.exec(xml)) !== null) {
    const block = m[0]
    const off = block.match(/<a:off\s+x="(-?\d+)"\s+y="(-?\d+)"\s*\/>/)
    const ext = block.match(/<a:ext\s+cx="(\d+)"\s+cy="(\d+)"\s*\/>/)
    if (!off || !ext) continue
    shapes.push({
      x: Number(off[1]) / EMU_PER_INCH,
      y: Number(off[2]) / EMU_PER_INCH,
      w: Number(ext[1]) / EMU_PER_INCH,
      h: Number(ext[2]) / EMU_PER_INCH,
      textLen: 0,
    })
  }
  return shapes
}

/**
 * Detect localised empty regions that the global coveredArea metric misses.
 *
 * Splits the body area (below the 0.84-inch master header band) into a
 * GRID_COLS × GRID_ROWS grid. A cell is "covered" if ANY shape overlaps it.
 * Find the largest 4-connected empty region; large contiguous voids
 * (> 8 cells = ~33% of the body) are the visual symptom users complain
 * about even when total whitespace ratio is fine.
 */
function findLargestEmptyRegion(shapes) {
  const bodyH = SLIDE_H_INCH - BODY_Y_START_INCH
  const cellW = SLIDE_W_INCH / GRID_COLS
  const cellH = bodyH / GRID_ROWS
  const grid = Array.from({ length: GRID_ROWS }, () => Array(GRID_COLS).fill(0))
  for (const s of shapes) {
    if (s.y + s.h <= BODY_Y_START_INCH) continue // skip header band
    const yEff = Math.max(s.y, BODY_Y_START_INCH)
    const c1 = Math.max(0, Math.floor(s.x / cellW))
    const c2 = Math.min(GRID_COLS - 1, Math.floor((s.x + s.w - 0.01) / cellW))
    const r1 = Math.max(0, Math.floor((yEff - BODY_Y_START_INCH) / cellH))
    const r2 = Math.min(
      GRID_ROWS - 1,
      Math.floor((s.y + s.h - 0.01 - BODY_Y_START_INCH) / cellH),
    )
    for (let r = r1; r <= r2; r++) {
      for (let c = c1; c <= c2; c++) {
        grid[r][c] = 1
      }
    }
  }
  // 4-connected flood fill to find largest empty region
  const visited = Array.from({ length: GRID_ROWS }, () =>
    Array(GRID_COLS).fill(false),
  )
  let maxRegion = 0
  for (let r = 0; r < GRID_ROWS; r++) {
    for (let c = 0; c < GRID_COLS; c++) {
      if (grid[r][c] === 0 && !visited[r][c]) {
        const stack = [[r, c]]
        let size = 0
        while (stack.length) {
          const [rr, cc] = stack.pop()
          if (rr < 0 || rr >= GRID_ROWS || cc < 0 || cc >= GRID_COLS) continue
          if (visited[rr][cc] || grid[rr][cc] === 1) continue
          visited[rr][cc] = true
          size++
          stack.push([rr + 1, cc], [rr - 1, cc], [rr, cc + 1], [rr, cc - 1])
        }
        if (size > maxRegion) maxRegion = size
      }
    }
  }
  return maxRegion
}

// Kinds whose body is expected to fill the slide. Everything else (cover,
// section_break, stat_callout, quote) is sparse BY DESIGN and must not be
// judged on whitespace — a big number on an empty page is the point.
const WHITESPACE_JUDGED_KINDS = new Set(['standard', 'two_column', 'icon_rows', 'image_focus', 'process', 'table'])

// One shape drawn inside another (an icon glyph inside its circle, a scrim
// over a hero image) is containment, not an overlap defect.
function isContainment(a, b, ix1, iy1, ix2, iy2) {
  const inter = (ix2 - ix1) * (iy2 - iy1)
  const smaller = Math.min(a.w * a.h, b.w * b.h)
  return smaller > 0 && inter >= smaller * 0.9
}

function analyseSlide(shapes, kind) {
  const defects = []
  const judgeWhitespace = kind === undefined || WHITESPACE_JUDGED_KINDS.has(String(kind))
  const largestEmptyCells = judgeWhitespace ? findLargestEmptyRegion(shapes) : 0
  if (largestEmptyCells >= MAX_EMPTY_CELLS_CRIT) {
    defects.push({
      severity: 'critical',
      kind: 'local_emptiness',
      detail: `largest contiguous empty region = ${largestEmptyCells}/${GRID_COLS * GRID_ROWS} cells`,
    })
  } else if (largestEmptyCells >= MAX_EMPTY_CELLS_WARN) {
    defects.push({
      severity: 'warning',
      kind: 'local_emptiness',
      detail: `largest contiguous empty region = ${largestEmptyCells}/${GRID_COLS * GRID_ROWS} cells`,
    })
  }
  let coveredArea = 0
  for (const s of shapes) {
    const x1 = Math.max(0, s.x)
    const y1 = Math.max(0, s.y)
    const x2 = Math.min(SLIDE_W_INCH, s.x + s.w)
    const y2 = Math.min(SLIDE_H_INCH, s.y + s.h)
    if (x2 > x1 && y2 > y1) {
      coveredArea += (x2 - x1) * (y2 - y1)
    }
  }
  const clampedCover = Math.min(coveredArea, SLIDE_AREA_INCH)
  const wsRatio = judgeWhitespace ? (SLIDE_AREA_INCH - clampedCover) / SLIDE_AREA_INCH : 0
  if (wsRatio > WHITESPACE_RATIO_CRIT) {
    defects.push({
      severity: 'critical',
      kind: 'whitespace',
      detail: `ratio=${wsRatio.toFixed(2)} (>${WHITESPACE_RATIO_CRIT})`,
    })
  } else if (wsRatio > WHITESPACE_RATIO_WARN) {
    defects.push({
      severity: 'warning',
      kind: 'whitespace',
      detail: `ratio=${wsRatio.toFixed(2)} (>${WHITESPACE_RATIO_WARN})`,
    })
  }
  for (const s of shapes) {
    if (s.isSlideNum) continue // master placeholder, not content
    const right = s.x + s.w
    const bottom = s.y + s.h
    if (right > SLIDE_W_INCH + 0.01) {
      defects.push({
        severity: 'critical',
        kind: 'overflow',
        detail: `shape extends to ${right.toFixed(2)} inch (>${SLIDE_W_INCH})`,
      })
    } else if (bottom > SLIDE_H_INCH + 0.01) {
      defects.push({
        severity: 'critical',
        kind: 'overflow',
        detail: `shape extends to ${bottom.toFixed(2)} inch (>${SLIDE_H_INCH})`,
      })
    }
  }
  for (let i = 0; i < shapes.length; i++) {
    for (let j = i + 1; j < shapes.length; j++) {
      const a = shapes[i]
      const b = shapes[j]
      const ix1 = Math.max(a.x, b.x)
      const iy1 = Math.max(a.y, b.y)
      const ix2 = Math.min(a.x + a.w, b.x + b.w)
      const iy2 = Math.min(a.y + a.h, b.y + b.h)
      if (a.isSlideNum || b.isSlideNum) continue
      if (ix2 > ix1 && iy2 > iy1) {
        const area = (ix2 - ix1) * (iy2 - iy1)
        if (area > OVERLAP_MIN_SQ_INCH && !isContainment(a, b, ix1, iy1, ix2, iy2)) {
          defects.push({
            severity: 'warning',
            kind: 'overlap',
            detail: `shapes ${i} and ${j} overlap ${area.toFixed(2)} sq inch`,
          })
        }
      }
    }
  }
  for (const s of shapes) {
    if (s.textLen === 0 || s.isFrame) continue
    const area = Math.max(0.01, s.w * s.h)
    const density = s.textLen / area
    if (density > TEXT_DENSITY_WARN) {
      defects.push({
        severity: 'warning',
        kind: 'text_density',
        detail: `density=${density.toFixed(1)} chars/sq inch (>${TEXT_DENSITY_WARN})`,
      })
    }
  }
  return defects
}

app.post('/qa-geometric', async (req, res) => {
  try {
    const { pptxBase64, kinds } = req.body || {}
    if (typeof pptxBase64 !== 'string' || pptxBase64.length === 0) {
      return res.status(400).json({ error: 'pptxBase64 required' })
    }
    // Optional: rendered kind per slide (same order as the deck, cover first
    // when the renderer prepended one) so sparse-by-design pages aren't
    // judged on whitespace. Missing/short → legacy behaviour (judge all).
    const kindList = Array.isArray(kinds) ? kinds.map(String) : []
    let zip
    try {
      zip = await JSZip.loadAsync(Buffer.from(pptxBase64, 'base64'))
    } catch (e) {
      return res
        .status(400)
        .json({ error: `invalid pptx (zip load failed): ${e.message}` })
    }
    const slideFiles = Object.keys(zip.files)
      .filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name))
      .sort((a, b) => {
        const ai = Number(a.match(/slide(\d+)\.xml$/)[1])
        const bi = Number(b.match(/slide(\d+)\.xml$/)[1])
        return ai - bi
      })
    const defects = []
    for (let i = 0; i < slideFiles.length; i++) {
      const name = slideFiles[i]
      let xml
      try {
        xml = await zip.files[name].async('string')
      } catch (e) {
        console.warn(`[qa-geometric] failed to read ${name}: ${e.message}`)
        continue
      }
      const shapes = extractShapesFromSlideXml(xml)
      const slideDefects = analyseSlide(shapes, kindList[i])
      for (const d of slideDefects) {
        defects.push({ slide_index: i, ...d })
      }
    }
    res.json({ defects })
  } catch (err) {
    console.error('[qa-geometric] error:', err)
    res
      .status(500)
      .json({ error: String((err && err.message) || 'qa-geometric failed') })
  }
})

if (require.main === module) {
  app.listen(PORT, '0.0.0.0', () => {
    console.log(`[pptx-renderer] listening on :${PORT}`)
  })
}

// Exported for tests (node tests/*.js) — nothing listens on require.
module.exports = {
  app,
  renderSpec,
  effectiveKind,
  analyseSlide,
  extractShapesFromSlideXml,
  findLargestEmptyRegion,
  parseBulletHierarchy,
  pickSectionTitleFont,
  THEMES,
  WHITESPACE_JUDGED_KINDS,
}
