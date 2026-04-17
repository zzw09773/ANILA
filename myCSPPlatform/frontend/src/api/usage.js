import client from './client'

export const getUsageSummary = (params) =>
  client.get('/api/usage/summary', { params })

export const getUsageChart = (params) =>
  client.get('/api/usage/chart', { params })

export const getTopModels = (params) =>
  client.get('/api/usage/top-models', { params })

export const getTopUsers = (params) =>
  client.get('/api/usage/top-users', { params })

export const getTopDepartments = (params) =>
  client.get('/api/usage/top-departments', { params })

export const exportUsageCsv = (params) =>
  client.get('/api/usage/export', { params, responseType: 'blob' })
