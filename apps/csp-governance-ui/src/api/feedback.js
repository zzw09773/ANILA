import client from './client'

/**
 * 使用者回饋總覽(P3.4)。admin-tier only。
 * 後端永不回傳訊息正文 —— 只看 rating / comment / reasons / 密等徽章。
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
