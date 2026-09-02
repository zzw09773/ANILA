#!/usr/bin/env node
/** two_column and image_focus bullets must strip hierarchy markers like standard does
 *  (2026-09-02 live deck showed "● ● 共同專案"). Run: node tests/test_two_column_markers.js */
'use strict'
const JSZip = require('jszip')
const { renderSpec } = require('../server.js')
function fail(msg) { console.error(`FAIL: ${msg}`); process.exit(1) }
async function main() {
  const out = await renderSpec({ title: 't', theme: 'corporate_navy', slides: [
    { title: '封面', layout_kind: 'section_break', bullets: ['x'] },
    { title: '對照', layout_kind: 'two_column', bullets: ['x'], columns: [
      { heading: '甲', bullets: ['● 共同項目：撤職', '◦ 子點'] }, { heading: '乙', bullets: ['● 紀律處分', '● 罰站'] } ] },
  ] })
  const zip = await JSZip.loadAsync(out.buffer)
  const xml = await zip.files['ppt/slides/slide2.xml'].async('string')
  const texts = [...xml.matchAll(/<a:t[^>]*>([^<]*)<\/a:t>/g)].map((m) => m[1])
  const leaked = texts.filter((t) => /^[●◦▪]/.test(t))
  if (leaked.length) fail(`marker leaked into visible text: ${JSON.stringify(leaked)}`)
  if (!/lvl="1"/.test(xml)) fail('sub-point did not get indent level 1')
  console.log('PASS: two_column strips markers and indents sub-points')
}
main().catch((e) => fail(e.stack || String(e)))
