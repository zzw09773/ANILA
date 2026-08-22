import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('./WSSidebar.tsx', import.meta.url), 'utf8')

test('來源列與製作共用已索引／總數', () => {
  assert.match(source, /sourceCountLabel/)
  assert.match(source, /已選/)
  assert.match(source, /toggleSource/)
  assert.match(source, /isIndexedSource\(d\.doc\)/)
  assert.doesNotMatch(source, /isIndexed = status === 'indexed'/)
})

test('失敗文件列必須畫出後端記錄的錯誤原因', () => {
  assert.match(source, /jobSnapshot\?\.error_message/)
  assert.match(source, /d\.doc\.error_message/)
  assert.match(source, /失敗.*failureReason/)
  assert.match(source, /maxWidth: 180/)
  assert.match(source, /textOverflow: 'ellipsis'/)
  assert.match(source, /whiteSpace: 'nowrap'/)
  assert.match(source, /title=\{failureReason \?\? undefined\}/)
})
