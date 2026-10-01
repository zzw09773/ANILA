import client from './client'

export const listModelPrices = (modelId) =>
  client.get(`/api/models/${modelId}/prices`)

export const addModelPrice = (modelId, data) =>
  client.post(`/api/models/${modelId}/prices`, data)

export const getBillingCurrency = () =>
  client.get('/api/billing/currency')

export const setBillingCurrency = (currency) =>
  client.put('/api/billing/currency', { currency })
