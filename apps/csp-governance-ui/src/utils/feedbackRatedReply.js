/**
 * 從 view=all 的訊息樹裡找出「被評分的助手回覆」與其最近的使用者祖先。
 * 只認 message_id ＋ assistant role，再沿 parent_id 往上走。
 * 不用陣列相鄰列，也不猜 active branch。
 */

import { extractError } from '../api/errors.js'

export function idsEqual(a, b) {
  if (a === b) return true
  if (a == null || b == null) return false
  const na = Number(a)
  const nb = Number(b)
  return Number.isFinite(na) && Number.isFinite(nb) && na === nb
}

/**
 * @param {unknown} messages
 * @param {unknown} messageId
 * @returns {{ ok: boolean, rated: object | null, user: object | null }}
 */
export function locateRatedReply(messages, messageId) {
  const list = Array.isArray(messages) ? messages : []
  const byId = new Map()
  for (const msg of list) {
    if (msg && msg.id != null) byId.set(msg.id, msg)
    if (msg && typeof msg.id === 'number') byId.set(String(msg.id), msg)
  }
  let rated = null
  for (const msg of list) {
    if (msg && idsEqual(msg.id, messageId) && msg.role === 'assistant') {
      rated = msg
      break
    }
  }
  if (!rated) return { ok: false, rated: null, user: null }

  const seen = new Set()
  let parentId = rated.parent_id
  let user = null
  while (parentId != null) {
    if (seen.has(parentId)) break
    seen.add(parentId)
    const parent = byId.get(parentId) || byId.get(String(parentId)) || byId.get(Number(parentId))
    if (!parent) break
    if (parent.role === 'user') {
      user = parent
      break
    }
    parentId = parent.parent_id
  }
  return { ok: true, rated, user }
}

/**
 * 一次點擊對應一次序號。關閉或改看另一列時加序號，
 * 過期的 GET 完成後不得覆寫目前畫面。不快取正文。
 */
export function createRatedReplySession({ getConversation }) {
  let seq = 0

  function snapshotEmpty(row = null) {
    return {
      row,
      loading: false,
      error: '',
      notFound: false,
      rated: null,
      user: null,
    }
  }

  async function open(row, apply) {
    const token = ++seq
    apply({
      row,
      loading: true,
      error: '',
      notFound: false,
      rated: null,
      user: null,
    })
    try {
      const res = await getConversation(row.conversation_id)
      if (token !== seq) return { stale: true, token }
      const messages = res?.data?.messages ?? res?.messages
      const found = locateRatedReply(messages, row.message_id)
      if (!found.ok) {
        apply({
          row,
          loading: false,
          error: '',
          notFound: true,
          rated: null,
          user: null,
        })
        return { stale: false, token, state: 'not-found' }
      }
      apply({
        row,
        loading: false,
        error: '',
        notFound: false,
        rated: found.rated,
        user: found.user,
      })
      return { stale: false, token, state: 'ready' }
    } catch (err) {
      if (token !== seq) return { stale: true, token }
      apply({
        row,
        loading: false,
        error: extractError(err, '載入被評分回覆失敗'),
        notFound: false,
        rated: null,
        user: null,
      })
      return { stale: false, token, state: 'error' }
    }
  }

  function close(apply) {
    seq += 1
    apply(snapshotEmpty(null))
  }

  return { open, close, currentSeq: () => seq }
}
