// 測試用 client 替身：走跟正式 feedback.js 同一條 client.get 形狀。
// 正文不快取；每次 get 都進 calls。

export const calls = []

let listImpl = async () => ({ data: { items: [], summary: { total: 0, up: 0, down: 0, with_comment: 0 } } })
let convImpl = async () => ({ data: { messages: [] } })
let postImpl = async () => ({ data: { count: 0 } })
let meImpl = async () => ({ data: { role: 'admin', username: 'admin' } })

export function resetFeedbackClient() {
  calls.length = 0
  listImpl = async () => ({ data: { items: [], summary: { total: 0, up: 0, down: 0, with_comment: 0 } } })
  convImpl = async () => ({ data: { messages: [] } })
  postImpl = async () => ({ data: { count: 0 } })
  meImpl = async () => ({ data: { role: 'admin', username: 'admin' } })
}

export function setListImpl(fn) {
  listImpl = fn
}

export function setConvImpl(fn) {
  convImpl = fn
}

export function setPostImpl(fn) {
  postImpl = fn
}

export function setMeImpl(fn) {
  meImpl = fn
}

export default {
  get(url, config) {
    calls.push({ method: 'get', url, params: config?.params || {} })
    const path = String(url)
    if (path.includes('/api/auth/me')) return meImpl()
    if (path.includes('/api/admin/feedback')) return listImpl(url, config)
    if (path.includes('/api/conversations/')) return convImpl(url, config)
    return Promise.reject(Object.assign(new Error(`unmocked GET ${path}`), {
      response: { status: 500, data: { detail: `unmocked GET ${path}` } },
    }))
  },
  post(url, body) {
    calls.push({ method: 'post', url, body: body ?? null })
    return postImpl(url, body)
  },
}
