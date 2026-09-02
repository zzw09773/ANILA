#!/usr/bin/env node
/** A section break whose bullets[0] merely repeats the title must not print it twice. */
'use strict'
const JSZip = require('jszip')
const { renderSpec } = require('../server.js')
function fail(msg) { console.error(`FAIL: ${msg}`); process.exit(1) }
async function main() {
  const out = await renderSpec({ title: 't', theme: 'corporate_navy', slides: [
    { title: '懲罰種類與權責劃分', layout_kind: 'section_break', bullets: ['● 懲罰種類與權責劃分'] },
    { title: '救濟', layout_kind: 'section_break', bullets: ['申訴、再申訴、行政訴訟'] },
  ] })
  const zip = await JSZip.loadAsync(out.buffer)
  const x1 = await zip.files['ppt/slides/slide1.xml'].async('string')
  const x2 = await zip.files['ppt/slides/slide2.xml'].async('string')
  if ((x1.match(/懲罰種類與權責劃分/g) || []).length !== 1) fail('title repeated as subtitle')
  if (!x2.includes('申訴、再申訴、行政訴訟')) fail('real subtitle dropped')
  console.log('PASS: section break subtitle deduplicated')
}
main().catch((e) => fail(e.stack || String(e)))
