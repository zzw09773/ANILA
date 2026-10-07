import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

function read(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

test('人資資料庫頁的密碼是密碼框，路由與側欄都叫人資資料庫', () => {
  const view = read('../src/views/HrDatabaseView.vue')
  const router = read('../src/router/index.js')
  const sidebar = read('../src/components/layout/AppSidebar.vue')
  const header = read('../src/components/layout/AppHeader.vue')
  const users = read('../src/views/UsersView.vue')

  assert.match(view, /type="password"/)
  assert.match(view, /autocomplete="new-password"/)
  assert.doesNotMatch(view, /has_password\s*\}\}/)
  assert.match(view, /測試連線/)
  assert.match(view, /根單位名稱/)
  assert.match(view, /主管自動成為單位管理員/)
  assert.match(view, /人資有職稱的人，成為自己那個單位的單位管理員，可以看單位的成員與用量。主管不占「每單位最多三名」的名額，那個上限只算指派的人。/)
  assert.match(view, /主管自動取得降密審批權責/)
  assert.match(view, /人資有職稱的人取得降密審批權。申請人不能核自己的申請。/)
  assert.match(view, /只限這些職稱（選填）/)
  assert.match(view, /留空表示任何職稱都算/)
  assert.match(view, /auto_unit_admin/)
  assert.match(view, /auto_declass/)
  assert.doesNotMatch(view, /單位管理員職稱/)
  assert.doesNotMatch(view, /留空表示不依職稱授與/)
  assert.match(view, /root_unit_name/)
  assert.match(view, /placementNote/)
  assert.match(view, /只有擁有者可以變更這些設定。/)
  assert.match(view, /authStore\.isOwner/)
  assert.match(view, /v-if="authStore\.isOwner"/)
  assert.match(router, /hr-database/)
  assert.match(router, /requiresAdmin:\s*true/)
  assert.match(sidebar, /path:\s*'\/hr-database',\s*label:\s*'人資資料庫'/)
  assert.match(header, /'\/hr-database':\s*'人資資料庫'/)
  assert.match(users, /department_source/)
  assert.match(users, /hr_titles/)
  assert.match(users, /（人資）/)
})
