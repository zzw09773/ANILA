import client from './client'

/**
 * 使用者回饋總覽(P3.4)。admin-tier only。
 * 後端永不回傳訊息正文 —— 只看 rating / rating_score / comment / reasons / 密等徽章。
 * rating_score 是拇指旁的細分(讚 6–10／爛 1–5 兩把尺);舊列或只按拇指時為 null。
 */
export const listFeedback = (params = {}) =>
  client.get('/api/admin/feedback', { params })

/**
 * 依「目前畫面上的篩選」匯出 CSV(同一支端點、同一份白名單,一樣不含正文)。
 * 匯出的是整個篩選結果而非當前這頁,所以刻意不帶 limit;
 * 筆數超過後端上限時會回 400,呼叫端要把訊息顯示出來,不能當成空檔案。
 */
export const exportFeedbackCsv = (params = {}) =>
  client.get('/api/admin/feedback', {
    params: { ...params, format: 'csv' },
    responseType: 'blob',
  })

/**
 * 點「查看被評分回覆」才打。view=all 才能拿到非活躍分支。
 * 沿用既有 client（admin cookie／CSRF）；後端 GET 本身會做密等稽核。
 * 不快取正文。
 */
export const getConversationAll = (conversationId) =>
  client.get(`/api/conversations/${conversationId}`, { params: { view: 'all' } })
