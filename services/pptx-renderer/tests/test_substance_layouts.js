#!/usr/bin/env node
/** 言之有物、不密密麻麻：key_message band, figure (timeline / org / svg), agenda page, official theme. */
'use strict'
const JSZip = require('jszip')
const { renderSpec, effectiveKind, analyseSlide, extractShapesFromSlideXml, THEMES } = require('../server.js')
function fail(msg) { console.error(`FAIL: ${msg}`); process.exit(1) }

const spec = { title: '軍人懲罰法實務教學', theme: 'official', slides: [
  { title: '軍人懲罰法實務教學', layout_kind: 'section_break', bullets: ['給新進行政同仁'] },
  { title: '目錄', layout_kind: 'agenda', bullets: ['x'], agenda: ['懲罰種類', '權責劃分', '權益救濟'] },
  { title: '重點頁', layout_kind: 'standard', bullets: ['第一點', '第二點', '第三點'], key_message: '申訴要在三十日內提出，逾期不受理。' },
  { title: '救濟時程', layout_kind: 'figure', bullets: ['x'], figure: { kind: 'timeline', items: [
    { label: '處分送達', note: '第 0 日' }, { label: '申訴', note: '30 日內' }, { label: '再申訴', note: '20 日內' }, { label: '行政訴訟', note: '2 個月內' } ] } },
  { title: '權責機關', layout_kind: 'figure', bullets: ['x'], figure: { kind: 'org', items: [
    { label: '總統' }, { label: '國防部', parent: '總統' }, { label: '服役機關', parent: '國防部' }, { label: '權責長官', parent: '國防部' } ] } },
  { title: '系統架構', layout_kind: 'figure', bullets: ['x'], figure: { kind: 'svg', svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 675"><rect x="100" y="200" width="400" height="200" rx="20" fill="#1E2761"/><text x="300" y="315" font-family="Noto Sans CJK TC" font-size="48" fill="#fff" text-anchor="middle">申訴管轄機關</text><circle cx="900" cy="300" r="120" fill="#F4B740"/></svg>' } },
  { title: '權責劃分', layout_kind: 'section_break', bullets: ['第二章'] },
]}

async function main() {
  if (!THEMES.official) fail('official theme missing')
  if (effectiveKind(spec.slides[1]) !== 'agenda') fail('agenda kind')
  if (effectiveKind(spec.slides[3]) !== 'figure') fail('timeline figure kind')
  if (effectiveKind({ layout_kind: 'figure', bullets: ['a'] }) !== 'standard') fail('figure without payload must fall back')
  const out = await renderSpec(spec)
  const zip = await JSZip.loadAsync(out.buffer)
  const xml = async (i) => zip.files[`ppt/slides/slide${i}.xml`].async('string')
  const agenda = await xml(2)
  if (!agenda.includes('權益救濟') || !agenda.includes('<a:t>03</a:t>')) fail('agenda page lacks numbered sections')
  const km = await xml(3)
  if (!km.includes('申訴要在三十日內提出')) fail('key_message band missing')
  for (const [i, name] of [[4, 'timeline'], [5, 'org'], [6, 'svg']]) {
    const x = await xml(i)
    if (!x.includes('<p:pic')) fail(`${name} figure did not render a picture`)
  }
  const section = await xml(7)
  if (!section.includes('第二章') || !section.includes('權責劃分')) fail('official section page')
  for (let i = 1; i <= 7; i++) {
    const shapes = extractShapesFromSlideXml(await xml(i))
    const crit = analyseSlide(shapes, out.kinds[i - 1]).filter((d) => d.severity === 'critical')
    if (crit.length) fail(`slide ${i} (${out.kinds[i - 1]}) critical: ${JSON.stringify(crit)}`)
  }
  console.log('PASS: official theme, agenda, key_message band, timeline/org/svg figures, zero critical geometry')
}
main().catch((e) => fail(e.stack || String(e)))
