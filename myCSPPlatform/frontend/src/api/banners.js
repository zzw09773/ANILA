import client from './client'

// Admin announcement banners. Management is admin-only; the chat UI reads
// /active separately.
export const listBanners = () => client.get('/api/banners')
export const createBanner = (data) => client.post('/api/banners', data)
export const updateBanner = (id, patch) => client.put(`/api/banners/${id}`, patch)
export const deleteBanner = (id) => client.delete(`/api/banners/${id}`)
