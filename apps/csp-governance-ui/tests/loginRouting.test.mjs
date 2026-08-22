import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const clientSource = readFileSync(new URL('../src/api/client.js', import.meta.url), 'utf8')

test('401 recovery clears auth and hard-replaces with configured login origin', () => {
  assert.match(
    clientSource,
    /await authStore\.logout\(\)/
  )
  assert.match(clientSource, /window\.location\.replace\(loginHref\(\)\)/)
  assert.doesNotMatch(clientSource, /router\.push\(['"]\/login['"]\)/)
})
