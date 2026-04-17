import client from './client'

export const listAlerts = (params) =>
  client.get('/api/alerts', { params })

export const getAlertSummary = () =>
  client.get('/api/alerts/summary')

export const acknowledgeAlert = (id, note = '') =>
  client.post(`/api/alerts/${id}/ack`, { note })

export const resolveAlert = (id, note = '') =>
  client.post(`/api/alerts/${id}/resolve`, { note })
