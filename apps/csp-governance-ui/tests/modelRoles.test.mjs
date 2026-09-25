import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { eligibleRoleModels, roleWarning } from '../src/utils/modelRoles.js'

const here = dirname(fileURLToPath(import.meta.url))

test('dropdown only lists active models of the role type', () => {
  const models = [
    { id: 1, name: 'off', model_type: 'llm', is_active: false },
    { id: 2, name: 'embed', model_type: 'embedding', is_active: true },
    { id: 3, name: 'chat', model_type: 'llm', is_active: true },
    { id: 4, name: 'anila-router', model_type: 'llm', is_active: true },
  ]
  assert.deepEqual(
    eligibleRoleModels(models, ['llm']).map((m) => m.name),
    ['chat'],
  )
})

test('unset and inactive roles carry a warning', () => {
  assert.equal(roleWarning({ status: 'ok', message: null }), '')
  assert.equal(
    roleWarning({ status: 'unset', message: '簡報模型尚未在治理中心設定' }),
    '簡報模型尚未在治理中心設定',
  )
  assert.equal(
    roleWarning({ status: 'inactive', message: '視覺模型已停用，請在治理中心改選啟用中的模型' }),
    '視覺模型已停用，請在治理中心改選啟用中的模型',
  )
})

test('models page mounts the role panel', () => {
  const view = readFileSync(join(here, '../src/views/ModelsView.vue'), 'utf8')
  const panel = readFileSync(join(here, '../src/components/ModelRolesPanel.vue'), 'utf8')
  const api = readFileSync(join(here, '../src/api/models.js'), 'utf8')
  assert.match(view, /ModelRolesPanel/)
  assert.match(panel, /模型角色/)
  assert.match(panel, /listModelRoles/)
  assert.match(panel, /assignModelRole/)
  assert.match(panel, /clearModelRole/)
  assert.match(panel, /roleWarning/)
  assert.match(panel, /roleAudience/)
  assert.match(panel, /grantModelRoleAllUsers/)
  assert.match(panel, /授權給所有使用者/)
  assert.match(api, /export const grantModelRoleAllUsers/)
  assert.match(api, /\/api\/models\/roles\/\$\{role\}\/grant-all-users/)
})

test('end-user roles without an all-users grant warn in the same panel', async () => {
  const { roleAudience } = await import('../src/utils/modelRoles.js')
  assert.equal(typeof roleAudience, 'function')
  const open = roleAudience({
    role: 'knowledge_chat',
    end_user_credential: true,
    model: { id: 3, name: 'dept-llm' },
    all_users_grant: false,
  })
  assert.equal(open.granted, false)
  assert.match(open.warning, /所有使用者/)
  const granted = roleAudience({
    role: 'slides',
    end_user_credential: true,
    model: { id: 4 },
    all_users_grant: true,
  })
  assert.equal(granted.granted, true)
  assert.equal(granted.warning, '')
  assert.equal(
    roleAudience({
      role: 'platform_embedding',
      end_user_credential: false,
      model: { id: 1 },
      all_users_grant: false,
    }),
    null,
  )
  assert.equal(
    roleAudience({
      role: 'knowledge_chat',
      end_user_credential: true,
      model: null,
      all_users_grant: false,
    }),
    null,
  )
})
