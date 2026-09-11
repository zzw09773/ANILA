import client from './client'

export const listModels = () =>
  client.get('/api/models')

export const createModel = (data) =>
  client.post('/api/models', data)

export const updateModel = (id, data) =>
  client.put(`/api/models/${id}`, data)

export const deleteModel = (id) =>
  client.delete(`/api/models/${id}`)

export const activateModel = (id) =>
  client.post(`/api/models/${id}/activate`)

export const purgeModel = (id) =>
  client.delete(`/api/models/${id}/purge`)

// Slice 6b — Model Gateway hardening。GET 讀最近一次探測結果的五態快照；
// POST /test 觸發主動探測（active probe），回 { health_status, latency_ms }。
export const getModelHealth = (id) =>
  client.get(`/api/models/${id}/health`)

export const testModelConnection = (id) =>
  client.post(`/api/models/${id}/test`)

export const setRouterPrimary = (id) =>
  client.post(`/api/models/${id}/set-router-primary`)

export const unsetRouterPrimary = (id) =>
  client.post(`/api/models/${id}/unset-router-primary`)

// 主簡報模型（slides-primary）— anila-studio 寫簡報用的 LLM，比照 router-primary。
export const setSlidesPrimary = (id) =>
  client.post(`/api/models/${id}/set-slides-primary`)

export const unsetSlidesPrimary = (id) =>
  client.post(`/api/models/${id}/unset-slides-primary`)

// FLUX 主圖像模型（image-primary）— 完全比照 router-primary 三件組寫法。
export const setImagePrimary = (id) =>
  client.post(`/api/models/${id}/set-image-primary`)

export const unsetImagePrimary = (id) =>
  client.post(`/api/models/${id}/unset-image-primary`)

// 主語音辨識 decoder（asr-primary）— 比照 image-primary；位址進 registry，
// 共享密鑰 ASR_DECODER_TOKEN 仍只在 gateway／decoder 環境變數。
export const setAsrPrimary = (id) =>
  client.post(`/api/models/${id}/set-asr-primary`)

export const unsetAsrPrimary = (id) =>
  client.post(`/api/models/${id}/unset-asr-primary`)

// P4.8 — 平台主 embedding（記憶／新建知識庫預設／ingestion-worker）。
export const setPlatformEmbedding = (id) =>
  client.post(`/api/models/${id}/set-platform-embedding`)

export const unsetPlatformEmbedding = (id) =>
  client.post(`/api/models/${id}/unset-platform-embedding`)

export const getPlatformEmbedding = () =>
  client.get('/api/models/platform-embedding')

// P4.6 — 整批帶入上游 /v1/models listing（選已註冊端點的代表列）。
export const importModelsFromEndpoint = (sourceModelId) =>
  client.post('/api/models/import', { source_model_id: sourceModelId })

// P4.6 — 一次啟用「本次帶入」產生的停用列（仍維持預設停用柵欄）。
export const activateCreatedFromImport = (sourceModelId, names) =>
  client.post('/api/models/import/activate-created', {
    source_model_id: sourceModelId,
    names,
  })

// P4.6b — 端點位址設定授權（擁有者逐一指派開發者）。
export const getMyEndpointAuthorStatus = () =>
  client.get('/api/endpoint-authors/me')

export const listEndpointAuthors = () =>
  client.get('/api/endpoint-authors')

export const grantEndpointAuthor = (userId) =>
  client.post('/api/endpoint-authors', { user_id: userId })

export const revokeEndpointAuthor = (grantId) =>
  client.delete(`/api/endpoint-authors/${grantId}`)

export const listRouterGrants = (id) =>
  client.get(`/api/models/${id}/router-grants`)

export const replaceRouterGrants = (id, grants) =>
  client.put(`/api/models/${id}/router-grants`, { grants })

export const setCampusRouterDefault = (modelId) =>
  client.put('/api/router-models/default', { model_id: modelId })

export const listModelAccessGroups = () =>
  client.get('/api/model-access-groups')

export const createModelAccessGroup = (data) =>
  client.post('/api/model-access-groups', data)

export const updateModelAccessGroup = (id, data) =>
  client.put(`/api/model-access-groups/${id}`, data)

export const deleteModelAccessGroup = (id) =>
  client.delete(`/api/model-access-groups/${id}`)

export const replaceModelAccessGroupMembers = (id, userIds) =>
  client.put(`/api/model-access-groups/${id}/members`, { user_ids: userIds })
