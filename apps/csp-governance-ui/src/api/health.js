import client from './client'

// 服務健康總覽（W3-3⑦）。admin only。
//
// 刻意**不接受任何參數**：探測目標是後端寫死的部署服務清單，端點對任何查詢
// 參數 fail-closed 回 400。若哪天有人想在這裡加 `params`，先讀
// `services/csp/app/api/admin/health_overview.py` 開頭那段。
export const getHealthOverview = () => client.get('/api/admin/health/overview')
