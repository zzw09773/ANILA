#!/usr/bin/env node
// CVE-2025-71329 / CVE-2025-71330: zero-sized boxes/entries never advance
// the parser offset. image-size <= 2.0.2 has no upstream fix.
'use strict'
const fs = require('fs')
const path = require('path')

const root = path.join(__dirname, '..', 'node_modules', 'image-size')
const replacements = [
  [
    'imageOffset += imageHeader[1];',
    'if (imageHeader[1] <= 0) break; imageOffset += imageHeader[1];',
  ],
  [
    'currentOffset = ispeBox.offset + ispeBox.size;',
    'if (ispeBox.size <= 0) break; currentOffset = ispeBox.offset + ispeBox.size;',
  ],
  [
    'offset = jxlpBox.offset + jxlpBox.size;',
    'if (jxlpBox.size <= 0) break; offset = jxlpBox.offset + jxlpBox.size;',
  ],
]

function walk(dir, acc) {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, ent.name)
    if (ent.isDirectory()) walk(p, acc)
    else if (/\.(cjs|mjs|js)$/.test(ent.name)) acc.push(p)
  }
  return acc
}

const files = walk(path.join(root, 'dist'), [])
let changed = 0
for (const file of files) {
  let text = fs.readFileSync(file, 'utf8')
  let next = text
  for (const [from, to] of replacements) next = next.split(from).join(to)
  if (next !== text) {
    fs.writeFileSync(file, next)
    changed += 1
  }
}

const pkgPath = path.join(root, 'package.json')
const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'))
pkg.version = '2.0.3-anila.1'
fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + '\n')
if (changed < 1) {
  console.error('patch-image-size-cve: no parser files matched')
  process.exit(1)
}
console.log('patched', changed, 'files; version', pkg.version)
