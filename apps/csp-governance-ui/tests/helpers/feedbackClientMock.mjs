// 測試用 client 替身：走跟正式 feedback.js 同一條 client.get 形狀。
// 正文不快取；每次 get 都進 calls。

export const calls = []

let listImpl = async () => ({ data: { items: [], summary: { total: 0, up: 0, down: 0, with_comment: 0 } } })
let convImpl = async () => ({ data: { messages: [] } })

export function resetFeedbackClient() {
  calls.length = 0
  listImpl = async () => ({ data: { items: [], summary: { total: 0, up: 0, down: 0, with_comment: 0 } } })
  convImpl = async () => ({ data: { messages: [] } })
}

export function setListImpl(fn) {
  listImpl = fn
}

export function setConvImpl(fn) {
  convImpl = fn
}

export default {
  get(url, config) {
    calls.push({ url, params: config?.params || {} })
    const path = String(url)
    if (path.includes('/api/admin/feedback')) return listImpl(url, config)
    if (path.includes('/api/conversations/')) return convImpl(url, config)
    return Promise.reject(Object.assign(new Error(`unmocked GET ${path}`), {
      response: { status: 500, data: { detail: `unmocked GET ${path}` } },
    }))
  },
}
