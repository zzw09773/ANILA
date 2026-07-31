import client from './client'

// Admin announcement banners. Management is admin-only; the chat UI reads
// /active separately.
// 目前啟用中的公告。登入頁的「等待核准」訊息也讀這支（見
// src/utils/approvalContact.js）；取不到就走保底文案。
export const listActiveBanners = () => client.get('/api/banners/active')
export const listBanners = () => client.get('/api/banners')
export const createBanner = (data) => client.post('/api/banners', data)
export const updateBanner = (id, patch) => client.put(`/api/banners/${id}`, patch)
export const deleteBanner = (id) => client.delete(`/api/banners/${id}`)
