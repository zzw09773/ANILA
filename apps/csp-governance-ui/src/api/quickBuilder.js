import client from './client'
import { readTestJsonStream, testStreamError } from '../quickBuilder/testStream.js'
const root = '/api/quick-builder'
export const listAssistants = () => client.get(root)
export const getAssistant = (id) => client.get(`${root}/${encodeURIComponent(id)}`)
export const listTemplates = () => client.get(`${root}/templates`)
export const getCatalog = () => client.get(`${root}/catalog`)
export const getReadiness = () => client.get(`${root}/readiness`)
export const cancelTestRun = (id, body, options) => client.post(`${root}/runs/${encodeURIComponent(id)}/cancel`, body, options)
export const getRun = (id, options) => client.get(`${root}/runs/${encodeURIComponent(id)}`, options)
export const getTestRuns = (id, params = {}, options = {}) => client.get(`${root}/${encodeURIComponent(id)}/test-runs`, {...options, params})
export const createAssistant = (body, options) => client.post(root, body, options)
export const saveVersion = (id, body, options) => client.post(`${root}/${encodeURIComponent(id)}/versions`, body, options)
export const assistantAction = (id, action, body = {}, options) => client.post(`${root}/${encodeURIComponent(id)}/${action}`, body, options)
export const compareVersions = (id, left, right) => client.get(`${root}/${encodeURIComponent(id)}/compare`, { params: { left, right } })

export const createTemplate = (body, options) => client.post(`${root}/templates`, body, options)
export const updateTemplate = (id, body, options) => client.put(`${root}/templates/${id}`, body, options)
export const setCollectionPublication = (id, body, options) => client.put(`${root}/collections/${id}/publication`, body, options)


export async function streamAssistantTest(id, body, options = {}) {
  try {
    const response = await client.post(`${root}/${encodeURIComponent(id)}/test`, {...body, stream: true}, {
      ...options, adapter: 'fetch', responseType: 'stream',
      headers: {...options.headers, Accept: 'text/event-stream'}
    });
    const contentType = response.headers?.get?.('content-type') || response.headers?.['content-type'] || '';
    if (contentType.toLowerCase().includes('text/event-stream')) return response;
    if (!contentType.toLowerCase().includes('application/json')) throw testStreamError();
    return {...response, data: await readTestJsonStream(response.data, {signal: options.signal})};
  } catch (error) {
    if (typeof error.response?.data?.getReader === 'function') {
      try { error.response.data = await readTestJsonStream(error.response.data, {signal: options.signal, maxBytes: 64 * 1024}); }
      catch { error.response.data = {}; }
    }
    throw error;
  }
}
