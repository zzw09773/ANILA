// File-picker accept ↔ ParserRegistry._PARSERS — asserted on the value the
// browser actually receives, not on the presence of an identifier.
//
// The knowledge-base pickers used to offer a strict subset of what the worker
// can actually parse (DATCOM .out / .dcm missing; .markdown offered but
// unregistered). Three components upload to the same endpoint
// (POST /api/ingestion/collections/{id}/documents), so all three are pinned:
//
//   apps/csp-governance-ui/src/views/CollectionDetailView.vue
//   apps/csp-governance-ui/src/views/ChunkingPreviewView.vue
//   apps/anilalm/src/workspace/WSSidebar.tsx
//
// Why source extraction rather than "does the file mention the constant":
// an earlier version of this suite only regex-checked that the identifier
// INGESTION_FILE_ACCEPT appeared somewhere in the file. Hard-coding
// accept=".pdf,.docx,.doc" back onto the <input> while leaving the import
// line in place satisfied that check — the exact regression this package
// fixed could grow back with the suite green. So instead we resolve each
// picker's *effective* accept string:
//
//   accept="literal"          → the literal wins (that is what ships)
//   :accept="IDENT" / {IDENT} → resolve IDENT out of that app's constant file
//
// and assert the resulting extension set equals _PARSERS. Re-hardcoding now
// takes the literal branch and fails.

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '../../..')
const P = (rel) => resolve(REPO_ROOT, rel)

const PARSER_REGISTRY = P(
  'packages/anila-core/src/anila_core/ingestion/parser_registry.py',
)

// app 名 → 該 app 的常數檔（兩份是刻意的副本，見檔內註解；本測試釘住兩份
// 都等於後端清單，所以副本不會各自漂走）。
const CONSTANT_FILES = {
  'csp-governance-ui': P(
    'apps/csp-governance-ui/src/utils/ingestionFileAccept.js',
  ),
  anilalm: P('apps/anilalm/src/utils/ingestionFileAccept.ts'),
}

// 每個知識庫上傳 picker：檔案、用來認出該 <input> 的標記、常數來源 app。
const PICKERS = [
  {
    name: 'CollectionDetailView.vue',
    file: P('apps/csp-governance-ui/src/views/CollectionDetailView.vue'),
    marker: 'ref="fileInput"',
    app: 'csp-governance-ui',
  },
  {
    name: 'ChunkingPreviewView.vue',
    file: P('apps/csp-governance-ui/src/views/ChunkingPreviewView.vue'),
    marker: 'ref="fileInput"',
    app: 'csp-governance-ui',
  },
  {
    name: 'WSSidebar.tsx',
    file: P('apps/anilalm/src/workspace/WSSidebar.tsx'),
    marker: 'ref={fileRef}',
    app: 'anilalm',
  },
]

const read = (file) => readFileSync(file, 'utf8')

/** Parse ``_PARSERS = { ".ext": ..., }`` keys from parser_registry.py. */
function parseParserExtensions(pySource) {
  const start = pySource.indexOf('_PARSERS')
  if (start < 0) throw new Error('_PARSERS not found in parser_registry.py')
  const brace = pySource.indexOf('{', start)
  // The dict body only contains `.ext`: Constructor(), lines — no nested `{}`.
  const end = pySource.indexOf('}', brace)
  if (brace < 0 || end < 0) throw new Error('_PARSERS dict incomplete')
  const exts = new Set()
  for (const m of pySource.slice(brace, end + 1).matchAll(/"(\.[A-Za-z0-9]+)"/g)) {
    exts.add(m[1].toLowerCase())
  }
  if (exts.size === 0) throw new Error('_PARSERS parsed empty')
  return exts
}

/**
 * Parse ``INGESTION_FILE_EXTENSIONS = [ '.a', '.b' ]`` out of a constant file.
 * Source-parsed (not imported) so the .ts twin is handled the same way.
 */
function parseConstantExtensions(source, file) {
  const decl = source.indexOf('INGESTION_FILE_EXTENSIONS')
  if (decl < 0) throw new Error(`INGESTION_FILE_EXTENSIONS not found in ${file}`)
  // Start after the `=` so a TS type annotation (`: string[]`) is skipped.
  const start = source.indexOf('=', decl)
  const open = source.indexOf('[', start)
  const close = source.indexOf(']', open)
  if (open < 0 || close < 0) throw new Error(`array literal incomplete in ${file}`)
  const exts = source
    .slice(open, close)
    .match(/'(\.[A-Za-z0-9]+)'/g)
    ?.map((s) => s.slice(1, -1).toLowerCase())
  if (!exts || exts.length === 0) throw new Error(`array parsed empty in ${file}`)
  return exts
}

/** Slice out the single ``<input …/>`` tag carrying ``marker``. */
function inputTag(source, marker, name) {
  const tags = []
  let idx = source.indexOf('<input')
  while (idx >= 0) {
    const end = source.indexOf('/>', idx)
    if (end < 0) break
    tags.push(source.slice(idx, end + 2))
    idx = source.indexOf('<input', end)
  }
  const matched = tags.filter((tag) => tag.includes(marker))
  assert.equal(
    matched.length,
    1,
    `${name}: expected exactly 1 <input> matching ${marker}, found ${matched.length}` +
      ' — the upload picker moved or was renamed; fix this guard, do not delete it',
  )
  return matched[0]
}

/**
 * The accept string this picker actually hands the browser.
 *
 * Static ``accept="…"`` wins over any binding, because that is what renders.
 * A binding must name INGESTION_FILE_ACCEPT and the component must import it;
 * the value then comes from that app's constant file.
 */
function effectiveAccept(picker) {
  const source = read(picker.file)
  const tag = inputTag(source, picker.marker, picker.name)

  const staticAttr = tag.match(/(?<![:\w-])accept="([^"]*)"/)
  if (staticAttr) return staticAttr[1]

  const bound = tag.match(/(?::accept="([^"]+)"|accept=\{([^}]+)\})/)
  assert.ok(bound, `${picker.name}: <input> has no accept attribute at all`)
  const expr = (bound[1] ?? bound[2]).trim()
  assert.equal(
    expr,
    'INGESTION_FILE_ACCEPT',
    `${picker.name}: accept is bound to ${expr}, which this guard cannot resolve`,
  )
  assert.match(
    source,
    /import\s*\{[^}]*INGESTION_FILE_ACCEPT[^}]*\}\s*from\s*'[^']*ingestionFileAccept'/,
    `${picker.name}: binds INGESTION_FILE_ACCEPT without importing it`,
  )
  const constFile = CONSTANT_FILES[picker.app]
  return parseConstantExtensions(read(constFile), constFile).join(',')
}

const toSet = (accept) =>
  new Set(
    accept
      .split(',')
      .map((s) => s.trim().toLowerCase())
      .filter((s) => s.startsWith('.')),
  )

const backend = parseParserExtensions(read(PARSER_REGISTRY))

test('derives a non-empty _PARSERS from the backend source', () => {
  assert.ok(backend.size > 20)
  assert.ok(backend.has('.dcm'))
  assert.ok(backend.has('.out'))
  assert.ok(backend.has('.markdown'), '.markdown must be registered (alias of .md)')
})

for (const picker of PICKERS) {
  test(`${picker.name}: rendered accept equals ParserRegistry._PARSERS`, () => {
    const offered = toSet(effectiveAccept(picker))
    const missingFromPicker = [...backend].filter((e) => !offered.has(e)).sort()
    const extrasInPicker = [...offered].filter((e) => !backend.has(e)).sort()
    assert.deepEqual(
      missingFromPicker,
      [],
      `${picker.name} is a strict subset of the backend; missing: ${missingFromPicker.join(', ')}`,
    )
    assert.deepEqual(
      extrasInPicker,
      [],
      `${picker.name} offers extensions the backend cannot parse: ${extrasInPicker.join(', ')}`,
    )
  })

  test(`${picker.name}: offers DATCOM / owner file types .dcm .dat .inp .out`, () => {
    const offered = toSet(effectiveAccept(picker))
    for (const ext of ['.dcm', '.dat', '.inp', '.out']) {
      assert.ok(offered.has(ext), `${picker.name} must offer ${ext}`)
    }
  })
}

test('the two app constant files stay identical to each other', () => {
  const [a, b] = Object.values(CONSTANT_FILES).map((file) =>
    parseConstantExtensions(read(file), file),
  )
  assert.deepEqual(
    [...a].sort(),
    [...b].sort(),
    'the csp-governance-ui and anilalm copies of INGESTION_FILE_EXTENSIONS diverged',
  )
})
