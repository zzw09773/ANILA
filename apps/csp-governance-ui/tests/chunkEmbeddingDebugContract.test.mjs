// Cross-wire contract for embedding-debug.
//
// Producer: services/csp ChunkEmbeddingDebug.
// Consumer: CollectionDetailView.vue vecDebug[c.id].{note,dim,norm}.
// Both sides read the same JSON fixture. Renaming a key in the fixture
// (or in the Vue accessors) must turn this file red — otherwise both
// suites can stay green while disagreeing.
//
// node --test. Does not import Vue. Does not touch files deepseek is
// editing (healthOverview / alertSummary / UsageView).

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const contractPath = join(
  here,
  '../../../services/csp/tests/fixtures/chunk_embedding_debug_contract.json',
)
const viewPath = join(here, '../src/views/CollectionDetailView.vue')

const contract = JSON.parse(readFileSync(contractPath, 'utf8'))
const view = readFileSync(viewPath, 'utf8')

test('shared contract names the four wire keys the producer emits', () => {
  assert.deepEqual(contract.required_keys, ['chunk_id', 'dim', 'norm', 'note'])
  for (const key of contract.required_keys) {
    assert.ok(key in contract.heading, `heading missing ${key}`)
    assert.ok(key in contract.leaf, `leaf missing ${key}`)
  }
  assert.equal(contract.heading.note, 'heading 層級不嵌入向量')
  assert.equal(contract.heading.norm, null)
  assert.equal(contract.leaf.note, null)
})

test('CollectionDetailView reads the same keys the contract names', () => {
  // Accessors, not comments — strip HTML/line comments first.
  const live = view
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
  assert.match(live, /vecDebug\[c\.id\]\?\.note/)
  assert.match(live, /vecDebug\[c\.id\]\.note/)
  assert.match(live, /vecDebug\[c\.id\]\.dim/)
  assert.match(live, /vecDebug\[c\.id\]\.norm/)
  for (const key of contract.required_keys) {
    if (key === 'chunk_id') continue // route param, not rendered
    assert.match(
      live,
      new RegExp(`vecDebug\\[c\\.id\\](?:\\?\\.)?\\.${key}`),
      `view no longer reads vecDebug[c.id].${key}`,
    )
  }
})
