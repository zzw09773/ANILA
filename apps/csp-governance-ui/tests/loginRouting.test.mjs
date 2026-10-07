import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const clientSource = readFileSync(new URL('../src/api/client.js', import.meta.url), 'utf8')
const routerSource = readFileSync(new URL('../src/router/index.js', import.meta.url), 'utf8')

test('未登入守衛用相對路徑，埠號留在瀏覽器', () => {
  assert.match(routerSource, /next\('\/login'\)/)
  assert.doesNotMatch(routerSource, /location\.hostname/)
  assert.doesNotMatch(routerSource, /https?:\/\//)
})

test('401 recovery does not replace an in-flight login URL with a bare path', () => {
  assert.match(
    clientSource,
    /if\s*\(\s*router\.currentRoute\.value\?\.path\s*!==\s*['"]\/login['"]\s*&&\s*window\.location\.pathname\s*!==\s*['"]\/login['"]\s*\)\s*\{\s*router\.push\(['"]\/login['"]\)/
  )
})
