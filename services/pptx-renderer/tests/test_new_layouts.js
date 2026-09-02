#!/usr/bin/env node
/** Batch 3 layouts a regulations deck actually needs: process (numbered steps),
 *  table (native, editable), sources (closing list of cited documents). */
'use strict'
const JSZip = require('jszip')
const { renderSpec, effectiveKind, analyseSlide, extractShapesFromSlideXml } = require('../server.js')
function fail(msg) { console.error(`FAIL: ${msg}`); process.exit(1) }

const spec = { title: 't', theme: 'corporate_navy', slides: [
  { title: '封面', layout_kind: 'section_break', bullets: ['x'] },
  { title: '救濟程序', layout_kind: 'process', bullets: ['x'], steps: [
    { heading: '申訴', description: '向管轄機關提出' }, { heading: '再申訴', description: '權保會審議' },
    { heading: '行政訴訟', description: '勤務法庭審理' }, { heading: '判決', description: '確定' } ] },
  { title: '懲罰種類對照', layout_kind: 'table', bullets: ['x'], table: {
    columns: ['身分', '共同懲罰', '特有懲罰'],
    rows: [['軍官', '撤職、降階、記過', '—'], ['士官', '記過、申誡', '悔過、檢束、罰勤'], ['士兵', '記過、申誡', '禁足、罰站']] } },
  { title: '資料來源', layout_kind: 'sources', bullets: ['x'], sources: [
    { label: '軍人懲罰法.pdf', note: '第 3、5、12 條' }, { label: '陸海空軍懲罰法施行細則.pdf', note: '第 8 條' } ] },
]}

async function main() {
  if (effectiveKind(spec.slides[1]) !== 'process') fail('process kind not recognised')
  if (effectiveKind(spec.slides[2]) !== 'table') fail('table kind not recognised')
  if (effectiveKind(spec.slides[3]) !== 'sources') fail('sources kind not recognised')
  if (effectiveKind({ layout_kind: 'process', bullets: ['a'] }) !== 'standard') fail('process without steps must fall back')
  const out = await renderSpec(spec)
  const zip = await JSZip.loadAsync(out.buffer)
  const x2 = await zip.files['ppt/slides/slide2.xml'].async('string')
  const x3 = await zip.files['ppt/slides/slide3.xml'].async('string')
  const x4 = await zip.files['ppt/slides/slide4.xml'].async('string')
  for (const n of ['1', '2', '3', '4']) if (!x2.includes(`<a:t>${n}</a:t>`)) fail(`process step number ${n} missing`)
  if (!x2.includes('再申訴')) fail('process heading missing')
  if (!x3.includes('<a:tbl>')) fail('table slide has no native table')
  if (!x3.includes('罰站')) fail('table cell text missing')
  if (!x4.includes('施行細則')) fail('sources entry missing')
  const names = ['ppt/slides/slide1.xml', 'ppt/slides/slide2.xml', 'ppt/slides/slide3.xml', 'ppt/slides/slide4.xml']
  for (let i = 0; i < names.length; i++) {
    const shapes = extractShapesFromSlideXml(await zip.files[names[i]].async('string'))
    const crit = analyseSlide(shapes, out.kinds[i]).filter((d) => d.severity === 'critical')
    if (crit.length) fail(`slide ${i} (${out.kinds[i]}) critical: ${JSON.stringify(crit)}`)
  }
  console.log('PASS: process / table / sources render, native table present, zero critical geometry')
}
main().catch((e) => fail(e.stack || String(e)))
