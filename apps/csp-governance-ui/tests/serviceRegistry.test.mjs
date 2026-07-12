import test from 'node:test'
import assert from 'node:assert/strict'

import { buildPlatformLinkPayload } from '../src/utils/serviceRegistry.js'


const form = {
  name: '  Agent Portal  ',
  url: '  https://agent.example.tw/app  ',
  icon: '  cpu  ',
  description: '  formal agent  ',
  sort_order: 2,
  is_public: false,
  required_roles: ['developer'],
  launch_mode: 'new_tab',
  classification_ceiling: '機密',
  healthcheck_url: '  https://agent.example.tw/health  ',
  service_admin_user_ids: [7],
}


test('registry payload uses entry_url and never sends legacy url', () => {
  const payload = buildPlatformLinkPayload(form, true)

  assert.equal(payload.entry_url, 'https://agent.example.tw/app')
  assert.equal('url' in payload, false)
  assert.equal(payload.name, 'Agent Portal')
  assert.equal(payload.launch_mode, 'new_tab')
})


test('legacy platform-link payload keeps url for compatibility', () => {
  const payload = buildPlatformLinkPayload(form, false)

  assert.equal(payload.url, 'https://agent.example.tw/app')
  assert.equal('entry_url' in payload, false)
  assert.equal('launch_mode' in payload, false)
})
