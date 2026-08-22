import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  postLoginDestination,
  isGovernanceRole,
  workbenchEntries,
} from '../src/utils/postLoginDestination.js'
import { safeNextDestination } from '../src/utils/appOrigins.js'

function source(relative) {
  return readFileSync(new URL(`../src/${relative}`, import.meta.url), 'utf8')
}

test('regular users land in the task workbench; governance roles stay in system management', () => {
  assert.equal(postLoginDestination({ role: 'user' }), '/anila/app')
  assert.equal(postLoginDestination({ role: 'developer' }), '/')
  assert.equal(postLoginDestination({ role: 'admin' }), '/')
  assert.equal(postLoginDestination({ role: 'owner' }), '/')
  assert.equal(isGovernanceRole('user'), false)
  assert.equal(isGovernanceRole('developer'), true)
})

test('the regular-user workbench exposes four distinct entries', () => {
  const entries = workbenchEntries()
  assert.deepEqual(
    entries.map((entry) => entry.label),
    ['任務中心', '我的知識庫', '產出中心', '專案入口'],
  )
  assert.equal(new Set(entries.map((entry) => entry.href)).size, entries.length)
})

test('governance routes use a route-keyed view and reserve /keys for the SPA', () => {
  const layout = source('components/layout/AppLayout.vue')
  const router = source('router/index.js')
  const vite = readFileSync(new URL('../vite.config.js', import.meta.url), 'utf8')
  assert.match(layout, /:key="viewRoute\.fullPath"/)
  assert.match(router, /path:\s*'keys'/)
  assert.match(router, /path:\s*'api-keys'[\s\S]{0,100}redirect:\s*\{\s*name:\s*'ApiKeys'/)
  assert.match(vite, /\^\/api\(\?:\/\|\$\)/)
  assert.match(vite, /strictPort:\s*true/)
  assert.doesNotMatch(vite, /['"]\/api['"]\s*:/)
  for (const path of ['models', 'usage', 'developer/agents']) {
    assert.match(router, new RegExp(`path:\\s*'${path.replace('/', '\\/')}'`))
  }
})

test('forbidden pages explain the required role instead of redirecting home', () => {
  const router = source('router/index.js')
  assert.match(router, /name:\s*'Forbidden'/)
  assert.match(router, /required:\s*to\.meta\.requiredRole/)
  assert.doesNotMatch(router, /requiresAdmin[\s\S]{0,120}next\(['"]\/['"]\)/)
})

test('logout clears local state before the network and then hard-replaces /login', () => {
  const store = source('stores/auth.js')
  const header = source('components/layout/AppHeader.vue')
  const clearAt = store.indexOf('user.value = null', store.indexOf('async function logout'))
  const requestAt = store.indexOf('await logoutApi()', store.indexOf('async function logout'))
  assert.ok(clearAt > 0 && requestAt > clearAt)
  assert.match(header, /await authStore\.logout\(\)/)
  assert.match(header, /window\.location\.replace\(loginHref\(\)\)/)
})

test('user-facing copy uses 工作臺 / 系統管理, not leftover admin chrome', () => {
  const workbench = source('views/WorkbenchHomeView.vue')
  const header = source('components/layout/AppHeader.vue')
  const sidebar = source('components/layout/AppSidebar.vue')
  const login = source('views/LoginView.vue')
  assert.match(workbench, /院內 AI 工作臺/)
  assert.match(workbench, /系統管理是進階管理畫面/)
  assert.match(header, /subtitle="系統管理"/)
  assert.match(sidebar, /label: '工作臺'/)
  assert.match(login, /院內 AI 工作臺/)
  assert.doesNotMatch(login, /cardComponentOrigin \}\}/)
})

test('user-table destructive actions are in an overflow menu and self-protected', () => {
  const users = source('views/UsersView.vue')
  assert.match(users, /<OverflowMenu/)
  assert.match(users, /:disabled="isSelf\(user\)"/)
  assert.match(users, /不能停用自己目前登入的帳號/)
  assert.match(users, /不能刪除自己目前登入的帳號/)
  assert.doesNotMatch(users, /class="term-action term-action--danger" @click="handleDeactivate/)
})

test('next= rejects open redirects and protocol-relative tricks', () => {
  assert.equal(safeNextDestination('https://evil.example', '/safe'), '/safe')
  assert.equal(safeNextDestination('//evil.example', '/safe'), '/safe')
  assert.equal(safeNextDestination('/\\evil.example', '/safe'), '/safe')
  assert.equal(safeNextDestination('javascript:alert(1)', '/safe'), '/safe')
  assert.match(safeNextDestination('/anila/app', '/safe'), /\/anila\/app$/)
})

test('unauthenticated governance routes keep next=, and signed-in /login uses role landing', () => {
  const router = source('router/index.js')
  const login = source('views/LoginView.vue')
  assert.match(router, /query:\s*\{\s*next:\s*nextHref\s*\}/)
  assert.match(router, /postLoginDestination\(authStore\.user, to\.query\.next\)/)
  assert.match(login, /oidcNextPath\(resolveNextDestination\(\)\)/)
})
