import client from './client'

export const listExternalServices = () => client.get('/api/admin/external-services')

export const updateExternalService = (serviceKey, body) => (
  client.put(`/api/admin/external-services/${serviceKey}`, body)
)

export const probeExternalService = (serviceKey) => (
  client.post(`/api/admin/external-services/${serviceKey}/probe`)
)
