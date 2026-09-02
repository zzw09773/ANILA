#!/usr/bin/env node
/** Every concept in the prompt whitelist must resolve to a glyph; the 2026-09-02 deck
 *  drew a fallback dot for "process". Also covers the new law/admin domain. */
'use strict'
const { renderIconPng } = require('../icons.js')
function fail(msg) { console.error(`FAIL: ${msg}`); process.exit(1) }
const must = ['process', 'procedure', 'law', 'regulation', 'court', 'appeal', 'penalty', 'record', 'announcement',
  'payroll', 'organization', 'plan', 'goal', 'idea', 'key_point', 'report', 'archive', 'timeline', 'personnel',
  'approval', 'rights', 'identity', 'training', 'security', 'schedule']
async function main() {
  for (const c of must) {
    const png = await renderIconPng(c, { color: '#123456', size: 64 })
    if (!png || !png.length) fail(`concept "${c}" has no icon`)
  }
  console.log(`PASS: ${must.length} concepts resolve to glyphs`)
}
main().catch((e) => fail(e.stack || String(e)))
