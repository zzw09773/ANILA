#!/usr/bin/env node
/**
 * Smoke test: image_data renders on `image_focus` layouts but NOT on
 * `standard` layouts.
 *
 * Why this exists (regression guard for Stage 4): the studio backend only
 * gets a content illustration onto a slide by setting layout_kind="image_focus"
 * (renderImageFocus draws image_data). Earlier we wrongly assumed the renderer
 * showed image_data on any layout; in fact `standard`/`stat_callout`/`quote`/
 * `two_column`/`icon_rows` ignore image_data entirely. If renderImageFocus ever
 * stops drawing image_data — or `standard` starts drawing it — Stage 4's
 * illustration behaviour silently breaks. This catches that.
 *
 * Run against a live renderer (it needs the full PptxGenJS pipeline, so it
 * cannot inline the function under test like the other smoke tests):
 *   node server.js &                 # or use the running container
 *   node tests/test_image_focus_render.js
 *   RENDERER_URL=http://pptx-renderer:7100 node tests/test_image_focus_render.js
 */
'use strict'

const JSZip = require('jszip')

const RENDERER_URL = process.env.RENDERER_URL || 'http://localhost:7100'

// 1x1 PNG — content is irrelevant, we only assert it gets embedded (or not).
const DATA_URL =
  'data:image/png;base64,' +
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

const MARKER_IMAGE_FOCUS = 'SMOKE_IMAGE_FOCUS'
const MARKER_STANDARD = 'SMOKE_STANDARD_CONTROL'

const spec = {
  title: 'image_focus render smoke',
  theme: 'corporate_navy',
  slides: [
    { title: 'Cover', bullets: ['x'], layout_kind: 'section_break' },
    {
      title: MARKER_IMAGE_FOCUS,
      bullets: ['point one', 'point two'],
      layout_kind: 'image_focus',
      image_data: DATA_URL,
    },
    {
      title: MARKER_STANDARD,
      bullets: ['a', 'b'],
      layout_kind: 'standard',
      image_data: DATA_URL,
    },
  ],
}

function fail(msg) {
  console.error(`FAIL: ${msg}`)
  process.exit(1)
}

async function main() {
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

  // Map each slideN.xml → (title text, r:embed count).
  let focusEmbeds = null
  let standardEmbeds = null
  for (const name of Object.keys(zip.files)) {
    if (!/^ppt\/slides\/slide\d+\.xml$/.test(name)) continue
    const xml = await zip.files[name].async('string')
    const embeds = (xml.match(/r:embed/g) || []).length
    if (xml.includes(MARKER_IMAGE_FOCUS)) focusEmbeds = embeds
    if (xml.includes(MARKER_STANDARD)) standardEmbeds = embeds
  }

  if (focusEmbeds === null) fail('image_focus slide not found in output')
  if (standardEmbeds === null) fail('standard control slide not found in output')
  if (focusEmbeds < 1) {
    fail(`image_focus slide has no embedded image (r:embed=${focusEmbeds}); renderImageFocus must draw image_data`)
  }
  if (standardEmbeds !== 0) {
    fail(`standard slide unexpectedly embedded an image (r:embed=${standardEmbeds}); standard must ignore image_data`)
  }

  console.log(
    `PASS: image_focus r:embed=${focusEmbeds} (shows image), ` +
      `standard r:embed=${standardEmbeds} (ignores image_data)`,
  )
}

main()
