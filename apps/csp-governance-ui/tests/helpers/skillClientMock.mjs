export const calls = []

let listImpl = async () => ({ data: { pending: [], published: [] } })

export function resetSkillClient() {
  calls.length = 0
  listImpl = async () => ({ data: { pending: [], published: [] } })
}

export function setSkillList(impl) {
  listImpl = impl
}

const client = {
  async get(url) {
    calls.push({ method: 'get', url })
    return listImpl(url)
  },
  async post(url, body) {
    calls.push({ method: 'post', url, body })
    return { data: { ok: true } }
  },
}

export default client
