#!/usr/bin/env node
/**
 * Golden geometry test — renders a representative deck through the REAL
 * renderer code (no HTTP) and runs the REAL geometric QA on the result.
 *
 * Guards the class of bug found on 2026-09-02: the master's slide-number box
 * had no width, so pptxgenjs gave it 0.875" at x=12.5 → 13.375" > 13.333" →
 * every content slide was "critical overflow", vision QA was skipped for
 * every slide, and the fix pass ran on every job for nothing.
 *
 * Run: node tests/test_geometry_golden.js
 */
'use strict'
const JSZip = require('jszip')
const { renderSpec, analyseSlide, extractShapesFromSlideXml } = require('../server.js')

const spec = {
  title: '幾何黃金測試',
  theme: 'corporate_navy',
  slides: [
    // no section_break first → renderer prepends a cover
    { title: '一般內容頁', layout_kind: 'standard', bullets: ['第一點', '第二點', '第三點', '第四點'] },
    { title: '對照', layout_kind: 'two_column', bullets: ['x'], columns: [
      { heading: '甲', bullets: ['甲一', '甲二'] }, { heading: '乙', bullets: ['乙一', '乙二'] } ] },
    { title: '三件事', layout_kind: 'icon_rows', bullets: ['x'], icon_rows: [
      { concept: 'security', heading: '安全', description: '說明一' },
      { concept: 'schedule', heading: '時效', description: '說明二' },
      { concept: 'document', heading: '文件', description: '說明三' } ] },
    { title: '關鍵數字', layout_kind: 'stat_callout', bullets: ['所以要注意'], stat: { value: '3 日', label: '處理時限', supporting: '申訴無理由時三日內加具意見移送，樣本為法條原文。' } },
    { title: '章節', layout_kind: 'section_break', bullets: ['副標'] },
    { title: '引言', layout_kind: 'quote', bullets: ['x'], quote: { text: '軍人非經彈劾不受懲戒。', attribution: '軍人懲罰法' } },
  ],
}

function fail(msg) { console.error(`FAIL: ${msg}`); process.exit(1) }

async function main() {
  const out = await renderSpec(spec)
  if (!out || !out.buffer) fail('renderSpec did not return a buffer')
  if (out.coverPrepended !== true) fail(`expected coverPrepended=true, got ${out.coverPrepended}`)
  const expectedKinds = ['cover', 'standard', 'two_column', 'icon_rows', 'stat_callout', 'section_break', 'quote']
  if (JSON.stringify(out.kinds) !== JSON.stringify(expectedKinds)) fail(`kinds mismatch: ${JSON.stringify(out.kinds)}`)

  const zip = await JSZip.loadAsync(out.buffer)
  const names = Object.keys(zip.files).filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n))
    .sort((a, b) => Number(a.match(/slide(\d+)/)[1]) - Number(b.match(/slide(\d+)/)[1]))
  if (names.length !== expectedKinds.length) fail(`rendered ${names.length} slides, expected ${expectedKinds.length}`)

  const critical = []
  for (let i = 0; i < names.length; i++) {
    const xml = await zip.files[names[i]].async('string')
    const shapes = extractShapesFromSlideXml(xml)
    for (const d of analyseSlide(shapes, out.kinds[i])) {
      if (d.severity === 'critical') critical.push(`slide ${i} (${out.kinds[i]}): ${d.kind} ${d.detail}`)
    }
  }
  if (critical.length) fail(`geometric QA reports critical defects on a clean deck:\n  ${critical.join('\n  ')}`)
  console.log(`PASS: ${names.length} slides, zero critical geometric defects, kinds=${out.kinds.join(',')}`)
}
main().catch((e) => fail(e.stack || String(e)))
