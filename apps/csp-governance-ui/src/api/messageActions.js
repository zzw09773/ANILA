import client from './client'

export const listMessageActions = () =>
  client.get('/api/message-actions')

export const listActionIcons = () =>
  client.get('/api/message-actions/icons')

export const createMessageAction = (data) =>
  client.post('/api/message-actions', data)

export const updateMessageAction = (id, data) =>
  client.put(`/api/message-actions/${id}`, data)

export const deleteMessageAction = (id) =>
  client.delete(`/api/message-actions/${id}`)

export const getActionBindings = (id) =>
  client.get(`/api/message-actions/${id}/bindings`)

export const replaceActionBindings = (id, bindings) =>
  client.put(`/api/message-actions/${id}/bindings`, { bindings })

export const exportMessageActionAudit = (params) =>
  client.get('/api/message-actions/audit/export', {
    params,
    responseType: 'blob',
  })
