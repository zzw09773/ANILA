import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  normalizeSessionProfile,
  shouldApplySessionProfile,
} from '../src/utils/sessionProfile.js'

const DEVELOPER = { id: 7, username: 'dev.lin', role: 'developer' }
const USER = { id: 7, username: 'dev.lin', role: 'user' }
const OTHER_USER = { id: 3, username: 'alice', role: 'user' }

test('a developer /me body keeps role=developer', () => {
  assert.deepEqual(normalizeSessionProfile(DEVELOPER).role, 'developer')
  assert.equal(normalizeSessionProfile(DEVELOPER).id, 7)
})

test('TokenResponse and missing role do not become role=user', () => {
  assert.equal(
    normalizeSessionProfile({
      access_token: 'aaa',
      refresh_token: 'bbb',
      token_type: 'bearer',
    }),
    null,
  )
  assert.equal(
    normalizeSessionProfile({ id: 7, username: 'dev.lin' }),
    null,
  )
  assert.equal(
    normalizeSessionProfile({ id: 7, username: 'dev.lin', role: 'userx' }),
    null,
  )
  assert.equal(normalizeSessionProfile(null), null)
})

test('the same id cannot hydrate downward from developer to user', () => {
  assert.equal(shouldApplySessionProfile(DEVELOPER, USER), false)
  assert.equal(shouldApplySessionProfile(USER, DEVELOPER), true)
  assert.equal(shouldApplySessionProfile(null, DEVELOPER), true)
  assert.equal(shouldApplySessionProfile(DEVELOPER, OTHER_USER), true)
})

test('fetchUser uses the session profile guard and /me is not cacheable', () => {
  const store = readFileSync(new URL('../src/stores/auth.js', import.meta.url), 'utf8')
  const authApi = readFileSync(new URL('../src/api/auth.js', import.meta.url), 'utf8')
  assert.match(store, /normalizeSessionProfile/)
  assert.match(store, /shouldApplySessionProfile/)
  assert.doesNotMatch(store, /user\.value = data/)
  assert.match(authApi, /Cache-Control/)
  assert.match(authApi, /no-store/)
  assert.match(authApi, /params:\s*\{\s*_: Date\.now\(\)\s*\}/)
})

test('logout still nulls the user before the network call', () => {
  const store = readFileSync(new URL('../src/stores/auth.js', import.meta.url), 'utf8')
  const logoutAt = store.indexOf('async function logout')
  const clearAt = store.indexOf('user.value = null', logoutAt)
  const requestAt = store.indexOf('await logoutApi()', logoutAt)
  assert.ok(logoutAt > 0)
  assert.ok(clearAt > logoutAt && requestAt > clearAt)
})
