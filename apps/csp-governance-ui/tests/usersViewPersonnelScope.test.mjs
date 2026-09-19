// 單位管理員＝人事與用量：UsersView 不得畫出後端只准 admin 的入口。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const usersView = readFileSync(
  new URL('../src/views/UsersView.vue', import.meta.url),
  'utf8',
)
const routerSrc = readFileSync(
  new URL('../src/router/index.js', import.meta.url),
  'utf8',
)
const sidebarSrc = readFileSync(
  new URL('../src/components/layout/AppSidebar.vue', import.meta.url),
  'utf8',
)

function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

test('UsersView 註解寫明單位管理員＝人事與用量', () => {
  assert.match(usersView, /單位管理員範圍/)
  assert.match(usersView, /人事與用量/)
})

test('admin-only 入口對單位管理員不畫', () => {
  const src = stripComments(usersView)
  assert.match(src, /v-if="canAdminUsers"[\s\S]*新增使用者/)
  assert.match(src, /v-if="canAdminUsers"[\s\S]*openEditModal/)
  assert.match(src, /v-if="canAdminUsers"[\s\S]*openRouterModelsModal/)
  assert.match(src, /v-if="canAdminUsers"[\s\S]*#more/)
  assert.match(src, /openHardDeleteModal/)
})

test('人事動作走單位管理員可用的端點', () => {
  const src = stripComments(usersView)
  assert.match(src, /handleApprove/)
  assert.match(src, /handleDeactivate/)
  assert.match(src, /handleActivate/)
  assert.match(src, /reactivateUser/)
  assert.doesNotMatch(
    src,
    /updateUser\(user\.id, \{ is_active: true \}\)/,
  )
})

test('單位管理員可進 /users，側欄只給使用者頁', () => {
  assert.match(routerSrc, /requiresPersonnel/)
  assert.match(sidebarSrc, /isUnitAdmin && !authStore.isAdmin/)
  assert.match(sidebarSrc, /人事與用量/)
})
