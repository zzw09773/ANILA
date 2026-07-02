#!/usr/bin/env node
/**
 * Standard layout — hierarchical bullet markers.
 *
 * Wire format stays list[str]; level is encoded as a Unicode marker:
 *   ●  level 0  (renders with U+25CF, indentLevel 0)
 *   ◦  level 1  (renders with U+25E6, indentLevel 1)
 *   ▪  level 2  (renders with U+25AA, indentLevel 2)
 *
 * Back-compat: bullets without any marker default to level 0.
 *
 * What's tested:
 *   - the parser strips the leading marker (no double-symbol visual)
 *   - per-bullet ``indentLevel`` reflects the hierarchy
 *   - per-bullet ``bullet.code`` reflects the level's marker
 *   - a bare bullet still renders as level 0
 *
 * Run:
 *   node server.js &                 # or against running container
 *   node tests/test_hierarchy_bullets.js
 *   RENDERER_URL=http://pptx-renderer:7100 node tests/test_hierarchy_bullets.js
 */
'use strict'

const JSZip = require('jszip')

const RENDERER_URL = process.env.RENDERER_URL || 'http://localhost:7100'

const SLIDE_TITLE_MARKER = 'HIER_BULLET_TEST_SLIDE'

const spec = {
  title: 'hierarchical bullet test',
  theme: 'corporate_navy',
  slides: [
    { title: 'Cover', bullets: ['x'], layout_kind: 'section_break' },
    {
      title: SLIDE_TITLE_MARKER,
      layout_kind: 'standard',
      bullets: [
        'Naive RAG: 線性',                  // no marker → level 0
        '● Advanced RAG: hybrid + rerank',  // explicit level 0
        '◦ Reranking',                      // level 1 (sub of advanced)
        '◦ Query rewriting',                // level 1
        '▪ Cross-encoder',                  // level 2 (sub-sub)
        'Modular RAG',                      // back to level 0 (no marker)
      ],
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

  // Find the standard-layout test slide by its title text.
  let slideXml = null
  for (const name of Object.keys(zip.files).sort()) {
    if (!/^ppt\/slides\/slide\d+\.xml$/.test(name)) continue
    const xml = await zip.files[name].async('string')
    if (xml.includes(SLIDE_TITLE_MARKER)) {
      slideXml = xml
      break
    }
  }
  if (slideXml === null) fail(`slide '${SLIDE_TITLE_MARKER}' not found`)

  // PPTX paragraph properties live in a:pPr; indentLevel is the lvl="N"
  // attribute (lvl="0" for the default is sometimes omitted by pptxgenjs,
  // so count its presence rather than asserting on each pPr).
  const lvlMatches = slideXml.match(/lvl="(\d+)"/g) || []
  const levels = lvlMatches.map((s) => Number(s.match(/lvl="(\d+)"/)[1]))

  // We expected at least one indentLevel=1 and one indentLevel=2.
  if (!levels.some((l) => l === 1)) {
    fail(`expected at least one paragraph with lvl="1" (◦ marker) in slide XML; found levels=${JSON.stringify(levels)}`)
  }
  if (!levels.some((l) => l === 2)) {
    fail(`expected at least one paragraph with lvl="2" (▪ marker) in slide XML; found levels=${JSON.stringify(levels)}`)
  }

  // Each bullet should carry an explicit buChar — pptxgenjs emits
  // <a:buChar char="&#x25CF;"/> / "&#x25E6;" / "&#x25AA;" (HTML hex
  // entity, NOT raw Unicode), so we assert on the entity form.
  const expectedEntities = [
    { name: '●', entity: '&#x25CF;' },
    { name: '◦', entity: '&#x25E6;' },
    { name: '▪', entity: '&#x25AA;' },
  ]
  for (const { name, entity } of expectedEntities) {
    if (!slideXml.includes(`char="${entity}"`)) {
      fail(`expected slide XML to embed <a:buChar char="${entity}"/> (level marker ${name}) but did not`)
    }
  }

  // Leading marker must NOT remain in the rendered text run — otherwise
  // we'd see a double symbol ("● ● Advanced RAG"). Look for
  // '<a:t>● Advanced' (marker still in the text body). If found, parser failed.
  if (/<a:t>[●◦▪]\s+/.test(slideXml)) {
    fail('leading marker leaked into <a:t> text; parseBulletHierarchy should have stripped it')
  }

  // The marker-free bullet should be there (parser stripped the leading "● ").
  if (!slideXml.includes('<a:t>Advanced RAG: hybrid + rerank')) {
    fail("expected '<a:t>Advanced RAG: hybrid + rerank' (without leading ● ) in slide XML")
  }
  if (!slideXml.includes('<a:t>Naive RAG: 線性')) {
    fail("expected '<a:t>Naive RAG: 線性' in slide XML (back-compat: no marker → level 0)")
  }

  console.log(
    `PASS: hierarchy levels seen=${JSON.stringify(levels)} ` +
      `(buChar entities &#x25CF; &#x25E6; &#x25AA; all present, leading markers stripped)`,
  )
}

main()
