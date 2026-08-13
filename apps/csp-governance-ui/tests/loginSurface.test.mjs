import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  DEFAULT_LOGIN_AUTH_MODE,
  LOGIN_ALTERNATIVES_QUERY,
  LOGIN_ALTERNATIVES_QUERY_VALUE,
  getLoginAuthModeFromProviders,
  loadLoginSurface,
  shouldRenderAlternativeLogin,
} from '../src/utils/loginSurface.js'

const loginView = readFileSync(new URL('../src/views/LoginView.vue', import.meta.url), 'utf8')

test('card-only without the bypass does not render alternative login', () => {
  assert.equal(
    shouldRenderAlternativeLogin(
      getLoginAuthModeFromProviders({ auth_mode: 'card-only', providers: [] }),
      {},
    ),
    false,
  )
  assert.match(loginView, /<details v-if="showAlternativeLogin" class="login__more">/)
  assert.match(loginView, /<TermModal v-if="showAlternativeLogin" :visible="showRegisterModal"/)
})

test('card-only with the explicit bypass renders alternative login', () => {
  assert.equal(
    shouldRenderAlternativeLogin(
      getLoginAuthModeFromProviders({ auth_mode: 'card-only', providers: [] }),
      { [LOGIN_ALTERNATIVES_QUERY]: LOGIN_ALTERNATIVES_QUERY_VALUE },
    ),
    true,
  )
})

test('non-card-only modes keep rendering alternative login', () => {
  assert.equal(
    shouldRenderAlternativeLogin(
      getLoginAuthModeFromProviders({ auth_mode: 'password', providers: [] }),
      {},
    ),
    true,
  )
  assert.equal(
    shouldRenderAlternativeLogin(
      getLoginAuthModeFromProviders({ auth_mode: 'mixed', providers: [] }),
      {},
    ),
    true,
  )
})

test('missing auth_mode fails closed but the bypass still renders the block', () => {
  const missingSignal = getLoginAuthModeFromProviders({ providers: [] })
  assert.equal(missingSignal, DEFAULT_LOGIN_AUTH_MODE)
  assert.equal(shouldRenderAlternativeLogin(missingSignal, {}), false)
  assert.equal(
    shouldRenderAlternativeLogin(missingSignal, {
      [LOGIN_ALTERNATIVES_QUERY]: LOGIN_ALTERNATIVES_QUERY_VALUE,
    }),
    true,
  )
})

test('a failed providers request fails closed but the bypass remains independent', async () => {
  const failedRequest = await loadLoginSurface(async () => {
    throw new Error('providers unavailable')
  })
  assert.equal(failedRequest.authMode, DEFAULT_LOGIN_AUTH_MODE)
  assert.equal(shouldRenderAlternativeLogin(failedRequest.authMode, {}), false)
  assert.equal(
    shouldRenderAlternativeLogin(failedRequest.authMode, {
      [LOGIN_ALTERNATIVES_QUERY]: LOGIN_ALTERNATIVES_QUERY_VALUE,
    }),
    true,
  )
})
