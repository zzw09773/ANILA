import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/dom.mjs'
import { build } from 'vite'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { createPinia, setActivePinia } from 'pinia'
import { invalidateIdentity, currentIdentityEpoch } from '../src/api/identityEpoch.js'
import { reads, logouts, resetIdentityMock, clientAuthCalls, setRefreshBehaviour } from './helpers/authIdentityMock.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const mock = new URL('./helpers/authIdentityMock.mjs', import.meta.url).href
const identity = new URL('../src/api/identityEpoch.js', import.meta.url).href
let useAuthStore, client, store
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }

async function load(entry, aliases) {
  const result = await build({ configFile: false, envDir: false, root: ROOT, logLevel: 'silent',
    plugins: [{ name: 'auth-identity-isolation', enforce: 'pre', resolveId(id) {
      if (Object.hasOwn(aliases, id)) return { id: aliases[id], external: true }
    } }], build: { write: false, minify: false,
      lib: { entry: resolve(ROOT, entry), formats: ['es'] },
      rollupOptions: { external: ['vue', 'pinia', 'axios'], output: { inlineDynamicImports: true,
        paths: Object.fromEntries(['vue', 'pinia', 'axios'].map(name => [name, import.meta.resolve(name)])) } } } })
  const chunk = (Array.isArray(result) ? result[0] : result).output.find(item => item.isEntry)
  return import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)
}

test.before(async () => {
  ;({ useAuthStore } = await load('src/stores/auth.js', {
    '../api/auth': mock, '../api/caAuth': mock, '../api/identityEpoch': identity,
  }))
  ;({ default: client } = await load('src/api/client.js', {
    '../stores/auth': mock, '../router': mock, './identityEpoch': identity,
  }))
})
test.beforeEach(() => { resetIdentityMock(); setActivePinia(createPinia()); store = null })
test.afterEach(() => { store?.$dispose() })

test('a pending /me cannot restore the previous user after local identity invalidation', async () => {
  store = useAuthStore()
  assert.equal(reads.length, 1)
  invalidateIdentity()
  reads[0].resolve({ data: { id: 41, role: 'owner' } })
  await settle()
  assert.equal(store.user, null)
  assert.equal(store.isAdmin, false)
})

test('other-tab invalidation rechecks /me and ignores an older account response', async () => {
  store = useAuthStore()
  const event = new Event('storage')
  Object.assign(event, { key: 'anila:identity-epoch', newValue: JSON.stringify({ version: 1,
    epoch: 'bbbbcccc-dddd-4eee-8fff-000000000001' }) })
  window.dispatchEvent(event)
  assert.equal(reads.length, 2)
  assert.equal(store.user, null)
  reads[1].resolve({ data: { id: 52, role: 'user' } })
  reads[0].resolve({ data: { id: 41, role: 'owner' } })
  await settle()
  assert.equal(store.user.id, 52)
  assert.equal(store.isOwner, false)
  assert.equal(store.initialized, true)
})

test('concurrent account reads only adopt the most recently requested result', async () => {
  store = useAuthStore()
  const newer = store.fetchUser()
  reads[1].resolve({ data: { id: 52, role: 'user' } })
  await newer
  reads[0].reject(new Error('old request'))
  await settle()
  assert.equal(store.user.id, 52)
})

test('logout clears identity before waiting for HTTP and cannot overwrite a later confirmed read', async () => {
  store = useAuthStore()
  reads[0].resolve({ data: { id: 41, role: 'owner' } })
  await settle()
  const pending = store.logout()
  assert.equal(store.user, null)
  assert.equal(logouts.length, 1)
  const newer = store.fetchUser()
  reads[1].resolve({ data: { id: 52, role: 'user' } })
  await newer
  logouts[0].resolve({})
  await pending
  assert.equal(store.user.id, 52)
})

test('disposed auth store stops responding to cross-tab notifications', () => {
  store = useAuthStore()
  store.$dispose()
  store = null
  const event = new Event('storage')
  Object.assign(event, { key: 'anila:identity-epoch', newValue: JSON.stringify({ version: 1,
    epoch: 'bbbbcccc-dddd-4eee-8fff-000000000002' }) })
  window.dispatchEvent(event)
  assert.equal(reads.length, 1)
})

test('HTTP client rejects stale /me before announcing the old actor', async () => {
  let release, entered
  const adapterEntered = new Promise(resolve => { entered = resolve })
  client.defaults.adapter = config => new Promise(resolve => {
    release = () => resolve({ config, data: { id: 41 }, status: 200, statusText: 'OK', headers: {} })
    entered()
  })
  const announced = []
  const listener = event => announced.push(event.detail.userId)
  window.addEventListener('anila:identity', listener)
  try {
    const pending = client.get('/api/auth/me')
    await adapterEntered
    invalidateIdentity()
    release()
    await assert.rejects(pending, error => error.code === 'IDENTITY_CHANGED')
    assert.deepEqual(announced, [null])
  } finally { window.removeEventListener('anila:identity', listener) }
})

test('successful login, card verification and logout invalidate; pending card registration does not', async () => {
  let status = 200
  client.defaults.adapter = async config => ({ config, data: {}, status, statusText: 'OK', headers: {} })
  const announced = []
  const listener = event => announced.push(event.detail.userId)
  window.addEventListener('anila:identity', listener)
  try {
    await client.post('/api/auth/login', {})
    await client.post('/api/auth/card/verify', {})
    await client.post('/api/auth/logout', {})
    status = 202
    await client.post('/api/auth/card/verify', {})
    assert.deepEqual(announced, [null, null, null])
  } finally { window.removeEventListener('anila:identity', listener) }
})

test('a current /me announces only the server-confirmed actor', async () => {
  client.defaults.adapter = async config => ({ config, data: { id: 52 }, status: 200, statusText: 'OK', headers: {} })
  const announced = []
  const listener = event => announced.push(event.detail.userId)
  window.addEventListener('anila:identity', listener)
  try { await client.get('/api/auth/me'); assert.deepEqual(announced, [52]) }
  finally { window.removeEventListener('anila:identity', listener) }
})

function unauthorized(config) {
  return Object.assign(new Error('synthetic 401'), { config, response: { status: 401 } })
}

test('a late 401 cannot refresh, replay an old mutation or log out the current account', async () => {
  let release, entered, calls = 0
  const ready = new Promise(resolve => { entered = resolve })
  client.defaults.adapter = config => new Promise((resolve, reject) => {
    calls += 1
    release = () => reject(unauthorized(config))
    entered()
  })
  const pending = client.post('/api/synthetic-action', { value: 'old account data' })
  await ready
  invalidateIdentity()
  release()
  await assert.rejects(pending, error => error.code === 'IDENTITY_CHANGED')
  assert.equal(calls, 1)
  assert.deepEqual(clientAuthCalls, { refresh: 0, logout: 0 })
})

for (const failure of [false, true]) {
  test(`identity change during refresh ${failure ? 'failure' : 'success'} prevents both replay and logout`, async () => {
    let release, entered, calls = 0
    const ready = new Promise(resolve => { entered = resolve })
    setRefreshBehaviour(() => new Promise((resolve, reject) => {
      release = () => failure ? reject(new Error('synthetic refresh failure')) : resolve()
      entered()
    }))
    client.defaults.adapter = async config => { calls += 1; throw unauthorized(config) }
    const pending = client.post('/api/synthetic-action', {})
    await ready
    invalidateIdentity()
    release()
    await assert.rejects(pending, error => error.code === 'IDENTITY_CHANGED')
    assert.equal(calls, 1)
    assert.deepEqual(clientAuthCalls, { refresh: 1, logout: 0 })
  })
}

test('an ordinary same-identity 401 still refreshes and retries once', async () => {
  let calls = 0
  client.defaults.adapter = async config => {
    if (++calls === 1) throw unauthorized(config)
    return { config, data: { ok: true }, status: 200, statusText: 'OK', headers: {} }
  }
  assert.equal((await client.post('/api/synthetic-action', {})).data.ok, true)
  assert.equal(calls, 2)
  assert.deepEqual(clientAuthCalls, { refresh: 1, logout: 0 })
})

test('reusing a previous-account request config cannot replace its captured identity epoch', async () => {
  let calls = 0
  client.defaults.adapter = async config => {
    calls += 1
    return { config, data: {}, status: 200, statusText: 'OK', headers: {} }
  }
  const first = await client.post('/api/synthetic-action', {})
  invalidateIdentity()
  await assert.rejects(client(first.config), error => error.code === 'IDENTITY_CHANGED')
  assert.equal(calls, 1)
})

for (const failure of [false, true]) {
  test(`a late refresh HTTP ${failure ? 'failure' : 'success'} invalidates display without adopting a user`, async () => {
    let release, entered
    const ready = new Promise(resolve => { entered = resolve })
    client.defaults.adapter = config => new Promise((resolve, reject) => {
      release = () => failure ? reject(unauthorized(config))
        : resolve({ config, data: {}, status: 200, statusText: 'OK', headers: {} })
      entered()
    })
    const pending = client.post('/api/auth/refresh', {})
    await ready
    invalidateIdentity()
    const before = currentIdentityEpoch()
    release()
    await assert.rejects(pending, error => error.code === 'IDENTITY_CHANGED')
    assert.equal(currentIdentityEpoch(), before + 1)
    assert.deepEqual(clientAuthCalls, { refresh: 0, logout: 0 })
  })
}

test('ordinary stale successful responses are rejected before updating account controls', async () => {
  let release, entered
  const ready = new Promise(resolve => { entered = resolve })
  client.defaults.adapter = config => new Promise(resolve => {
    release = () => resolve({ config, data: { private: 'old account' }, status: 200, statusText: 'OK', headers: {} })
    entered()
  })
  const pending = client.get('/api/synthetic-private')
  await ready
  invalidateIdentity()
  release()
  await assert.rejects(pending, error => error.code === 'IDENTITY_CHANGED')
})
