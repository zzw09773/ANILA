import client from './client'

// 機敏分類盤點(doc 08 §15)。admin/owner-only,後端 require_admin 把關。
export const getClassificationInventory = () =>
  client.get('/api/classification/inventory')

// CSV 變體:blob 回應供瀏覽器下載(含 UTF-8 BOM,Excel 可正確辨識繁中)。
export const downloadClassificationInventoryCsv = () =>
  client.get('/api/classification/inventory', {
    params: { format: 'csv' },
    responseType: 'blob',
  })

// W2-11 持續性抽查報表(隨機樣本,含文件標題 → 雙重 gate)。
export const getClassificationSamplingReport = (n = 20) =>
  client.get('/api/classification/sampling-report', { params: { n } })

export const createClassificationSamplingReview = (payload) =>
  client.post('/api/classification/sampling-reviews', payload)
