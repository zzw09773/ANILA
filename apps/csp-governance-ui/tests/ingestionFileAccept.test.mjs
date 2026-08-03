// INGESTION_FILE_ACCEPT ↔ ParserRegistry._PARSERS — derived, not hard-coded.
//
// The knowledge-base file picker used to offer a strict subset of what the
// worker can actually parse (DATCOM .out / .dcm missing; .markdown offered
// but unregistered). Lock both directions:
//   1. every picker extension must be in _PARSERS (no unsupported offer)
//   2. every _PARSERS extension must be in the picker (no strict subset)
// so the next divergence fails the suite instead of shipping a lying dialog.

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  INGESTION_FILE_ACCEPT,
  INGESTION_FILE_EXTENSIONS,
} from '../src/utils/ingestionFileAccept.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '../../..')
const PARSER_REGISTRY = resolve(
  REPO_ROOT,
  'packages/anila-core/src/anila_core/ingestion/parser_registry.py',
)

/** Parse ``_PARSERS = { ".ext": ..., }`` keys from parser_registry.py. */
function parseParserExtensions(pySource) {
  const start = pySource.indexOf('_PARSERS')
  if (start < 0) throw new Error('_PARSERS not found in parser_registry.py')
  const brace = pySource.indexOf('{', start)
  // Match the closing brace of the dict (next top-level `}` after brace).
  // The dict body only contains `.ext`: Constructor(), lines — no nested `{}`.
  const end = pySource.indexOf('}', brace)
  if (brace < 0 || end < 0) throw new Error('_PARSERS dict incomplete')
  const body = pySource.slice(brace, end + 1)
  const exts = new Set()
  for (const m of body.matchAll(/"(\.[A-Za-z0-9]+)"/g)) {
    exts.add(m[1].toLowerCase())
  }
  if (exts.size === 0) throw new Error('_PARSERS parsed empty')
  return exts
}

function pickerExtensions(accept) {
  return accept
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter((t) => t.startsWith('.'))
}

function readSource(relativeFromSrc) {
  return readFileSync(new URL(relativeFromSrc, new URL('../src/', import.meta.url)), 'utf8')
}

const py = readFileSync(PARSER_REGISTRY, 'utf8')
const backend = parseParserExtensions(py)
const offered = new Set(pickerExtensions(INGESTION_FILE_ACCEPT))

test('derives a non-empty _PARSERS from the backend source', () => {
  assert.ok(backend.size > 20)
  assert.ok(backend.has('.dcm'))
  assert.ok(backend.has('.out'))
  assert.ok(backend.has('.markdown'), '.markdown must be registered (alias of .md)')
})

test('picker extensions equal ParserRegistry._PARSERS (not a strict subset)', () => {
  const missingFromPicker = [...backend].filter((ext) => !offered.has(ext)).sort()
  const extrasInPicker = [...offered].filter((ext) => !backend.has(ext)).sort()
  assert.deepEqual(
    missingFromPicker,
    [],
    `picker is a strict subset of backend; missing: ${missingFromPicker.join(', ')}`,
  )
  assert.deepEqual(
    extrasInPicker,
    [],
    `picker offers extensions the backend cannot parse: ${extrasInPicker.join(', ')}`,
  )
})

test('offers DATCOM / owner file types .dcm .dat .inp .out', () => {
  for (const ext of ['.dcm', '.dat', '.inp', '.out']) {
    assert.ok(offered.has(ext), `picker must offer ${ext}`)
  }
})

test('INGESTION_FILE_EXTENSIONS matches the joined accept string', () => {
  assert.equal(INGESTION_FILE_EXTENSIONS.join(','), INGESTION_FILE_ACCEPT)
})

test('CollectionDetailView and ChunkingPreviewView bind INGESTION_FILE_ACCEPT', () => {
  const detail = readSource('views/CollectionDetailView.vue')
  const preview = readSource('views/ChunkingPreviewView.vue')
  for (const [name, src] of [
    ['CollectionDetailView', detail],
    ['ChunkingPreviewView', preview],
  ]) {
    assert.match(
      src,
      /INGESTION_FILE_ACCEPT/,
      `${name} must import/bind INGESTION_FILE_ACCEPT`,
    )
    // Guard against a hard-coded narrow accept sneaking back in beside the binding.
    assert.doesNotMatch(
      src,
      /accept="\.txt,\.md/,
      `${name} must not hard-code a narrow accept= string`,
    )
  }
})

test('mutation check: a narrowed picker fails the subset assertion', () => {
  const narrowed = INGESTION_FILE_EXTENSIONS.filter(
    (ext) => !['.dcm', '.out'].includes(ext),
  )
  const narrowedSet = new Set(narrowed)
  const missing = [...backend].filter((ext) => !narrowedSet.has(ext))
  assert.ok(missing.includes('.dcm'))
  assert.ok(missing.includes('.out'))
})
