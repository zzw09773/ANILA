#!/usr/bin/env node
/**
 * Smoke test: renderIconPng() exercises the sharp SVG->PNG native path
 * (icons.js -> sharp(Buffer.from(svg)).png().toBuffer()).
 *
 * Exists to catch a broken/incompatible sharp native binary or subtle
 * SVG-rasterization regression across sharp version bumps (e.g. the
 * 0.33.5 -> 0.35.3 CVE remediation), since none of the other contract
 * tests drive the `icon_rows` layout that calls into this path.
 *
 * Run: node tests/test_icon_png_smoke.js
 */
'use strict'

const { renderIconPng } = require('../icons.js')

function assert(cond, msg) {
  if (!cond) {
    console.error('FAIL:', msg)
    process.exit(1)
  }
  console.log('ok:', msg)
}

renderIconPng('metrics', { color: '#1E2761', size: 256 })
  .then((buf) => {
    assert(Buffer.isBuffer(buf), 'renderIconPng resolved a Buffer')
    assert(buf.length >= 100, `PNG buffer size sane: got ${buf.length} bytes, expected >= 100`)
    assert(
      buf.slice(0, 8).toString('hex') === '89504e470d0a1a0a',
      `full 8-byte PNG signature present: got ${buf.slice(0, 8).toString('hex')}`,
    )
    console.log('sharp OK', buf.length)
  })
  .catch((err) => {
    console.error('FAIL:', err && err.stack ? err.stack : err)
    process.exit(1)
  })
