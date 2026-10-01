import client from './client'

export const listQuotas = () =>
  client.get('/api/quotas')

export const createQuota = (data) =>
  client.post('/api/quotas', data)

export const updateQuota = (id, data) =>
  client.put(`/api/quotas/${id}`, data)

export const deleteQuota = (id) =>
  client.delete(`/api/quotas/${id}`)

export const quotaPriceCoverage = (params) =>
  client.get('/api/quotas/price-coverage', { params })
