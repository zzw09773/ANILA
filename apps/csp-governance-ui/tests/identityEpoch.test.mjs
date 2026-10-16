import assert from 'node:assert/strict'
import test from 'node:test'
import { createIdentityEpochBridge } from '../src/api/identityEpoch.js'

const nonce = 'aaaabbbb-cccc-4ddd-8eee-000000000001'
const other = 'aaaabbbb-cccc-4ddd-8eee-000000000002'

function browser({ deniedStorage = false, deniedChannel = false, deniedCrypto = false } = {}) {
  const target = new EventTarget()
  target.CustomEvent = CustomEvent
  target.writes = []
  target.messages = []
  target.crypto = { randomUUID() { if (deniedCrypto) throw new Error('unavailable'); return nonce } }
  target.localStorage = { setItem(key, value) {
    if (deniedStorage) throw new Error('unavailable')
    target.writes.push({ key, value })
  } }
  target.BroadcastChannel = class {
    constructor(name) { if (deniedChannel) throw new Error('unavailable'); this.name = name; target.channel = this }
    postMessage(value) { target.messages.push(value) }
    close() { this.closed = true }
  }
  target.identities = []
  target.addEventListener('anila:identity', (event) => target.identities.push(event.detail.userId))
  return target
}

function storage(target, value, key = 'anila:identity-epoch') {
  const event = new Event('storage')
  Object.assign(event, { key, newValue: typeof value === 'string' ? value : JSON.stringify(value) })
  target.dispatchEvent(event)
}

test('local identity mutation immediately invalidates and publishes only an opaque epoch', () => {
  const target = browser(), bridge = createIdentityEpochBridge(target)
  const reasons = []
  bridge.subscribe((reason) => reasons.push(reason))
  bridge.announce(41)
  bridge.invalidate()
  assert.deepEqual(target.identities, [41, null])
  assert.deepEqual(reasons, ['local'])
  assert.equal(bridge.current(), 1)
  assert.deepEqual(JSON.parse(target.writes[0].value), { version: 1, epoch: nonce })
  assert.deepEqual(target.messages, [{ version: 1, epoch: nonce }])
  bridge.close()
})

test('another tab notification clears display before asking subscribers to reauthenticate', () => {
  const target = browser(), bridge = createIdentityEpochBridge(target)
  bridge.announce(41)
  bridge.subscribe((reason) => {
    assert.equal(reason, 'remote')
    assert.equal(target.identities.at(-1), null)
  })
  storage(target, { version: 1, epoch: other })
  assert.equal(bridge.current(), 1)
  assert.deepEqual(target.identities, [41, null])
  assert.equal(target.writes.length, 0)
  assert.equal(target.messages.length, 0)
  bridge.close()
})

test('storage and channel copies of the same notification do not trigger a feedback loop', () => {
  const target = browser(), bridge = createIdentityEpochBridge(target)
  const value = { version: 1, epoch: other }
  storage(target, value)
  target.channel.onmessage({ data: value })
  assert.equal(bridge.current(), 1)
  assert.deepEqual(target.identities, [null])
  assert.equal(target.messages.length, 0)
  bridge.close()
})

test('own broadcast echo cannot invalidate a freshly confirmed identity a second time', () => {
  const target = browser(), bridge = createIdentityEpochBridge(target)
  bridge.invalidate()
  bridge.announce(52)
  target.channel.onmessage({ data: { version: 1, epoch: nonce } })
  assert.equal(bridge.current(), 1)
  assert.deepEqual(target.identities, [null, 52])
  bridge.close()
})

test('malformed or forged actor-bearing notifications never grant identity', () => {
  const target = browser(), bridge = createIdentityEpochBridge(target)
  const invalid = [null, {}, { version: 2, epoch: nonce }, { version: 1, epoch: 'bad' },
    { version: 1, epoch: nonce, userId: 99 }, { version: 1, epoch: 99 }]
  for (const value of invalid) storage(target, value)
  storage(target, '{')
  storage(target, { version: 1, epoch: nonce }, 'other:key')
  assert.equal(bridge.current(), 0)
  assert.deepEqual(target.identities, [])
  bridge.close()
})

test('blocked storage still broadcasts without storing bodies or tokens', () => {
  const target = browser({ deniedStorage: true }), bridge = createIdentityEpochBridge(target)
  bridge.invalidate()
  assert.equal(bridge.current(), 1)
  assert.equal(target.writes.length, 0)
  assert.deepEqual(target.messages, [{ version: 1, epoch: nonce }])
  bridge.close()
})

test('blocked channel retains storage notifications and local invalidation', () => {
  const target = browser({ deniedChannel: true }), bridge = createIdentityEpochBridge(target)
  bridge.invalidate()
  assert.equal(bridge.current(), 1)
  assert.equal(target.writes.length, 1)
  bridge.close()
})

test('HTTP origins without randomUUID still notify through getRandomValues', () => {
  const target = browser({ deniedStorage: true })
  target.crypto = { getRandomValues: bytes => bytes.fill(0) }
  const bridge = createIdentityEpochBridge(target)
  bridge.invalidate()
  assert.deepEqual(target.messages, [{ version: 1, epoch: '00000000-0000-4000-8000-000000000000' }])
  assert.deepEqual(target.identities, [null])
  bridge.close()
})

test('a rejected randomUUID call can use the remaining safe random generator', () => {
  const target = browser({ deniedCrypto: true })
  target.crypto.getRandomValues = bytes => bytes.fill(0)
  const bridge = createIdentityEpochBridge(target)
  bridge.invalidate()
  assert.equal(target.writes.length, 1)
  assert.equal(target.messages.length, 1)
  bridge.close()
})

test('denied notification facilities do not prevent local fail-closed invalidation', () => {
  const target = browser({ deniedStorage: true, deniedChannel: true, deniedCrypto: true })
  const bridge = createIdentityEpochBridge(target)
  assert.doesNotThrow(() => bridge.invalidate())
  assert.deepEqual(target.identities, [null])
  assert.equal(bridge.current(), 1)
  bridge.close()
})

test('closing and unsubscribing prevents further storage callbacks', () => {
  const target = browser(), bridge = createIdentityEpochBridge(target)
  let calls = 0
  const unsubscribe = bridge.subscribe(() => { calls += 1 })
  unsubscribe()
  storage(target, { version: 1, epoch: other })
  assert.equal(calls, 0)
  bridge.close()
  storage(target, { version: 1, epoch: nonce })
  assert.equal(bridge.current(), 1)
  assert.equal(target.channel.closed, true)
})
