// 代理管理員可以進用量頁；路由守衛與側欄用同一份頁面清單。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { deputyMayOpen } from '../src/router/deputyPages.js'

const routerSrc = readFileSync(
  new URL('../src/router/index.js', import.meta.url),
  'utf8',
)
const sidebarSrc = readFileSync(
  new URL('../src/components/layout/AppSidebar.vue', import.meta.url),
  'utf8',
)

test('路由守衛放行代理管理員的用量頁，其餘頁回首頁', () => {
  assert.match(routerSrc, /deputyMayOpen\(to\.path/)
  const deputy = { isDeputy: true, isAdmin: false }
  assert.equal(deputyMayOpen('/usage', deputy), true)
  assert.equal(deputyMayOpen('/', deputy), true)
  assert.equal(deputyMayOpen('/audit-logs', deputy), false)
  assert.equal(deputyMayOpen('/models', deputy), false)
  assert.equal(deputyMayOpen('/audit-logs', { isDeputy: false, isAdmin: true }), true)
  assert.equal(deputyMayOpen('/usage', { isDeputy: true, isAdmin: true }), true)
})

test('代理管理員的側欄有用量', () => {
  const start = sidebarSrc.indexOf('isDeputy && !authStore.isAdmin')
  const end = sidebarSrc.indexOf('const groups', start)
  const deputyMenu = sidebarSrc.slice(start, end)
  assert.match(deputyMenu, /path: '\/usage'/)
})
