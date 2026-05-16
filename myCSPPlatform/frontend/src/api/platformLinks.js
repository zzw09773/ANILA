import client from './client'

export const listPlatformLinks = (params) =>
  client.get('/api/platform-links', { params })

export const createPlatformLink = (data) =>
  client.post('/api/platform-links', data)

export const updatePlatformLink = (id, data) =>
  client.put(`/api/platform-links/${id}`, data)

// Soft delete — sets is_active=false, link can be revived by toggling is_active
// back via the edit form. Admin-tier OK.
export const deactivatePlatformLink = (id) =>
  client.delete(`/api/platform-links/${id}`)

// Hard delete — row is removed; CASCADE drops service_access_grant rows
// pointing at it. Irreversible.
export const purgePlatformLink = (id) =>
  client.delete(`/api/platform-links/${id}/purge`)

// Back-compat alias for existing callsites.
export const deletePlatformLink = deactivatePlatformLink
