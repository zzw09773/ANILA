import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { DEFAULT_MODEL_MAX_CONCURRENT, MODEL_LIST_REFRESH_MS, modelQueueHot } from '../src/utils/modelConcurrency.js'

function readView() {
  return readFileSync(new URL('../src/views/ModelsView.vue', import.meta.url), 'utf8')
}

test('queue highlight is only when someone is waiting', () => {
  assert.equal(DEFAULT_MODEL_MAX_CONCURRENT, 16)
  assert.equal(MODEL_LIST_REFRESH_MS, 15000)
  assert.equal(modelQueueHot(1), true)
  assert.equal(modelQueueHot(0), false)
  assert.equal(modelQueueHot(null), false)
  assert.equal(modelQueueHot(undefined), false)
})

test('models page prefills 16 and shows live inflight and queue', () => {
  const view = readView()
  assert.match(view, /DEFAULT_MODEL_MAX_CONCURRENT/)
  assert.match(view, /處理中/)
  assert.match(view, /排隊中/)
  assert.match(view, /modelQueueHot\(model\.queue_length\)/)
  assert.match(view, /MODEL_LIST_REFRESH_MS/)
  assert.match(view, /onUnmounted/)
  assert.match(view, /clearInterval/)
  assert.match(view, /setInterval/)
})
