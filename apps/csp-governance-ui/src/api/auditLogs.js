import client from './client'

export const listAuditLogs = (params) =>
  client.get('/api/audit-logs', { params })
