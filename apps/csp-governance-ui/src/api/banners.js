import client from './client'

// Admin announcement banners. Management is admin-only; the chat UI reads
// /active separately.
// 目前啟用中的公告 —— 需要登入，聊天介面用。
export const listActiveBanners = () => client.get('/api/banners/active')
// 登入頁公告 —— **不需要登入**，這正是重點：等核准的人還沒有帳號，/active 對
// 他們一律 401。只回管理員逐則勾選（show_on_login）的那幾則，而且只帶
// { level, content }。取不到就走保底文案（src/utils/approvalContact.js）。
export const listPublicBanners = () => client.get('/api/banners/public')
export const listBanners = () => client.get('/api/banners')
export const createBanner = (data) => client.post('/api/banners', data)
export const updateBanner = (id, patch) => client.put(`/api/banners/${id}`, patch)
export const deleteBanner = (id) => client.delete(`/api/banners/${id}`)
