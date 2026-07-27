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
  runUploadJobs,
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

test('three dropped zips all get queued', () => {
  const { plain, zips } = partitionDroppedFiles([
    { name: 'x.zip' },
    { name: 'y.ZIP' },
    { name: 'z.zip' },
  ])
  assert.equal(plain.length, 0)
  assert.equal(zips.length, 3, '大小寫混用的副檔名也要算 zip')
  assert.equal(expandUploadJobs({ plain, zips }).length, 3)
})

// 派送邏輯住在 uploadDropQueue.js 而不是元件裡,就是為了讓「每個拖進來的檔案都真的
// 送出去」這個性質**被真的執行過**。前一版的接線測試是比對原始碼有沒有出現某些識別字
// —— 一次「保留識別字但又開始丟檔」的重構會照樣綠,而那正是這道 gate 存在的理由。
test('每個檔案都被派送,且都帶著宣告的密等', async () => {
  const calls = []

  await runUploadJobs(
    {
      plain: [{ name: 'a.txt' }, { name: 'b.pdf' }],
      zips: [{ name: 'c.zip' }, { name: 'd.zip' }],
    },
    '機密',
    {
      uploadFiles: async (files, level) =>
        calls.push(['files', files.map((f) => f.name), level]),
      uploadZip: async (zip, level) => calls.push(['zip', zip.name, level]),
    },
  )

  assert.deepEqual(calls, [
    ['files', ['a.txt', 'b.pdf'], '機密'],
    ['zip', 'c.zip', '機密'],
    ['zip', 'd.zip', '機密'],
  ])
})

test('三個 zip 全部送出,一個都不吞', async () => {
  const sent = []

  await runUploadJobs({ plain: [], zips: [{ name: 'x.zip' }, { name: 'y.zip' }, { name: 'z.zip' }] }, '機密', {
    uploadFiles: async () => assert.fail('沒有一般檔案時不該呼叫檔案傳輸'),
    uploadZip: async (zip, level) => sent.push([zip.name, level]),
  })

  assert.deepEqual(sent, [['x.zip', '機密'], ['y.zip', '機密'], ['z.zip', '機密']])
})

test('元件的確認處理器只是薄殼,派送走共用模組', () => {
  assert.ok(
    source.includes('runUploadJobs'),
    'confirmPendingUpload 應呼叫 runUploadJobs,派送邏輯不可回到元件內',
  )
  assert.ok(
    !/const jobs = expandUploadJobs/.test(source),
    '元件不該自己展開工作清單 —— 那條路徑沒有測試覆蓋',
  )
})
