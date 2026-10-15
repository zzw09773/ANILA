import client from './client'
const root = '/api/quick-builder'
export const listAssistants = () => client.get(root)
export const getAssistant = (id) => client.get(`${root}/${encodeURIComponent(id)}`)
export const listTemplates = () => client.get(`${root}/templates`)
export const getCatalog = () => client.get(`${root}/catalog`)
export const createAssistant = (body, options) => client.post(root, body, options)
export const saveVersion = (id, body, options) => client.post(`${root}/${encodeURIComponent(id)}/versions`, body, options)
export const assistantAction = (id, action, body = {}, options) => client.post(`${root}/${encodeURIComponent(id)}/${action}`, body, options)
export const compareVersions = (id, left, right) => client.get(`${root}/${encodeURIComponent(id)}/compare`, { params: { left, right } })

export const createTemplate = (body, options) => client.post(`${root}/templates`, body, options)
export const updateTemplate = (id, body, options) => client.put(`${root}/templates/${id}`, body, options)
export const setCollectionPublication = (id, body, options) => client.put(`${root}/collections/${id}/publication`, body, options)
