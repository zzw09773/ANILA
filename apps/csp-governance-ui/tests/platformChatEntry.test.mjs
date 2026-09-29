import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import {
  isPlatformChatEntry,
  ordinaryModels,
  platformChatEntryCard,
} from '../src/utils/platformChatEntry.js'

const here = dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(join(here, rel), 'utf8')

const EXPLANATION =
  '平台對話入口，用哪個模型回答由上方主要模型決定；不是助手，也不能當一般模型編輯'

test('ordinary model list omits the platform chat entry', () => {
  const models = [
    { id: 1, name: 'anila-router', display_name: 'ANILA', is_active: true },
    { id: 2, name: 'glm', display_name: 'GLM', is_active: true },
  ]
  assert.deepEqual(
    ordinaryModels(models).map((model) => model.name),
    ['glm'],
  )
  assert.equal(isPlatformChatEntry(models[0]), true)
  assert.equal(isPlatformChatEntry(models[1]), false)
})

test('platform chat entry card is ANILA, with status and the router-primary model', () => {
  const card = platformChatEntryCard([
    { id: 1, name: 'anila-router', display_name: '自訂名稱', is_active: true },
    {
      id: 2,
      name: 'glm',
      display_name: 'GLM 快閃',
      is_router_primary: true,
      is_active: true,
    },
  ])
  assert.equal(card.title, '平台對話入口：ANILA')
  assert.equal(card.statusLabel, '啟用')
  assert.equal(card.active, true)
  assert.equal(card.mainModelLabel, 'GLM 快閃')
  assert.equal(card.explanation, EXPLANATION)
})

test('inactive entry and a missing primary are labeled without controls', () => {
  const card = platformChatEntryCard([
    { id: 1, name: 'anila-router', display_name: 'ANILA', is_active: false },
  ])
  assert.equal(card.statusLabel, '停用')
  assert.equal(card.active, false)
  assert.equal(card.mainModelLabel, '尚未設定')
  assert.equal(card.explanation, EXPLANATION)
})

test('a missing registry row is not shown as active', () => {
  const card = platformChatEntryCard([])
  assert.equal(card.title, '平台對話入口：ANILA')
  assert.equal(card.statusLabel, '尚未建立')
  assert.equal(card.active, false)
  assert.equal(card.mainModelLabel, '尚未設定')
})

test('models page card has no edit controls and the table lists ordinary models', () => {
  const view = read('../src/views/ModelsView.vue')
  assert.match(view, /platformChatEntryCard/)
  assert.match(view, /v-for="model in registeredModels"/)
  assert.doesNotMatch(view, /v-for="model in modelsStore\.models"/)
  assert.equal(view.includes('ANILA 自動選助手'), false)
  assert.match(view, /這是平台入口「ANILA」，不必另設金鑰/)
  const start = view.indexOf('data-platform-chat-entry')
  assert.ok(start >= 0, '平台對話入口卡片沒有 data-platform-chat-entry')
  assert.ok(start < view.indexOf('<ModelRolesPanel'), '卡片要在頁面頂端（角色面板之前）')
  // 卡片放在頁首正下方；只看到它自己的 </TermBox> 為止。
  const end = view.indexOf('</TermBox>', start)
  assert.ok(end > start)
  const card = view.slice(start, end)
  assert.match(card, /平台對話入口：ANILA/)
  assert.match(card, /主要模型/)
  assert.match(card, /狀態/)
  assert.match(card, /platformEntry\.explanation/)
  assert.match(read('../src/utils/platformChatEntry.js'), new RegExp(EXPLANATION))
  assert.doesNotMatch(card, /<button|term-action|@click|可使用對象|編輯|刪除/)
})

test('usage model menu skips the platform chat entry', () => {
  const view = read('../src/views/UsageView.vue')
  assert.match(view, /isPlatformChatEntry/)
})
