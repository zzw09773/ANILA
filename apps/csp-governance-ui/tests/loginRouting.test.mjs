import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const clientSource = readFileSync(new URL('../src/api/client.js', import.meta.url), 'utf8')

test('401 recovery does not replace an in-flight login URL with a bare path', () => {
  assert.match(
    clientSource,
    /if\s*\(\s*router\.currentRoute\.value\?\.path\s*!==\s*['"]\/login['"]\s*&&\s*window\.location\.pathname\s*!==\s*['"]\/login['"]\s*\)\s*\{\s*router\.push\(['"]\/login['"]\)/
  )
})
