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
    ['工作臺', '我的知識庫', '製作', '專案入口'],
  )
  assert.equal(new Set(entries.map((entry) => entry.href)).size, entries.length)
})

test('governance routes use a route-keyed view and reserve /keys for the SPA', () => {
  const layout = source('components/layout/AppLayout.vue')
  const router = source('router/index.js')
  const vite = readFileSync(new URL('../vite.config.js', import.meta.url), 'utf8')
  assert.match(layout, /:key="viewRoute\.fullPath"/)
  assert.doesNotMatch(layout, /AppStatusBar/)
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
  const forbidden = source('views/ForbiddenView.vue')
  assert.match(router, /name:\s*'Forbidden'/)
  assert.match(router, /required:\s*to\.meta\.requiredRole/)
  assert.doesNotMatch(router, /requiresAdmin[\s\S]{0,120}next\(['"]\/['"]\)/)
  assert.match(forbidden, /這頁你看不到。請回工作臺，或請管理員開權限。/)
  assert.match(forbidden, /回工作臺/)
  assert.doesNotMatch(forbidden, /前往任務中心/)
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

test('shared design plan pins 繪 tokens and 編 workbench names', () => {
  const plan = readFileSync(new URL('../../DESIGN.md', import.meta.url), 'utf8')
  const tokens = readFileSync(new URL('../../shared/tokens.css', import.meta.url), 'utf8')
  const alias = source('assets/styles/tokens.css')
  assert.match(plan, /#1B3A6B/)
  assert.match(plan, /#1A5BB8/)
  assert.match(plan, /#F2F5F9/)
  assert.match(plan, /#9BB8DC/)
  assert.match(plan, /ANILA · 院內 AI 工作平台/)
  assert.match(plan, /工作臺/)
  assert.match(plan, /Open WebUI/)
  assert.match(plan, /製作/)
  assert.match(plan, /我的知識庫/)
  assert.match(plan, /專案入口/)
  assert.doesNotMatch(plan, /產出中心/)
  assert.doesNotMatch(plan, /即將推出/)
  assert.match(tokens, /--ink:\s*#1b3a6b/)
  assert.match(tokens, /--signal:\s*#1a5bb8/)
  assert.match(tokens, /--canvas:\s*#f2f5f9/)
  assert.match(alias, /shared\/tokens\.css/)
  assert.match(alias, /shared\/chrome\.css/)
})

test('admin tables put extra actions in an overflow, not a wrapping stack', () => {
  const models = source('views/ModelsView.vue')
  const collections = source('views/KnowledgeCollectionsView.vue')
  const agents = source('views/DeveloperAgentsView.vue')
  const links = source('views/PlatformLinksView.vue')
  const actions = source('views/MessageActionsView.vue')
  for (const src of [models, collections, agents, links, actions]) {
    assert.match(src, /<OverflowMenu/)
  }
  for (const src of [models, agents, links, actions]) {
    assert.match(src, /flex-wrap: nowrap/)
  }
})

test('shared chrome labels are sentence case, not uppercase tracking', () => {
  const section = source('components/cli/TermSection.vue')
  const stat = source('components/cli/TermStat.vue')
  const modal = source('components/cli/TermModal.vue')
  assert.doesNotMatch(section, /text-transform:\s*uppercase/)
  assert.doesNotMatch(stat, /text-transform:\s*uppercase/)
  assert.doesNotMatch(modal, /text-transform:\s*uppercase/)
})

test('user-facing copy uses 工作臺 / 系統管理, not leftover admin chrome', () => {
  const workbench = source('views/WorkbenchHomeView.vue')
  const header = source('components/layout/AppHeader.vue')
  const sidebar = source('components/layout/AppSidebar.vue')
  const login = source('views/LoginView.vue')
  const entries = source('utils/postLoginDestination.js')
  assert.match(workbench, /早安，開始今天的工作/)
  assert.match(workbench, /人事、採購、總務/)
  assert.match(entries, /人事、採購、總務/)
  assert.match(header, /AnilaAccountMenu/)
  assert.match(sidebar, /label: '人'/)
  assert.match(sidebar, /label: '用量'/)
  assert.match(sidebar, /label: '平台'/)
  assert.match(login, /查規範、問流程、做成文件/)
  assert.match(login, /<TermLogo/)
  assert.doesNotMatch(workbench, /任務中心/)
  assert.doesNotMatch(login, /治理中心/)
  assert.doesNotMatch(login, /cardComponentOrigin \}\}/)
  assert.doesNotMatch(workbench, /DATA PLANE/)
  assert.doesNotMatch(login, /DATA PLANE/)
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
