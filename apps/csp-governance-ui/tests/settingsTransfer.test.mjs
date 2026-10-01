import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { formatPlanItem } from '../src/utils/settingsTransfer.js'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const view = readFileSync(resolve(root, 'src/views/SettingsOverviewView.vue'), 'utf8')
const api = readFileSync(resolve(root, 'src/api/settingsTransfer.js'), 'utf8')

test('平台設定頁可以匯出、試算匯入、再套用', () => {
  assert.match(view, /匯出設定/)
  assert.match(view, /匯入設定/)
  assert.match(view, /套用/)
  assert.match(view, /exportSettings/)
  assert.match(view, /importSettings\(parsed, \{ dryRun: true \}/)
  assert.match(view, /importSettings\(importDocument\.value, \{ dryRun: false \}/)
  assert.match(view, /formatPlanItem/)
  assert.match(api, /\/api\/admin\/settings-export/)
  assert.match(api, /\/api\/admin\/settings-import/)
  assert.match(api, /dry_run: dryRun/)
})

test('試算結果用中文說明每一筆', () => {
  assert.equal(
    formatPlanItem({ entity: 'model', key: 'gemma', label: 'Gemma', action: 'needs-credential', message: '需另外填入金鑰' }),
    '模型 Gemma：需另外填入金鑰',
  )
  assert.equal(
    formatPlanItem({ entity: 'trusted_host', key: 'gemma4', label: 'gemma4', action: 'create', message: '' }),
    '信任主機 gemma4：新增',
  )
  assert.equal(
    formatPlanItem({
      entity: 'model_role',
      key: 'platform_embedding',
      label: '平台嵌入模型',
      action: 'skip',
      message: '平台嵌入模型要在端點可連線時於「模型角色」指定，匯入不會代替連線量測',
    }),
    '模型角色 平台嵌入模型：略過（平台嵌入模型要在端點可連線時於「模型角色」指定，匯入不會代替連線量測）',
  )
  assert.equal(
    formatPlanItem({ entity: 'external_service', key: 'speech', label: '語音辨識', action: 'skip', message: '' }),
    '外部服務 語音辨識：略過',
  )
})
