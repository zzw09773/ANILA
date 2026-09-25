// 測試用 client 替身：只服務助手下載契約。
// 每個 GET 都記進 calls；未登記的路徑拒絕，不假裝成功。

export const calls = []

const routes = new Map()

export function resetAgentDownloadClient() {
  calls.length = 0
  routes.clear()
}

export function setGetImpl(url, fn) {
  routes.set(url, fn)
}

export default {
  get(url, config) {
    calls.push({ method: 'get', url, config })
    const impl = routes.get(url)
    if (!impl) {
      return Promise.reject(Object.assign(new Error(`unmocked GET ${url}`), {
        response: { status: 500, data: { detail: `unmocked GET ${url}` } },
      }))
    }
    return impl(url, config)
  },
}
