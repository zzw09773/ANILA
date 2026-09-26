/**
 * Finding one — tokens must not land in durable storage.
 *
 * Revert mutant: change durableAuthSlice to return accessToken/refreshToken
 * (the old partialize) and this file goes red.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import * as esbuild from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))

test('durableAuthSlice never includes access or refresh tokens', async () => {
  const outfile = join(mkdtempSync(join(tmpdir(), 'anilalm-auth-')), 'authStorage.mjs')
  await esbuild.build({
    entryPoints: [join(here, 'authStorage.ts')],
    outfile,
    bundle: true,
    format: 'esm',
    platform: 'node',
  })
  const mod = await import(pathToFileURL(outfile).href + `?t=${Date.now()}`)
  const slice = mod.durableAuthSlice({
    accessToken: 'tok-access-must-not-persist',
    refreshToken: 'tok-refresh-must-not-persist',
  })
  assert.deepEqual(slice, {})
  assert.equal('accessToken' in slice, false)
  assert.equal('refreshToken' in slice, false)

  // Simulate what zustand persist would write if it used durableAuthSlice.
  const storage = {
    _data: {},
    getItem(k) {
      return this._data[k] ?? null
    },
    setItem(k, v) {
      this._data[k] = String(v)
    },
    removeItem(k) {
      delete this._data[k]
    },
  }
  storage.setItem(
    mod.AUTH_STORAGE_KEY,
    JSON.stringify({ state: slice, version: 0 }),
  )
  assert.equal(mod.authStorageHasTokens(storage), false)
})

test('chat model comes from the knowledge_chat role, not a build-time name', () => {
  const chat = readFileSync(join(here, '../api/chat.ts'), 'utf8')
  const modelRole = readFileSync(join(here, '../api/modelRole.ts'), 'utf8')
  assert.equal(chat.includes('VITE_DEFAULT_CHAT_MODEL'), false)
  assert.equal(modelRole.includes('VITE_DEFAULT_CHAT_MODEL'), false)
  assert.match(chat, /resolveKnowledgeChatModel/)
  assert.match(modelRole, /\/api\/models\/roles\/knowledge_chat/)
})

test('auth store does not keep access or refresh tokens', () => {
  const auth = readFileSync(join(here, 'auth.ts'), 'utf8')
  const client = readFileSync(join(here, '../api/client.ts'), 'utf8')
  const chat = readFileSync(join(here, '../api/chat.ts'), 'utf8')
  const studio = readFileSync(join(here, '../api/studio.ts'), 'utf8')
  assert.equal(/accessToken\s*:/.test(auth), false)
  assert.equal(/refreshToken\s*:/.test(auth), false)
  assert.equal(/headers\.Authorization\s*=/.test(client), false)
  assert.equal(/Authorization:\s*`Bearer/.test(chat), false)
  assert.equal(/Authorization:\s*`Bearer/.test(studio), false)
})

test('auth.ts no longer partializes tokens into localStorage', () => {
  const src = readFileSync(join(here, 'auth.ts'), 'utf8')
  // Old bug: partialize returned { accessToken, refreshToken }.
  assert.equal(
    /partialize:\s*\([^)]*\)\s*=>\s*\(\s*\{[\s\S]*accessToken:\s*s\.accessToken/.test(
      src,
    ),
    false,
    'auth.ts must not partialize accessToken into durable storage',
  )
  assert.equal(
    /createJSONStorage\(\s*\(\)\s*=>\s*localStorage\s*\)/.test(src),
    false,
    'auth.ts must not bind zustand persist to localStorage for tokens',
  )
  assert.match(src, /wipeAuthStorage/)
})
