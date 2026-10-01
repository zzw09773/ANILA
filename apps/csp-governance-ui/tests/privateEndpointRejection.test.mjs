import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { privateEndpointAction } from '../src/utils/privateEndpointRejection.js'

function readSource(relative) {
  return readFileSync(new URL(relative, new URL('../src/', import.meta.url)), 'utf8')
}

function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

const HOST_DETAIL = {
  code: 'host_not_trusted',
  host: '172.16.120.35',
  message: '主機 172.16.120.35 還不在信任主機清單',
}

test('只有 host_not_trusted 才給信任主機動作', () => {
  assert.equal(privateEndpointAction('這台平台未允許私有 IP 端點（ANILA_ALLOW_PRIVATE_ENDPOINT）'), null)
  assert.equal(privateEndpointAction({ code: 'untrusted_host', host: 'gemma4' }), null)
  assert.equal(privateEndpointAction({ code: 'host_not_trusted' }), null)
  assert.equal(privateEndpointAction(null), null)

  const owner = privateEndpointAction(HOST_DETAIL, { canManageTrustedHosts: true })
  assert.equal(owner.message, HOST_DETAIL.message)
  assert.equal(owner.host, '172.16.120.35')
  assert.equal(owner.canManage, true)
  assert.equal(owner.buttonLabel, '加入信任主機並重試')
  assert.equal(owner.note, '註冊模型時加入')
  assert.equal(owner.linkTo, '/trusted-hosts')

  const other = privateEndpointAction(HOST_DETAIL, { canManageTrustedHosts: false })
  assert.equal(other.canManage, false)
  assert.equal(other.linkLabel, '前往信任主機')
  assert.equal(other.linkTo, '/trusted-hosts')
})

test('註冊模型與外部服務表單都會依權限重試或連到信任主機', () => {
  const models = stripComments(readSource('views/ModelsView.vue'))
  const external = stripComments(readSource('views/ExternalServicesView.vue'))
  assert.match(models, /privateEndpointAction/)
  assert.match(external, /privateEndpointFromError/)
  assert.doesNotMatch(external, /getRawDetail\(/)
  for (const source of [models, external]) {
    assert.match(source, /createTrustedHost/)
    assert.match(source, /authStore\.isOwner/)
    assert.match(source, /buttonLabel/)
    assert.match(source, /linkTo/)
    assert.match(source, /note: block\.note/)
  }
  assert.match(models, /confirmPrivateEndpointAndRetry/)
  assert.match(external, /addTrustedHostAndRetry/)
  assert.match(external, /retryBody/)
  assert.doesNotMatch(models, /註冊模型時加入/)
  assert.match(readSource('utils/privateEndpointRejection.js'), /註冊模型時加入/)
  assert.match(readSource('api/errors.js'), /privateEndpointFromError/)
})
