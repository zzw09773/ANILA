/**
 * Finding one — cookie session survives a reload with empty durable storage.
 *
 * Simulates: localStorage cleared, in-memory tokens null, hydrate → getMe
 * succeeds because the axios client sends the httpOnly cookie
 * (withCredentials). After hydrate, status is authed and storage has no token.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import * as esbuild from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))

function makeMemoryStorage() {
  const data = Object.create(null)
  return {
    getItem(k) {
      return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null
    },
    setItem(k, v) {
      data[k] = String(v)
    },
    removeItem(k) {
      delete data[k]
    },
    clear() {
      for (const k of Object.keys(data)) delete data[k]
    },
    _dump() {
      return { ...data }
    },
  }
}

test('reload with cleared localStorage still auths via cookie; no token stored', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'anilalm-cookie-session-'))
  const mockDir = join(dir, 'mocks')
  mkdirSync(mockDir)

  writeFileSync(
    join(mockDir, 'client.ts'),
    `
export function bindAuthAdapter(_a) {}
`,
  )
  writeFileSync(
    join(mockDir, 'auth.ts'),
    `
export const refreshToken = async () => ({ data: { access_token: 'a2', refresh_token: 'r2' } })
export const getMe = async () => {
  // Cookie path: no Bearer needed; server accepts httpOnly session cookie.
  return { data: { id: 1, username: 'cookie-user', role: 'user' } }
}
export const logoutApi = async () => ({})
`,
  )
  writeFileSync(join(mockDir, 'types.ts'), `export {}\n`)

  const mockClient = join(mockDir, 'client.ts')
  const mockAuth = join(mockDir, 'auth.ts')
  const mockTypes = join(mockDir, 'types.ts')

  const outfile = join(dir, 'bundle.mjs')
  await esbuild.build({
    entryPoints: [join(here, 'auth.ts')],
    outfile,
    bundle: true,
    format: 'esm',
    platform: 'node',
    plugins: [
      {
        name: 'mock-anilalm-api',
        setup(build) {
          build.onResolve({ filter: /\/api\/client$/ }, () => ({ path: mockClient }))
          build.onResolve({ filter: /\/api\/auth$/ }, () => ({ path: mockAuth }))
          build.onResolve({ filter: /\/types$/ }, () => ({ path: mockTypes }))
        },
      },
    ],
    banner: {
      js: `
const __mem = globalThis.__ANILALM_TEST_STORAGE__;
if (__mem && typeof globalThis.localStorage === 'undefined') {
  Object.defineProperty(globalThis, 'localStorage', { value: __mem, configurable: true });
}
`,
    },
  })

  // Also bundle authStorage for assertions.
  const storageOut = join(dir, 'authStorage.mjs')
  await esbuild.build({
    entryPoints: [join(here, 'authStorage.ts')],
    outfile: storageOut,
    bundle: true,
    format: 'esm',
    platform: 'node',
  })

  const storage = makeMemoryStorage()
  storage.setItem(
    'anilalm:auth',
    JSON.stringify({
      state: {
        accessToken: 'legacy-access-should-be-wiped',
        refreshToken: 'legacy-refresh-should-be-wiped',
      },
      version: 0,
    }),
  )
  globalThis.__ANILALM_TEST_STORAGE__ = storage
  // Ensure localStorage exists before the store module evaluates wipeAuthStorage.
  Object.defineProperty(globalThis, 'localStorage', {
    value: storage,
    configurable: true,
  })

  const mod = await import(pathToFileURL(outfile).href + `?t=${Date.now()}`)
  const storageMod = await import(
    pathToFileURL(storageOut).href + `?t=${Date.now()}`
  )

  assert.equal(
    storageMod.authStorageHasTokens(storage),
    false,
    'legacy token blob must be wiped on module load',
  )

  const store = mod.useAuthStore
  assert.equal(Object.hasOwn(store.getState(), 'accessToken'), false)
  assert.equal(Object.hasOwn(store.getState(), 'refreshToken'), false)

  await store.getState().hydrate()

  assert.equal(store.getState().status, 'authed')
  assert.equal(store.getState().user?.username, 'cookie-user')
  assert.equal(Object.hasOwn(store.getState(), 'accessToken'), false)
  assert.equal(Object.hasOwn(store.getState(), 'refreshToken'), false)
  assert.equal(storageMod.authStorageHasTokens(storage), false)

  // refresh 回應裡的權杖不得進 store。
  assert.equal(await store.getState().refresh(), true)
  assert.equal(store.getState().status, 'authed')
  assert.equal(Object.hasOwn(store.getState(), 'refreshToken'), false)
  assert.equal(JSON.stringify(store.getState()).includes('r2'), false)
  assert.equal(JSON.stringify(store.getState()).includes('a2'), false)
})
