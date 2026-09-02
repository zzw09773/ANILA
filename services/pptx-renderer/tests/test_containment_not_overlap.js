#!/usr/bin/env node
/** A glyph drawn inside its own icon circle is containment, not an overlap defect;
 *  cover / section_break / stat_callout / quote are sparse by design and must not be
 *  judged on whitespace. Run: node tests/test_containment_not_overlap.js */
'use strict'
const { analyseSlide } = require('../server.js')
function fail(msg) { console.error(`FAIL: ${msg}`); process.exit(1) }
const circle = { x: 0.6, y: 1.2, w: 0.8, h: 0.8, textLen: 0 }
const glyph = { x: 0.74, y: 1.34, w: 0.52, h: 0.52, textLen: 0 }
const heading = { x: 1.7, y: 1.2, w: 10, h: 0.5, textLen: 8 }
const body = { x: 1.7, y: 1.7, w: 10, h: 4.5, textLen: 200 }
let d = analyseSlide([circle, glyph, heading, body], 'icon_rows')
if (d.some((x) => x.kind === 'overlap')) fail(`containment reported as overlap: ${JSON.stringify(d)}`)
// sparse-by-design kinds: a cover with a title strip and two short texts
const sparse = [{ x: 0.6, y: 2.0, w: 0.14, h: 3.5, textLen: 0 }, { x: 1.0, y: 2.1, w: 11.7, h: 1.8, textLen: 10 }, { x: 1.0, y: 4.7, w: 11.7, h: 0.4, textLen: 12 }]
for (const kind of ['cover', 'section_break', 'stat_callout', 'quote']) {
  d = analyseSlide(sparse, kind)
  if (d.some((x) => x.kind === 'whitespace' || x.kind === 'local_emptiness')) fail(`${kind} judged on whitespace: ${JSON.stringify(d)}`)
}
// a standard slide with the same sparse shapes IS flagged (rule still alive)
d = analyseSlide(sparse, 'standard')
if (!d.some((x) => x.kind === 'whitespace' || x.kind === 'local_emptiness')) fail('standard slide sparse but not flagged')
// a real overlap (two text boxes crossing) is still reported
d = analyseSlide([heading, { x: 1.7, y: 1.4, w: 10, h: 1, textLen: 30 }], 'standard')
if (!d.some((x) => x.kind === 'overlap')) fail('real overlap not reported')
console.log('PASS: containment ignored, sparse kinds exempt, real overlap kept')
