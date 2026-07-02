import client from './client'

// doc 07 Service Registry — registered_services CRUD 封裝。
//
// 7a 後端契約：GET /api/services（可存取清單，admin 見全部）、admin CRUD、
// GET /api/services/{id}/audit-callbacks（若實作；未實作回 404，UI 需 feature-check）。
// legacy /api/platform-links 仍保留運作，PlatformLinksView 於 /api/services
// 回 404 時退回 platformLinks.js。

export const listServices = (params) =>
  client.get('/api/services', { params })

export const createService = (data) =>
  client.post('/api/services', data)

export const updateService = (id, data) =>
  client.put(`/api/services/${id}`, data)

// Soft delete — is_active=false，可由編輯表單復原。
export const deactivateService = (id) =>
  client.delete(`/api/services/${id}`)

// Hard delete — 移除整列；不可復原。授權歷史（grant rows）由後端另存軌跡。
export const purgeService = (id) =>
  client.delete(`/api/services/${id}/purge`)

// 稽核回呼檢視：per-service 最近 N 筆。端點可能尚未實作 → 呼叫端需處理 404。
export const listServiceAuditCallbacks = (id, params) =>
  client.get(`/api/services/${id}/audit-callbacks`, { params })
