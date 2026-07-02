#!/usr/bin/env node
/**
 * Smoke test: cover hero P1 guard.
 *
 * The auto-generated title slide promotes spec.slides[0].image_data to a
 * full-bleed background ONLY when image_gen_meta.use_case === 'cover_hero'.
 * Other producers (curated image_ref, Graphviz diagrams) also write image_data
 * onto slide 0 but MUST NOT hijack the title-slide background — that would
 * disfigure the deck cover with an unrelated chart or photo.
 *
 * Two render calls share the same renderer:
 *   - Case A (use_case === 'cover_hero'): auto title slide shows the image.
 *   - Case B (image_gen_meta absent):     auto title slide stays text-only.
 *
 * Run against a live renderer:
 *   node server.js &                 # or use the running container
 *   node tests/test_cover_hero_guard.js
 *   RENDERER_URL=http://pptx-renderer:7100 node tests/test_cover_hero_guard.js
 */
'use strict'

const JSZip = require('jszip')

const RENDERER_URL = process.env.RENDERER_URL || 'http://localhost:7100'

// 1x1 PNG — content irrelevant, we only assert it gets embedded (or not).
const DATA_URL =
  'data:image/png;base64,' +
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

const TITLE_A = 'COVER_HERO_GUARD_A'
const TITLE_B = 'COVER_HERO_GUARD_B'

function fail(msg) {
  console.error(`FAIL: ${msg}`)
  process.exit(1)
}

function buildSpec(deckTitle, slideZero) {
  // Body slides use layout_kind='standard' which ignores image_data on the
  // slide itself, so any r:embed found in the title slide can only come from
  // the cover-hero promotion path.
  return {
    title: deckTitle,
    theme: 'corporate_navy',
    slides: [
      slideZero,
      { title: 'filler', bullets: ['x'], layout_kind: 'standard' },
    ],
  }
}

async function renderAndCountTitleEmbeds(spec) {
  let res
  try {
    res = await fetch(`${RENDERER_URL}/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ spec }),
    })
  } catch (e) {
    fail(`could not reach renderer at ${RENDERER_URL}: ${e.message}`)
  }
  if (!res.ok) fail(`/render returned HTTP ${res.status}`)
  const pptx = Buffer.from(await res.arrayBuffer())
  const zip = await JSZip.loadAsync(pptx)
  // The auto-generated title slide is the only one carrying spec.title text;
  // body slides use their own s.title.
  for (const name of Object.keys(zip.files).sort()) {
    if (!/^ppt\/slides\/slide\d+\.xml$/.test(name)) continue
    const xml = await zip.files[name].async('string')
    if (xml.includes(spec.title)) {
      return (xml.match(/r:embed/g) || []).length
    }
  }
  fail(`title slide carrying "${spec.title}" not found in render output`)
}

async function main() {
  // Case A — image_gen_meta.use_case === 'cover_hero' → promoted.
  const specA = buildSpec(TITLE_A, {
    title: 'spec0A',
    bullets: ['a'],
    layout_kind: 'standard',
    image_data: DATA_URL,
    image_gen_meta: { use_case: 'cover_hero' },
  })
  const titleEmbedsA = await renderAndCountTitleEmbeds(specA)
  if (titleEmbedsA < 1) {
    fail(
      `Case A: use_case='cover_hero' but title slide has no image ` +
        `(r:embed=${titleEmbedsA}); the hero promotion path is broken`,
    )
  }

  // Case B — image_data present but image_gen_meta absent (mimics image_ref
  // or Graphviz on slide 0). Guard must reject.
  const specB = buildSpec(TITLE_B, {
    title: 'spec0B',
    bullets: ['a'],
    layout_kind: 'standard',
    image_data: DATA_URL,
    // image_gen_meta intentionally omitted
  })
  const titleEmbedsB = await renderAndCountTitleEmbeds(specB)
  if (titleEmbedsB !== 0) {
    fail(
      `Case B: image_gen_meta absent but title slide embedded an image ` +
        `(r:embed=${titleEmbedsB}); P1 guard regression — a non-hero image_data ` +
        `is hijacking the title slide background`,
    )
  }

  console.log(
    `PASS: cover hero guard — title r:embed=${titleEmbedsA} with use_case='cover_hero', ` +
      `r:embed=${titleEmbedsB} without image_gen_meta`,
  )
}

main()
