import test from 'node:test'
import assert from 'node:assert/strict'
import {
  locateRatedReply,
  createRatedReplySession,
} from '../src/utils/feedbackRatedReply.js'

const branchMessages = [
  { id: 1, role: 'user', content: '活躍問句', parent_id: null },
  { id: 2, role: 'assistant', content: '活躍分支答（不該出現）', parent_id: 1 },
  { id: 3, role: 'user', content: '被評分那一問', parent_id: null },
  { id: 4, role: 'assistant', content: '被評分的非活躍答', parent_id: 3 },
  { id: 5, role: 'assistant', content: '同層另一答（不該出現）', parent_id: 3 },
]

test('locate：對上 message_id 且必須是 assistant，沿 parent_id 找最近 user', () => {
  const found = locateRatedReply(branchMessages, 4)
  assert.equal(found.ok, true)
  assert.equal(found.rated.content, '被評分的非活躍答')
  assert.equal(found.user.content, '被評分那一問')
  assert.equal(found.user.id, 3)
})

test('locate：不因陣列相鄰或活躍列而誤取', () => {
  const found = locateRatedReply(branchMessages, 4)
  assert.notEqual(found.rated.content, '活躍分支答（不該出現）')
  assert.notEqual(found.rated.content, '同層另一答（不該出現）')
  assert.notEqual(found.user?.content, '活躍問句')
})

test('locate：目標缺失或 role 不是 assistant → not found', () => {
  assert.equal(locateRatedReply(branchMessages, 99).ok, false)
  assert.equal(locateRatedReply(branchMessages, 3).ok, false)
  assert.equal(locateRatedReply(null, 4).ok, false)
})

test('locate：parent_id 環不會卡住，仍回傳助手正文', () => {
  const cyclic = [
    { id: 10, role: 'assistant', content: '環上的助手', parent_id: 11 },
    { id: 11, role: 'assistant', content: '另一助手', parent_id: 10 },
  ]
  const found = locateRatedReply(cyclic, 10)
  assert.equal(found.ok, true)
  assert.equal(found.rated.content, '環上的助手')
  assert.equal(found.user, null)
})

test('session：伺服器錯誤不得裝成成功', async () => {
  const applied = []
  const session = createRatedReplySession({
    getConversation: async () => {
      throw Object.assign(new Error('denied'), {
        response: { status: 403, data: { detail: '權限不足' } },
      })
    },
  })
  const result = await session.open({ conversation_id: 9, message_id: 4 }, (s) => applied.push(s))
  assert.equal(result.state, 'error')
  const last = applied.at(-1)
  assert.equal(last.error, '權限不足')
  assert.equal(last.rated, null)
  assert.equal(last.loading, false)
})

test('session：關閉後過期的 GET 不得覆寫', async () => {
  let release
  const hold = new Promise((resolve) => { release = resolve })
  const applied = []
  const session = createRatedReplySession({
    getConversation: async () => {
      await hold
      return { data: { messages: branchMessages } }
    },
  })
  const pending = session.open({ conversation_id: 1, message_id: 4 }, (s) => applied.push({ ...s }))
  session.close((s) => applied.push({ ...s }))
  release()
  const result = await pending
  assert.equal(result.stale, true)
  const last = applied.at(-1)
  assert.equal(last.row, null)
  assert.equal(last.rated, null)
  assert.equal(last.loading, false)
})

test('session：改看另一列時，先開的回應不得覆蓋後開的', async () => {
  let releaseA
  const holdA = new Promise((resolve) => { releaseA = resolve })
  const applied = []
  const session = createRatedReplySession({
    getConversation: async (id) => {
      if (id === 1) {
        await holdA
        return { data: { messages: branchMessages } }
      }
      return {
        data: {
          messages: [
            { id: 20, role: 'user', content: '第二問', parent_id: null },
            { id: 21, role: 'assistant', content: '第二答', parent_id: 20 },
          ],
        },
      }
    },
  })
  const first = session.open({ conversation_id: 1, message_id: 4 }, (s) => applied.push({ tag: 'a', ...s }))
  const second = session.open({ conversation_id: 2, message_id: 21 }, (s) => applied.push({ tag: 'b', ...s }))
  await second
  releaseA()
  const stale = await first
  assert.equal(stale.stale, true)
  const lastReady = [...applied].reverse().find((s) => s.rated)
  assert.equal(lastReady.rated.content, '第二答')
  assert.notEqual(lastReady.rated.content, '被評分的非活躍答')
})
