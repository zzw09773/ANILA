import { test } from 'node:test'
import assert from 'node:assert/strict'
import { grantsLoadResult, canReplaceRouterGrants } from '../src/utils/routerGrantsLoad.js'

test('failed GET is not an empty grant list', () => {
  const failed = grantsLoadResult(false, [])
  assert.equal(failed.state, 'failed')
  assert.equal(canReplaceRouterGrants(failed.state), false)
})

test('successful empty list may replace', () => {
  const empty = grantsLoadResult(true, [])
  assert.equal(empty.state, 'ready')
  assert.equal(canReplaceRouterGrants(empty.state), true)
  assert.deepEqual(empty.grants, [])
})

test('successful campus grant list may replace', () => {
  const loaded = grantsLoadResult(true, [{ scope_type: 'all' }])
  assert.equal(canReplaceRouterGrants(loaded.state), true)
  assert.equal(loaded.grants.length, 1)
})
