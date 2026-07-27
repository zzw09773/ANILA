// W2-11:上傳流程必須有密等確認步驟,並明示繼承來源。
// Mixed drag-and-drop must queue every plain file and every zip.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  expandUploadJobs,
  partitionDroppedFiles,
} from '../src/utils/uploadDropQueue.js'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const source = readFileSync(
  join(ROOT, 'src/views/CollectionDetailView.vue'),
  'utf8',
)

test('上傳流程含密等確認步驟且顯示繼承來源', () => {
  assert.ok(
    source.includes('確認文件密等'),
    '應有密等確認 modal 標題',
  )
  assert.ok(
    source.includes('繼承自知識庫'),
    '應明示密等繼承來源',
  )
  assert.ok(
    source.includes('uploadClassificationDraft'),
    '應允許上調文件密等',
  )
  assert.ok(
    source.includes('uploadWouldLatch') || source.includes('整個知識庫'),
    '上調時應警告會閂鎖整個知識庫',
  )
  assert.ok(
    source.includes('declassification-requests') || source.includes('雙人降密'),
    '應指出雙人降密是唯一救濟',
  )
  assert.ok(
    source.includes('openUploadConfirm') || source.includes('pendingUpload'),
    '上傳前應先進入確認閘,不可直接送出',
  )
})

test('mixed drag-and-drop queues 2 plain files + 2 zips as four upload jobs', () => {
  const dropped = [
    { name: 'a.txt' },
    { name: 'b.pdf' },
    { name: 'c.zip' },
    { name: 'd.zip' },
  ]
  const { plain, zips } = partitionDroppedFiles(dropped)
  assert.equal(plain.length, 2)
  assert.equal(zips.length, 2)
  const jobs = expandUploadJobs({ plain, zips })
  assert.equal(jobs.length, 4)
  assert.deepEqual(
    jobs.map((j) => [j.type, j.file.name]),
    [
      ['file', 'a.txt'],
      ['file', 'b.pdf'],
      ['zip', 'c.zip'],
      ['zip', 'd.zip'],
    ],
  )
})

test('CollectionDetailView wires drop partition into confirm → all jobs', () => {
  assert.ok(
    source.includes('partitionDroppedFiles'),
    'onDrop 應呼叫 partitionDroppedFiles',
  )
  assert.ok(
    source.includes('expandUploadJobs'),
    'confirm 應展開 plain + zips 全部工作',
  )
  assert.ok(
    source.includes('doZipUpload(zip, level)') ||
      source.includes('doZipUpload(zip,'),
    'zip 路徑應帶密等宣告',
  )
})
