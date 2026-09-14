import client from './client'

export const searchDirectory = (q, { includeSelf = false, limit = 20 } = {}) =>
  client.get('/api/directory/users', {
    params: { q, include_self: includeSelf, limit },
  })
