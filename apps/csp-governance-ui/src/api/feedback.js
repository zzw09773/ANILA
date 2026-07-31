import client from './client'

/**
 * 使用者回饋總覽(P3.4)。admin-tier only。
 * 後端永不回傳訊息正文 —— 只看 rating / comment / reasons / 密等徽章。
 */
export const listFeedback = (params = {}) =>
  client.get('/api/admin/feedback', { params })
