import client from './client'

export const listTrustedHosts = () =>
  client.get('/api/trusted-hosts')

export const createTrustedHost = (data) =>
  client.post('/api/trusted-hosts', data)

export const deleteTrustedHost = (id) =>
  client.delete(`/api/trusted-hosts/${id}`)
