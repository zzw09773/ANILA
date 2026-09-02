#!/usr/bin/env node
/** Content slides print Slide.source_line as a small footer; covers / section breaks never do. */
'use strict'
const JSZip = require('jszip')
const { renderSpec } = require('../server.js')
function fail(msg) { console.error(`FAIL: ${msg}`); process.exit(1) }
async function main() {
  const out = await renderSpec({ title: 't', theme: 'corporate_navy', slides: [
    { title: '封面', layout_kind: 'section_break', bullets: ['x'], source_line: '資料來源：不該出現.pdf' },
    { title: '內容', layout_kind: 'standard', bullets: ['a', 'b'], source_line: '資料來源：軍人懲罰法.pdf' },
    { title: '表', layout_kind: 'table', bullets: ['x'], table: { columns: ['a', 'b'], rows: [['1', '2']] }, source_line: '資料來源：施行細則.pdf' },
  ] })
  const zip = await JSZip.loadAsync(out.buffer)
  const x1 = await zip.files['ppt/slides/slide1.xml'].async('string')
  const x2 = await zip.files['ppt/slides/slide2.xml'].async('string')
  const x3 = await zip.files['ppt/slides/slide3.xml'].async('string')
  if (x1.includes('不該出現')) fail('section_break printed a source footer')
  if (!x2.includes('資料來源：軍人懲罰法.pdf')) fail('standard slide has no source footer')
  if (!x3.includes('資料來源：施行細則.pdf')) fail('table slide has no source footer')
  console.log('PASS: source footers on content slides only')
}
main().catch((e) => fail(e.stack || String(e)))
