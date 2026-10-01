/** Shared axios stand-in. The ModelsView bundle and the test import this same module. */

export const calls = []

let createBehavior = 'ok'
let createGate = null
let trustGate = null

export function resetModelFormClient() {
  calls.length = 0
  createBehavior = 'ok'
  createGate = null
  trustGate = null
}

export function holdNextCreate() {
  createBehavior = 'hold'
  let release
  const gate = new Promise((resolve) => { release = resolve })
  createGate = { gate, release }
  return createGate
}

export function rejectCreateAsHostNotTrusted() {
  createBehavior = 'host-not-trusted'
}

export function holdNextTrustedHost() {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  trustGate = { gate, release }
  return trustGate
}

export function postCount(url) {
  return calls.filter((call) => call.method === 'post' && call.url === url).length
}

function hostNotTrustedError() {
  const err = new Error('host not trusted')
  err.response = {
    status: 400,
    data: {
      detail: {
        code: 'host_not_trusted',
        host: '10.1.2.3',
        message: '主機 10.1.2.3 還不在信任主機清單',
      },
    },
  }
  return err
}

const client = {
  async get(url) {
    calls.push({ method: 'get', url })
    if (url === '/api/auth/me') {
      return { data: { id: 1, username: 'owner', role: 'owner', is_active: true } }
    }
    if (url === '/api/endpoint-authors/me') {
      return { data: { can_set_endpoint_address: true } }
    }
    return { data: [] }
  },
  async post(url, body) {
    calls.push({ method: 'post', url, body })
    if (url === '/api/models' && createBehavior === 'hold') {
      const data = await createGate.gate
      return { data: data ?? { id: 9, router_enabled: false, name: body?.name } }
    }
    if (url === '/api/models' && createBehavior === 'host-not-trusted') {
      throw hostNotTrustedError()
    }
    if (url === '/api/trusted-hosts' && trustGate) {
      await trustGate.gate
      return { data: { id: 1, ...(body || {}) } }
    }
    return { data: { id: 9, router_enabled: false, ...(body || {}) } }
  },
  async put(url, body) {
    calls.push({ method: 'put', url, body })
    return { data: { id: 9, router_enabled: false, ...(body || {}) } }
  },
  async delete(url) {
    calls.push({ method: 'delete', url })
    return { data: {} }
  },
}

export default client
