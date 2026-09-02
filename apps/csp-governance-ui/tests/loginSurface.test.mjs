import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  BREAK_GLASS_LOGIN_ERROR,
  BREAK_GLASS_LOGIN_NOTICE,
  DEFAULT_LOGIN_AUTH_MODE,
  PENDING_APPROVAL_LOGIN_CODE,
  LOGIN_ALTERNATIVES_QUERY,
  LOGIN_ALTERNATIVES_QUERY_VALUE,
  getLoginErrorMessage,
  getLoginErrorCode,
  getLoginAuthModeFromProviders,
  loadLoginSurface,
  shouldRenderSelfRegistration,
  shouldShowBreakGlassNotice,
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
  assert.match(loginView, /<details v-if="showAlternativeLogin"[^>]*class="login__more"/)
  assert.match(loginView, /<TermModal v-if="showSelfRegistration" :visible="showRegisterModal"/)
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

test('break-glass shows a static owner explanation and keeps the credential form', () => {
  const bypass = {
    [LOGIN_ALTERNATIVES_QUERY]: LOGIN_ALTERNATIVES_QUERY_VALUE,
  }

  assert.equal(shouldShowBreakGlassNotice('card-only', bypass), true)
  assert.equal(shouldRenderSelfRegistration('card-only'), false)
  assert.match(loginView, /v-if="showBreakGlassNotice"[\s\S]*BREAK_GLASS_LOGIN_NOTICE/)
  assert.match(loginView, /<form class="login__form" @submit\.prevent="handleLogin"/)
  assert.match(loginView, /<TermButton type="submit" variant="primary"/)
  assert.match(loginView, /showSelfRegistration[\s\S]*@click="openRegisterModal"/)
  assert.equal(BREAK_GLASS_LOGIN_NOTICE, '此帳號密碼登入僅供平台擁有者於憑證卡故障時緊急使用。')
})

test('self-registration stays available only where the backend supports it', () => {
  assert.equal(shouldRenderSelfRegistration('password'), true)
  assert.equal(shouldRenderSelfRegistration('mixed'), true)
  assert.equal(shouldRenderSelfRegistration('card-only'), false)
  assert.match(loginView, /<TermModal v-if="showSelfRegistration"/)
})

test('break-glass login errors are the same fixed Traditional Chinese message for every rejection', () => {
  const rejectionResponses = [
    { status: 404, data: { detail: 'Not Found' } },
    { status: 404, data: { detail: '帳號或密碼錯誤' } },
    { status: 404, data: { detail: '等待核准中' } },
    { status: 404, data: { detail: 'SSO only' } },
    { status: 404, data: { detail: '非平台擁有者' } },
  ]
  const messages = rejectionResponses.map((response) =>
    getLoginErrorMessage({ response }, true),
  )

  assert.deepEqual(new Set(messages), new Set([BREAK_GLASS_LOGIN_ERROR]))
  assert.equal(BREAK_GLASS_LOGIN_ERROR, '登入未成功；此帳號密碼通道僅供平台擁有者使用。')
  assert.match(loginView, /showBreakGlassNotice\.value[\s\S]*getLoginErrorMessage\(e, true\)/)
})

test('pending approval branch uses the stable code even when backend wording changes', () => {
  const error = {
    response: {
      data: {
        detail: {
          code: 'pending_approval',
          message: '後端改寫後的待核准說明',
        },
      },
    },
  }
  assert.equal(getLoginErrorCode(error), 'pending_approval')
  assert.equal(getLoginErrorMessage(error), '後端改寫後的待核准說明')
  assert.equal(PENDING_APPROVAL_LOGIN_CODE, 'pending_approval')
  assert.match(loginView, /getLoginErrorCode\(e\) === PENDING_APPROVAL_LOGIN_CODE/)
  assert.doesNotMatch(loginView, /detail\.(?:includes|toLowerCase)/)
})

test('without the query parameter card-only keeps the original hidden alternative branch', () => {
  assert.equal(shouldShowBreakGlassNotice('card-only', {}), false)
  assert.equal(shouldRenderSelfRegistration('card-only'), false)
  assert.equal(shouldRenderAlternativeLogin('card-only', {}), false)
  assert.match(loginView, /<details v-if="showAlternativeLogin"[^>]*class="login__more"/)
})

// 2026-09-02：這台要給外網用、走帳密登入。password 模式下登入頁不能再以
// 「請插入自然人憑證卡」開場、把帳密表單收在「其他登入方式」裡——沒有讀卡機的人
// 第一眼就卡住。password 模式：帳密表單是主角、憑證卡區塊整個不畫、hero 副標改字。
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { shouldRenderCardLogin, isPasswordPrimary, loginHeroSubtitle } from '../src/utils/loginSurface.js'

test('password 模式：不畫憑證卡區塊、帳密表單是主角、副標講帳密', () => {
  assert.equal(shouldRenderCardLogin('password'), false)
  assert.equal(isPasswordPrimary('password'), true)
  assert.equal(loginHeroSubtitle('password'), '請以帳號密碼登入')
})

test('mixed / card-only：憑證卡區塊照畫、副標照舊', () => {
  for (const mode of ['mixed', 'card-only']) {
    assert.equal(shouldRenderCardLogin(mode), true, mode)
    assert.equal(isPasswordPrimary(mode), false, mode)
    assert.equal(loginHeroSubtitle(mode), '請插入自然人憑證卡登入', mode)
  }
})

test('LoginView 把三個判斷接上了（v-if 憑證卡區塊、details 預設展開、副標綁定）', () => {
  const src = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../src/views/LoginView.vue'), 'utf8')
  assert.match(src, /v-if="showCardLogin"/u)
  assert.match(src, /:open="passwordPrimary"/u)
  assert.match(src, /\{\{ heroSubtitle \}\}/u)
})
